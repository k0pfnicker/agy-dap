"""Unittest fixture suite for verifying the 'unittest' runner profile in agy-dap."""

from __future__ import annotations

import unittest

from checkout import calculate_total


class TestCheckoutUnittest(unittest.TestCase):
    """Standard library unittest.TestCase fixture."""

    def test_discount_unittest(self) -> None:
        total = calculate_total(subtotal=100.0, discount_rate=0.15, customer=None)
        self.assertEqual(total, 85.0)


if __name__ == "__main__":
    unittest.main()
