"""B+ tree: values live only in leaves; linked leaves support range scans."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field


@dataclass(slots=True)
class Node:
    leaf: bool = True
    keys: list = field(default_factory=list)
    values: list = field(default_factory=list)
    children: list = field(default_factory=list)
    next: "Node | None" = None


class BPlusTree:
    def __init__(self, order=64):
        if order < 4:
            raise ValueError("order must be at least 4")
        self.order = order  # Maximum children per internal node.
        self.root = Node()
        self.size = 0

    def _leaf(self, key):
        node = self.root
        while not node.leaf:
            node = node.children[bisect_right(node.keys, key)]
        return node

    def get(self, key, default=None):
        node = self._leaf(key)
        i = bisect_left(node.keys, key)
        return node.values[i] if i < len(node.keys) and node.keys[i] == key else default

    def insert(self, key, value):
        node, path = self.root, []
        while not node.leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = node.children[i]
        i = bisect_left(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            raise ValueError(f"duplicate key: {key}")
        node.keys.insert(i, key)
        node.values.insert(i, value)
        self.size += 1
        while len(node.keys) >= self.order:
            mid = len(node.keys) // 2
            if node.leaf:
                right = Node(keys=node.keys[mid:], values=node.values[mid:], next=node.next)
                node.keys, node.values = node.keys[:mid], node.values[:mid]
                node.next = right
                separator = right.keys[0]
            else:
                separator = node.keys[mid]
                right = Node(False, node.keys[mid + 1:], children=node.children[mid + 1:])
                node.keys, node.children = node.keys[:mid], node.children[:mid + 1]
            if not path:
                self.root = Node(False, [separator], children=[node, right])
                return
            parent, i = path.pop()
            parent.keys.insert(i, separator)
            parent.children.insert(i + 1, right)
            node = parent

    def range(self, low=None, high=None, include_low=True, include_high=True):
        node = self.root if low is None else self._leaf(low)
        while not node.leaf:
            node = node.children[0]
        i = 0 if low is None else (bisect_left if include_low else bisect_right)(node.keys, low)
        while node:
            for j in range(i, len(node.keys)):
                key = node.keys[j]
                if high is not None and (key > high or (key == high and not include_high)):
                    return
                yield key, node.values[j]
            node, i = node.next, 0

    def validate(self):
        """Check structural invariants; useful for tests and teaching."""
        leaves, depths = [], set()

        def visit(node, depth, root=False):
            assert node.keys == sorted(set(node.keys))
            assert len(node.keys) < self.order
            if not root:
                assert len(node.keys) >= (self.order - 1) // 2
            if node.leaf:
                assert len(node.keys) == len(node.values)
                leaves.append(node)
                depths.add(depth)
                return (node.keys[0], node.keys[-1]) if node.keys else (None, None)
            assert len(node.children) == len(node.keys) + 1
            bounds = [visit(child, depth + 1) for child in node.children]
            for i, key in enumerate(node.keys):
                assert bounds[i][1] < key == bounds[i + 1][0]
            return bounds[0][0], bounds[-1][1]

        visit(self.root, 0, True)
        assert len(depths) == 1
        assert sum(len(n.keys) for n in leaves) == self.size
        for i, node in enumerate(leaves):
            assert node.next is (leaves[i + 1] if i + 1 < len(leaves) else None)
        return True
