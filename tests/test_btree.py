import random
import unittest

from minidb.btree import BPlusTree


class BPlusTreeTests(unittest.TestCase):
    def test_empty_and_invalid_order(self):
        with self.assertRaises(ValueError):
            BPlusTree(3)
        tree = BPlusTree()
        self.assertIsNone(tree.get(1))
        self.assertEqual(list(tree.range()), [])
        self.assertTrue(tree.validate())

    def test_random_sequential_reverse_and_splits(self):
        for order in (4, 5, 8, 64):
            for mode in ("random", "sequential", "reverse"):
                with self.subTest(order=order, mode=mode):
                    keys = list(range(1500))
                    if mode == "random":
                        random.Random(42).shuffle(keys)
                    elif mode == "reverse":
                        keys.reverse()
                    tree = BPlusTree(order)
                    for i, key in enumerate(keys):
                        tree.insert(key, key * 7)
                        if i % 100 == 0:
                            tree.validate()
                    tree.validate()
                    self.assertEqual([tree.get(k) for k in range(1500)], [k * 7 for k in range(1500)])
                    self.assertEqual(list(tree.range()), [(k, k * 7) for k in range(1500)])
                    self.assertIsNone(tree.get(-1))
                    self.assertIsNone(tree.get(1500))

    def test_range_boundaries_and_duplicates(self):
        tree = BPlusTree(4)
        for key in range(0, 100, 2):
            tree.insert(key, str(key))
        for low, high in ((None, None), (5, 31), (4, 30), (31, 5), (-10, 100), (4, 4)):
            for left in (True, False):
                for right in (True, False):
                    expected = [(k, str(k)) for k in range(0, 100, 2)
                                if (low is None or k > low or (left and k == low))
                                and (high is None or k < high or (right and k == high))]
                    self.assertEqual(list(tree.range(low, high, left, right)), expected)
        with self.assertRaises(ValueError):
            tree.insert(4, "changed")
        self.assertEqual(tree.get(4), "4")
        self.assertEqual(tree.size, 50)
