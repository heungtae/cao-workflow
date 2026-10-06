import unittest
from arithmetic import add


class AdditionTests(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(5, add(2, 3))
        self.assertEqual(1, add(-2, 3))
