"""Z3 is pinned because Render would otherwise install whatever the index serves."""

import unittest

import z3


class Z3Tests(unittest.TestCase):
    def test_solver_runs(self):
        amount = z3.Int("amount")
        solver = z3.Solver()
        solver.add(amount > 200)
        self.assertEqual(solver.check(), z3.sat)
        self.assertGreater(solver.model()[amount].as_long(), 200)
