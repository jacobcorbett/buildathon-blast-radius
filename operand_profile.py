"""Collect baseline operand invariants for simple identity comparisons.

The probe performs the original operation and only observes its operands.
It never evaluates an alternate comparison or sees a patch or sample data.
"""

import ast
import argument_usage
import tokenizer_profile
import importlib.abc
import importlib.machinery
import re
import operator
import sys
from types import CodeType
from enum import Enum
from functools import partial
from collections import defaultdict
from value_profile import HELPER as VALUE_HELPER, ValueSites, sample, trace_global_keys
from param_flow import CALL as FLOW_HELPER, ReadFlow, observe

OPERATOR = re.compile(rb"\s*(is(?:[ \t]+not)?)[ \t]*")
HELPER = "_blast_radius_profile_identity"
SETTING = "_blast_radius_profile_setting"
MAPPING = "_blast_radius_profile_mapping"
ORDER = "_blast_radius_profile_order"
USED = "_blast_radius_profile_operand"
REGISTRY = "_blast_radius_profile_binding"
ORDERING = {ast.Lt: ("<", operator.lt), ast.LtE: ("<=", operator.le),
            ast.Gt: (">", operator.gt), ast.GtE: (">=", operator.ge)}


class ObservedMapping(dict):
    """Value dependencies of a fixed-key literal map. Iterating its keys is value independent."""

    def __init__(self, profile, sites, values, argument_schema):
        super().__init__(values)
        self.profile = profile
        self.sites = sites
        self.argument_schema = argument_schema

    def observe(self, key):
        sites = self.sites.get(key, ())
        self.profile.execution.observation.settings.update(sites)
        if self.profile.loading:
            self.profile.eager_settings.update(sites)

    def __getitem__(self, key):
        self.observe(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.observe(key)
        return super().get(key, default)

    def __iter__(self):
        # This override also makes CPython's ** expansion use the mapping protocol.
        return super().__iter__()


def observe_all(method):
    def observed(self, *args, **kwargs):
        if self.argument_schema and method is dict.items:
            caller = sys._getframe(1).f_code
            if caller.co_qualname == "Expr.__init_subclass__" and caller.co_filename == self.profile.root + "sqlglot/expressions/core.py":
                return method(self, *args, **kwargs)
        for key in self.sites:
            self.observe(key)
        return method(self, *args, **kwargs)
    return observed


for _method in ("copy", "items", "values", "pop", "popitem", "setdefault", "__repr__",
                "__eq__", "__ne__", "__or__", "__ror__", "__ior__", "__reduce_ex__"):
    setattr(ObservedMapping, _method, observe_all(getattr(dict, _method)))


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
        if self.path.startswith("sqlglot/parsers/"):
            for stmt in node.body:
                if not (isinstance(stmt,ast.Assign) and len(stmt.targets)==1 and
                        isinstance(stmt.targets[0],ast.Name) and stmt.targets[0].id=="FUNCTIONS"
                        and isinstance(stmt.value,ast.Dict)):
                    continue
                for i,(key,expr) in enumerate(zip(stmt.value.keys,stmt.value.values)):
                    if not (isinstance(key,ast.Constant) and type(key.value) is str and
                            isinstance(expr,ast.Attribute) and expr.attr=="from_arg_list"
                            and isinstance(expr.value,ast.Attribute) and isinstance(expr.value.value,ast.Name)
                            and expr.value.value.id=="exp"):
                        continue
                    cls=expr.value
                    site=len(self.profile.settings)
                    self.profile.settings.append((self.path,cls.end_lineno,cls.end_col_offset-len(cls.attr),
                                                  cls.end_col_offset,("registry",cls.attr)))
                    stmt.value.values[i]=ast.copy_location(ast.Call(ast.Name(REGISTRY,ast.Load()),
                                                   [ast.Constant(site),expr],[]),expr)
        if self.path.startswith(("sqlglot/dialects/", "sqlglot/expressions/")):
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                        isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Dict)):
                    name = stmt.targets[0].id
                    if name.isupper() and self.path.startswith("sqlglot/dialects/"):
                        self.mapping(stmt, False)
                    elif name == "arg_types" and self.path.startswith("sqlglot/expressions/") and all(
                            isinstance(v, ast.Constant) and type(v.value) is bool for v in stmt.value.values):
                        self.mapping(stmt, True)
        if not self.path.startswith("sqlglot/dialects/") or len(node.bases) != 1:
            return node
        base = node.bases[0]
        dialect = isinstance(base, ast.Name) and base.id == "Dialect"
        tokenizer = node.name == "Tokenizer" and getattr(base, "attr", getattr(base, "id", None)) == "Tokenizer"
        if not dialect and not tokenizer:
            return node
        local_reads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                       and isinstance(n.ctx, ast.Load)}
        for stmt in node.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                continue
            name = stmt.targets[0].id
            if tokenizer and name not in self.profile.tokenizer.fields.values():
                continue
            value = stmt.value
            token = value.operand if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub) else value
            if (not name.isupper() or name in local_reads or not isinstance(token, ast.Constant)
                    or type(token.value) not in (int, bool, str)):
                continue
            site = len(self.profile.settings)
            self.profile.settings.append((self.path, token.lineno, token.col_offset, token.end_col_offset, name))
            stmt.value = ast.copy_location(ast.Call(ast.Name(SETTING, ast.Load()),
                                                   [ast.Constant(site), value], []), value)
        return node

    def mapping(self, stmt, argument_schema):
        value = stmt.value
        if not 0 < len(value.keys) <= 64 or not all(
            isinstance(key, ast.Constant) and type(key.value) is str for key in value.keys
        ):
            return
        sites = {}
        for key, entry in zip(value.keys, value.values):
            leaves = entry.elts if isinstance(entry, ast.Tuple) else [entry]
            if not all(isinstance(leaf, ast.Constant) and type(leaf.value) in (int, str, bool)
                       and leaf.lineno == leaf.end_lineno for leaf in leaves):
                return
            sites[key.value] = leaves
        indices = {}
        for key, leaves in sites.items():
            indices[key] = []
            for leaf in leaves:
                indices[key].append(len(self.profile.settings))
                self.profile.settings.append((self.path, leaf.lineno, leaf.col_offset,
                                              leaf.end_col_offset, "arg_types" if argument_schema else None))
        index = len(self.profile.mappings)
        self.profile.mappings.append((indices, argument_schema))
        stmt.value = ast.copy_location(ast.Call(ast.Name(MAPPING, ast.Load()),
                                               [ast.Constant(index), value], []), value)

    def visit_Compare(self, node):
        self.generic_visit(node)
        spelling = ({ast.Eq: "==", ast.NotEq: "!=", ast.In: "in", ast.NotIn: "not in"}
                    .get(type(node.ops[0])) if len(node.ops) == 1 else None)
        if spelling:
            left, right = node.left, node.comparators[0]
            if left.end_lineno == right.lineno:
                start = left.end_col_offset
                middle = self.lines[right.lineno - 1][start:right.col_offset]
                if middle.strip() == spelling.encode():
                    start += middle.index(spelling.encode())
                    site = len(self.profile.settings)
                    self.profile.settings.append((self.path, right.lineno, start,
                                                  start + len(spelling), ("order",)))
                    node.comparators[0] = ast.copy_location(ast.Call(
                        ast.Name(USED, ast.Load()), [ast.Constant(site), right], []), right)
            return node
        if len(node.ops) == 1 and type(node.ops[0]) in ORDERING:
            left, right = node.left, node.comparators[0]
            if left.end_lineno == right.lineno:
                text, operation = ORDERING[type(node.ops[0])]
                start = left.end_col_offset
                middle = self.lines[right.lineno-1][start:right.col_offset]
                if middle.strip() == text.encode():
                    start += middle.index(text.encode())
                    site = len(self.profile.settings)
                    self.profile.settings.append((self.path, right.lineno, start, start+len(text), ("order",)))
                    self.profile.orders[site] = operation
                    return ast.copy_location(ast.Call(ast.Name(ORDER, ast.Load()),
                                             [ast.Constant(site), left, right], []), node)
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
        module.__dict__[tokenizer_profile.FIELD] = self.profile.tokenizer.field
        module.__dict__[tokenizer_profile.ATTACH] = self.profile.tokenizer.attach
        module.__dict__[tokenizer_profile.READ] = self.profile.tokenizer.read
        module.__dict__[USED] = self.profile.operand_used
        module.__dict__[argument_usage.READ] = self.profile.arguments.read
        module.__dict__[argument_usage.BIND] = self.profile.arguments.bind
        module.__dict__[HELPER] = self.profile.probe
        module.__dict__[SETTING] = self.profile.setting
        module.__dict__[MAPPING] = self.profile.mapping
        module.__dict__[ORDER] = self.profile.order
        module.__dict__[REGISTRY] = self.profile.registry
        module.__dict__[VALUE_HELPER] = partial(sample,self.profile)
        module.__dict__[FLOW_HELPER] = partial(observe,self.profile)
        self.profile.loading += 1
        try:
            super().exec_module(module)
        finally:
            self.profile.loading -= 1

    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, filename=self.path)
        path = self.path.removeprefix(self.profile.root)
        pending_defaults = {}
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or func.decorator_list:
                continue
            args = func.args
            defaults = list(zip(reversed(args.posonlyargs + args.args), reversed(args.defaults)))
            defaults += list(zip(args.kwonlyargs, args.kw_defaults))
            eligible = [(arg.arg, default) for arg, default in defaults
                        if isinstance(default, ast.Constant) and type(default.value) in (int, str, bool)]
            if eligible:
                pending_defaults[func.lineno, func.name] = eligible
        tree = self.profile.tokenizer.instrument(tree, path)
        tree = trace_global_keys(tree, path, self.profile)
        tree = argument_usage.instrument(tree, path, self.profile.arguments)
        tree = ReadFlow(tree,path,self.profile,source).visit(tree)
        if path.startswith(("sqlglot/parsers/", "sqlglot/generators/")) or path in {
                "sqlglot/parser.py", "sqlglot/generator.py"}:
            tree = ValueSites(self.profile,path).visit(tree)
        tree = Instrument(self.profile, path, source).visit(tree)
        code = compile(ast.fix_missing_locations(tree), self.path, "exec")
        if self.profile.execution.active:
            pending = [code]
            while pending:
                obj = pending.pop()
                self.profile.arguments.codes[path, obj.co_qualname] = obj
                pending.extend(c for c in obj.co_consts if isinstance(c, CodeType))
                if obj.co_name == "<lambda>":
                    sys.monitoring.set_local_events(sys.monitoring.COVERAGE_ID, obj, sys.monitoring.events.PY_START)
                if path == "sqlglot/expressions/core.py" and obj.co_qualname == "Expression.error_messages":
                    self.profile.execution.validation = obj
                    sys.monitoring.set_local_events(sys.monitoring.COVERAGE_ID, obj, sys.monitoring.events.PY_START)
                defaults = pending_defaults.get((obj.co_firstlineno, obj.co_name))
                if defaults:
                    records = []
                    for name, token in defaults:
                        site = len(self.profile.settings)
                        self.profile.settings.append((path, token.lineno, token.col_offset,
                                                      token.end_col_offset, ("default", obj.co_firstlineno)))
                        records.append((site, name, token.value))
                    self.profile.execution.defaults[obj] = records
                    sys.monitoring.set_local_events(sys.monitoring.COVERAGE_ID, obj, sys.monitoring.events.PY_START)
        return code


class OperandProfile(importlib.abc.MetaPathFinder):
    def __init__(self, root, execution):
        self.execution = execution
        self.tokenizer = tokenizer_profile.TokenizerFlow(root, ObservedSetting)
        self.arguments = argument_usage.Arguments(self)
        self.settings = []
        self.mappings = []
        self.orders = {}
        self.value_sites = []
        self.global_keys = set()
        self.eager_values = defaultdict(set)
        self.parameter_sites = []
        self.parameter_rules = []
        self.safe_parameters = set()
        self.eager_ranges = {}
        self.eager_settings = set()
        self.loading = 0
        self.root = str(root) + "/"
        self.sites = []
        self.observed = []
        self.enabled = True

    def setting(self, site, value):
        return ObservedSetting(self, site, value)

    def mapping(self, index, value):
        sites, schema = self.mappings[index]
        return ObservedMapping(self, sites, value, schema)

    def registry(self,site,bound):
        code=getattr(getattr(bound,"__func__",None),"__code__",None)
        cls=getattr(bound,"__self__",None)
        if code is None or not isinstance(cls,type) or not self.execution.active:
            self.eager_settings.add(site)
        else:
            self.execution.registry_calls.setdefault(code,{}).setdefault(cls,set()).add(site)
            flags=sys.monitoring.get_local_events(sys.monitoring.COVERAGE_ID,code)
            sys.monitoring.set_local_events(sys.monitoring.COVERAGE_ID,code,flags|sys.monitoring.events.PY_START)
        return bound

    def order(self, site, left, right):
        result = self.orders[site](left, right)
        if self.loading:
            self.eager_settings.add(site)
        primitive = (int, float, bool, str, bytes)
        if type(left) not in primitive or type(right) not in primitive or left == right:
            self.execution.observation.settings.add(site)
        return result

    def operand_used(self, site, value):
        if self.loading:
            self.eager_settings.add(site)
        self.execution.observation.settings.add(site)
        return value

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
