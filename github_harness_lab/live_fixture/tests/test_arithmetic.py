import unittest

from src.arithmetic import add


class ArithmeticTests(unittest.TestCase):
    def test_positive_numbers(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative_numbers(self):
        self.assertEqual(add(-5, 2), -3)

    def test_zero(self):
        self.assertEqual(add(7, 0), 7)


if __name__ == "__main__":
    unittest.main()
