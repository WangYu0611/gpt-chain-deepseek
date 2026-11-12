import unittest
from src.arithmetic import add


class NumericEdgeTests(unittest.TestCase):
    def test_decimal_sum(self):
        self.assertAlmostEqual(add(0.1, 0.2), 0.3)

    def test_large_integers(self):
        self.assertEqual(add(10**30, -1), 10**30 - 1)


if __name__ == '__main__':
    unittest.main()

