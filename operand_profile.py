"""Collect baseline operand invariants for simple identity comparisons.

The probe performs the original operation and only observes its operands.
It never evaluates an alternate comparison or sees a patch or sample data.
"""

import ast
import importlib.abc
import importlib.machinery
import re
from enum import Enum

OPERATOR = re.compile(rb"\s*(is(?:[ \t]+not)?)[ \t]*")
HELPER = "_blast_radius_profile_identity"
SETTING = "_blast_radius_profile_setting"


class ObservedSetting:
    def __init__(self, owner, site, value):
        self.owner, self.site, self.value = owner, site, value

    def __get__(self, instance, cls):
        profile = self.owner
        if profile.loading:
            profile.eager_settings.add(self.site)
        profile.execution.observation.settings.add(self.site)
        return self.value


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

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        if (not self.path.startswith("sqlglot/dialects/") or len(node.bases) != 1
                or not isinstance(node.bases[0], ast.Name) or node.bases[0].id != "Dialect"):
            return node
        local_reads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                       and isinstance(n.ctx, ast.Load)}
        for stmt in node.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                continue
            name = stmt.targets[0].id
            value = stmt.value
            token = value.operand if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub) else value
            if (not name.isupper() or name in local_reads or not isinstance(token, ast.Constant)
                    or type(token.value) not in (int, bool)):
                continue
            site = len(self.profile.settings)
            self.profile.settings.append((self.path, token.lineno, token.col_offset, token.end_col_offset, name))
            stmt.value = ast.copy_location(ast.Call(ast.Name(SETTING, ast.Load()),
                                                   [ast.Constant(site), value], []), value)
        return node

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
        module.__dict__[SETTING] = self.profile.setting
        self.profile.loading += 1
        try:
            super().exec_module(module)
        finally:
            self.profile.loading -= 1

    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, filename=self.path)
        path = self.path.removeprefix(self.profile.root)
        tree = Instrument(self.profile, path, source).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class OperandProfile(importlib.abc.MetaPathFinder):
    def __init__(self, root, execution):
        self.execution = execution
        self.settings = []
        self.eager_settings = set()
        self.loading = 0
        self.root = str(root) + "/"
        self.sites = []
        self.observed = []
        self.enabled = True

    def setting(self, site, value):
        return ObservedSetting(self, site, value)

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
