"""Collect baseline operand invariants for simple identity comparisons.

The probe performs the original operation and only observes its operands.
It never evaluates an alternate comparison or sees a patch or sample data.
"""

import ast
import importlib.abc
import importlib.machinery
import re
import sys
from enum import Enum

OPERATOR = re.compile(rb"\s*(is(?:[ \t]+not)?)[ \t]*")
HELPER = "_blast_radius_profile_identity"


def standard_identity(kind):
    # Pinned CPython gives NoneType its own wrappers for object rich comparison.
    return kind is type(None) or (
        type.__getattribute__(kind, "__eq__") is object.__eq__
        and type.__getattribute__(kind, "__ne__") is object.__ne__
    )


def canonical_member(value, kind):
    name = object.__getattribute__(value, "_name_")
    return kind.__members__.get(name) is value


def unique_strings(kind):
    members = {}
    for member in kind.__members__.values():
        value = str.__str__(member)
        existing = members.setdefault(value, member)
        if existing is not member:
            return False
    return True


def equality_semantics(left, right):
    left_kind = type(left)
    right_kind = type(right)
    if standard_identity(left_kind) and standard_identity(right_kind):
        return "identity"
    if (
        left_kind is right_kind
        and issubclass(left_kind, str)
        and issubclass(left_kind, Enum)
        and left_kind.__eq__ is str.__eq__
        and left_kind.__ne__ is str.__ne__
        and canonical_member(left, left_kind)
        and canonical_member(right, right_kind)
        and unique_strings(left_kind)
    ):
        # Member aliases resolve to the same singleton. Distinct members of
        # one string enum have distinct underlying value strings.
        return "string_enum"
    return "unknown"


class Instrument(ast.NodeTransformer):
    def __init__(self, profile, path, source):
        self.profile = profile
        self.path = path
        self.lines = source.splitlines()

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            return node
        left, right = node.left, node.comparators[0]
        if not (
            isinstance(right, ast.Constant) and right.value is None
            or isinstance(right, ast.Attribute) and right.attr.isupper()
        ):
            return node
        if left.end_lineno != right.lineno:
            return node
        line = right.lineno
        start = left.end_col_offset
        match = OPERATOR.fullmatch(self.lines[line - 1][start:right.col_offset])
        if not match:
            return node
        identity = len(self.profile.sites)
        self.profile.sites.append((self.path, line, start + match.start(1), start + match.end(1)))
        self.profile.observed.append(set())
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id=HELPER, ctx=ast.Load()),
                args=[ast.Constant(identity), left, right, ast.Constant(isinstance(node.ops[0], ast.IsNot))],
                keywords=[],
            ),
            node,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, original, profile):
        super().__init__(original.name, original.path)
        self.profile = profile

    def exec_module(self, module):
        module.__dict__[HELPER] = self.profile.probe
        super().exec_module(module)

    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, filename=self.path)
        path = self.path.removeprefix(self.profile.root)
        tree = Instrument(self.profile, path, source).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class OperandProfile(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = str(root) + "/"
        self.sites = []
        self.observed = []
        self.enabled = True

    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.loader.path.startswith(self.root)
        ):
            spec.loader = Loader(spec.loader, self)
        return spec

    def probe(self, identity, left, right, negate):
        if self.enabled:
            observed = self.observed[identity]
            if "unknown" not in observed:
                try:
                    observed.add(equality_semantics(left, right))
                except Exception:
                    observed.add("unknown")
        return (left is not right) if negate else (left is right)

    def invariants(self):
        for site, observations in zip(self.sites, self.observed, strict=True):
            if observations and "unknown" not in observations:
                yield (*site, ",".join(sorted(observations)))
