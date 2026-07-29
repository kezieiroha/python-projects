"""Tests for pure Fib, upside, and alert-zone calculations."""

import unittest

from modules.calculations import classify_zone, fib, upside_to_ath


class CalculationTest(unittest.TestCase):
    def test_fib_and_upside_to_ath(self):
        self.assertEqual(fib(200, 100, 0.382), 161.8)
        self.assertEqual(upside_to_ath(100, 200), 1.0)

    def test_classify_zone_boundaries(self):
        self.assertEqual(classify_zone(120, 100, 200).rank, 0)
        self.assertEqual(classify_zone(130, 100, 200).rank, 1)
        self.assertEqual(classify_zone(140, 100, 200).rank, 2)
        self.assertEqual(classify_zone(160, 100, 200).rank, 3)
        self.assertEqual(classify_zone(170, 100, 200).rank, 4)
        self.assertEqual(classify_zone(190, 100, 200).rank, 5)
        self.assertEqual(classify_zone(100, None, None).rank, 9)


if __name__ == "__main__":
    unittest.main()
