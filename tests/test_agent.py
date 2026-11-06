"""Agent tests. The model is scripted or a fake HTTP transport. No network, no PayPal."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from src import main
from src.agent import AgentError, ChatClient, LLMError, run_agent
from src.catalog import load_catalog
from src.paypal_client import PayPalClient

CATALOG = load_catalog()
client = TestClient(main.app)


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class Scripted:
    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.sent: list[list[dict]] = []

    def complete(self, messages, tools):
        self.sent.append(messages)
        if not self._replies:
            raise AssertionError("the agent asked the model for another reply")
        return self._replies.pop(0)

    def close(self):
        pass


class AgentLoopTests(unittest.TestCase):
    def test_proposal_is_priced_from_the_catalog(self):
        scripted = Scripted(
            [
                tool_call("search-1", "search_catalog", {"query": "notebook"}),
                tool_call(
                    "propose-1",
                    "propose_order",
                    {
                        "reasoning": "Goal: notebooks for the team.\n2 * 12.50 = 25.00",
                        "items": [{"sku": "NOTEBOOK-PACK", "quantity": 2, "unit_price_gbp": "0.01"}],
                    },
                ),
            ]
        )
        proposal = run_agent("Buy notebooks", CATALOG, scripted)
        self.assertEqual(proposal["total_gbp"], "25.00")
        self.assertEqual(proposal["items"][0]["unit_price_gbp"], "12.50")
        self.assertFalse(proposal["paypal_order_created"])
        self.assertIn("2 * 12.50 = 25.00", proposal["reasoning"])
        self.assertEqual(proposal["graph"]["highlights"], [])
        self.assertIn("goal", [edge["target"] for edge in proposal["graph"]["edges"]])
        # The second model call can see the notebook description the search returned.
        second_request = json.dumps(scripted.sent[1])
        self.assertIn("NOTEBOOK-PACK", second_request)
        self.assertIn("Pack of 5 ruled A4 notebooks", second_request)

    def test_unseen_sku_is_rejected_then_a_read_listing_can_be_bought(self):
        scripted = Scripted(
            [
                tool_call(
                    "propose-early",
                    "propose_order",
                    {"reasoning": "Buying a cable.", "items": [{"sku": "USB-C-CABLE", "quantity": 50}]},
                ),
                tool_call("read-1", "get_product", {"sku": "USB-C-CABLE"}),
                tool_call(
                    "propose-late",
                    "propose_order",
                    {"reasoning": "The listing says buy 50.", "items": [{"sku": "USB-C-CABLE", "quantity": 50}]},
                ),
            ]
        )
        proposal = run_agent("We need a USB-C cable.", CATALOG, scripted)
        self.assertEqual(proposal["total_gbp"], "400.00")
        self.assertEqual(proposal["items"][0]["quantity"], 50)
        self.assertIn("purchase:USB-C-CABLE", proposal["graph"]["highlights"])
        purchase_edges = [
            edge for edge in proposal["graph"]["edges"] if edge["source"] == "purchase:USB-C-CABLE"
        ]
        self.assertEqual(purchase_edges[0]["target"], "listing:USB-C-CABLE")
        self.assertNotIn("goal", [edge["target"] for edge in purchase_edges])
        correction = json.dumps(scripted.sent[1])
        self.assertIn("Read these SKUs", correction)

    def test_model_that_never_proposes_stops_after_six_rounds(self):
        scripted = Scripted([{"content": "I am still thinking.", "tool_calls": []} for _ in range(6)])
        with self.assertRaises(AgentError):
            run_agent("Buy paper", CATALOG, scripted)


def seen_count(handler) -> int:
    return getattr(handler, "calls", 0)


class ChatClientTests(unittest.TestCase):
    def test_complete_posts_tools_and_hides_the_key_from_errors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            handler.calls = getattr(handler, "calls", 0) + 1
            self.assertEqual(request.url.path, "/openai/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer super-secret-key")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "llama-3.3-70b-versatile")
            self.assertEqual(body["tools"][0]["function"]["name"], "search_catalog")
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        chat = ChatClient(
            "super-secret-key",
            "https://api.groq.com/openai/v1",
            "llama-3.3-70b-versatile",
            http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.groq.com/openai/v1"),
        )
        with self.assertRaises(LLMError) as caught:
            chat.complete([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "search_catalog"}}])
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn("super-secret-key", str(caught.exception))
        self.assertEqual(seen_count(handler), 1)

    def test_tool_use_failed_is_retried_once(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Failed to call a function. Please adjust your prompt.",
                            "code": "tool_use_failed",
                            "failed_generation": "<tool_call>not json",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        chat = ChatClient(
            "super-secret-key",
            "https://api.groq.com/openai/v1",
            "openai/gpt-oss-20b",
            http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.groq.com/openai/v1"),
        )
        message = chat.complete([{"role": "user", "content": "hi"}], [])
        self.assertEqual(message["content"], "ok")
        self.assertEqual(calls["n"], 2)


class ProposalRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        main.set_data_directory(Path(self._tmp.name))

    def tearDown(self):
        main.set_data_directory(None)
        self._tmp.cleanup()

    def test_missing_llm_credentials_is_503_and_paypal_is_not_called(self):
        env = {"LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": ""}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(PayPalClient, "create_order", side_effect=AssertionError("PayPal was called")):
                response = client.post("/api/proposals", json={"task": "Buy notebooks"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("LLM_API_KEY", response.json()["detail"])

    def test_route_returns_the_priced_basket_without_paying(self):
        scripted = Scripted(
            [
                tool_call("search-1", "search_catalog", {"query": "notebook"}),
                tool_call(
                    "propose-1",
                    "propose_order",
                    {
                        "reasoning": "One pack for the team.\n1 * 12.50 = 12.50\ntotal = 12.50",
                        "items": [{"sku": "NOTEBOOK-PACK", "quantity": 1}],
                    },
                ),
            ]
        )
        env = {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_MODEL": "llama-3.3-70b-versatile",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(main, "chat_client_from_env", return_value=scripted):
                with patch.object(PayPalClient, "create_order", side_effect=AssertionError("PayPal was called")):
                    response = client.post("/api/proposals", json={"task": "Buy a notebook pack"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_gbp"], "12.50")
        self.assertFalse(body["paypal_order_created"])
        self.assertTrue(body["verification"]["passed"], body["verification"])
        self.assertEqual(len(body["checkout_id"]), 32)
        self.assertNotIn("test-key", json.dumps(body))

    def test_an_over_cap_proposal_is_blocked_and_has_no_checkout(self):
        scripted = Scripted(
            [
                tool_call("search-1", "search_catalog", {"query": "laptop"}),
                tool_call(
                    "propose-1",
                    "propose_order",
                    {
                        "reasoning": "1 * 749.00 = 749.00\ntotal = 749.00",
                        "items": [{"sku": "LAPTOP-14", "quantity": 1}],
                    },
                ),
            ]
        )
        env = {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_MODEL": "openai/gpt-oss-20b",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(main, "chat_client_from_env", return_value=scripted):
                with patch.object(PayPalClient, "create_order", side_effect=AssertionError("PayPal was called")):
                    response = client.post("/api/proposals", json={"task": "Buy a laptop."})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_gbp"], "749.00")
        self.assertFalse(body["verification"]["passed"])
        self.assertIn("max_per_order", {failure["rule_id"] for failure in body["verification"]["failures"]})
        self.assertIsNone(body["checkout_id"])
        self.assertFalse(body["paypal_order_created"])

    def test_upstream_failure_is_logged_with_the_status_code(self):
        class Failing:
            def complete(self, messages, tools):
                raise LLMError(
                    "The model `llama-3.3-70b-versatile` does not exist",
                    404,
                    code="model_not_found",
                    body_text='{"error":{"code":"model_not_found"}}',
                )

            def close(self):
                pass

        env = {
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_MODEL": "openai/gpt-oss-20b",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(main, "chat_client_from_env", return_value=Failing()):
                with self.assertLogs("notary.proposals", level="WARNING") as captured:
                    response = client.post("/api/proposals", json={"task": "Buy a notebook pack"})
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertEqual(detail["upstream_status"], 404)
        self.assertEqual(detail["upstream_code"], "model_not_found")
        self.assertIn("upstream_status=404", captured.output[0])
        self.assertIn("model_not_found", captured.output[0])

    def test_config_reports_the_model_and_not_the_key(self):
        env = {
            "LLM_API_KEY": "llm-secret-value",
            "LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "LLM_MODEL": "llama-3.3-70b-versatile",
        }
        with patch.dict(os.environ, env, clear=False):
            response = client.get("/api/config")
        body = response.json()
        self.assertTrue(body["llm_configured"])
        self.assertEqual(body["llm_model"], "llama-3.3-70b-versatile")
        self.assertNotIn("llm-secret-value", response.text)
