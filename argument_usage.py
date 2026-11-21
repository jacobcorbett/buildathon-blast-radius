"""Trace scalar arguments to reads, including verified same-module forwarding calls."""
import ast
from collections import defaultdict
from types import FunctionType, MethodType

READ = "_blast_argument_read"
BIND = "_blast_argument_bind"


class Arguments:
    def __init__(self, observer):
        self.observer = observer
        self.edges = []
        self.names = {}
        self.locations = []
        self.defaults = {}
        self.codes = {}
        self.eager = set()
        self.inputs = defaultdict(list)
        self.active = {}

    def event(self, key):
        target = self.eager if self.observer.loading else self.observer.execution.observation.arguments
        target.add(key)

    def read(self, key, value):
        self.event(key)
        for location in self.active.get(key, ()):
            self.event(("used", location))
        return value

    def bind(self, group, method, /, *args, **kwargs):
        target, bound = self.edges[group]
        function = method.__func__ if type(method) is MethodType else method
        execution = self.observer.execution
        previous = execution.in_audit
        execution.in_audit = True
        try:
            ok = (type(function) is FunctionType and function.__code__ is self.codes.get(target)
                  and bound == (type(method) is MethodType))
        finally:
            execution.in_audit = previous
        self.event(("call", group, ok))
        saved = {}
        for key, location, origin in self.inputs[group]:
            values = (location,) if location is not None else self.active.get(origin, ())
            if not values:
                continue
            if not ok:
                for value in values:
                    self.event(("used", value))
                continue
            saved.setdefault(key, self.active.get(key, ()))
            self.active[key] = frozenset(self.active.get(key, ())).union(values)
        try:
            return method(*args, **kwargs)
        finally:
            self.active.update(saved)

    def resolve(self, hits):
        selected = {key: hits[key] for key in self.names.values()}
        while True:
            updated = False
            for location, key, group, origin in self.locations:
                if origin is None:
                    continue
                mask = hits["call", group, False] | (hits["call", group, True] & selected[key])
                if mask & ~selected[origin]:
                    selected[origin] |= mask
                    updated = True
            if not updated:
                break
        sites = defaultdict(int)
        for location, key, group, origin in self.locations:
            if location is not None:
                sites[location] |= hits["call", group, False] | hits["used", location]
        return sites, {loc: selected[key] for loc, key in self.defaults.items()}


def definitions(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            yield "", node
        if isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.FunctionDef):
                    yield node.name + ".", method


def instrument(tree, path, registry):
    funcs = {}
    targets = defaultdict(list)
    for owner, func in definitions(tree):
        decorators = [getattr(d, "id", None) for d in func.decorator_list]
        if decorators not in ([], ["classmethod"], ["staticmethod"]):
            continue
        body_nodes = [n for stmt in func.body for n in ast.walk(stmt)]
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                              ast.Yield, ast.YieldFrom, ast.GeneratorExp)) for n in body_nodes):
            continue
        if any(isinstance(n, ast.Name) and n.id in {"locals", "vars", "eval", "exec", "super"} or
               isinstance(n, ast.Attribute) and n.attr in {"f_locals", "_getframe", "currentframe"}
               for n in body_nodes):
            continue
        args = func.args
        positional = args.posonlyargs + args.args
        defaults = [*zip(reversed(positional), reversed(args.defaults)),
                    *zip(args.kwonlyargs, args.kw_defaults)]
        ident = path, owner + func.name
        tracked = {}
        for arg, val in defaults:
            if not (isinstance(val, ast.Constant) and type(val.value) is bool):
                continue
            key = registry.names.setdefault((ident, arg.arg), len(registry.names))
            tracked[arg.arg] = key
            registry.defaults[path, val.lineno, val.col_offset, val.end_col_offset] = key
        if tracked:
            bound = bool(owner) and decorators != ["staticmethod"]
            funcs[func] = tracked
            targets[func.name].append((ident, tracked, positional[bound:], bound))
    names, binds = {}, {}

    def visit_body(func, tracked):
        forwarded = set()
        for stmt in func.body:
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "attr", getattr(call.func, "id", None))
                candidates = targets.get(name, ())
                if len(candidates) != 1:
                    continue
                ident, params, positional, bound = candidates[0]
                provided = [(k.arg, k.value) for k in call.keywords]
                if not any(isinstance(a, ast.Starred) for a in call.args):
                    provided.extend((arg.arg, value) for arg, value in zip(positional, call.args))
                group = len(registry.edges)
                for arg, value in provided:
                    if arg not in params:
                        continue
                    origin = tracked.get(value.id) if isinstance(value, ast.Name) else None
                    location = None
                    if isinstance(value, ast.Constant) and type(value.value) is bool:
                        location = path, value.lineno, value.col_offset, value.end_col_offset
                    if origin is None and location is None:
                        continue
                    if id(call) not in binds:
                        registry.edges.append((ident, bound))
                        binds[id(call)] = group
                    group = binds[id(call)]
                    registry.locations.append((location, params[arg], group, origin))
                    registry.inputs[group].append((params[arg], location, origin))
                    forwarded.add(id(value))
        for stmt in func.body:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                        and node.id in tracked and id(node) not in forwarded):
                    names[id(node)] = tracked[node.id]

    for _, func in definitions(tree):
        # Overlapping nested definitions are opaque to this graph.
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))
               for stmt in func.body for n in ast.walk(stmt)):
            continue
        visit_body(func, funcs.get(func, {}))

    class Probe(ast.NodeTransformer):
        def visit(self, n):
            key = id(n)
            n = super().visit(n)
            if key in names:
                return ast.copy_location(ast.Call(ast.Name(READ, ast.Load()), [ast.Constant(names[key]), n], []), n)
            if key in binds:
                return ast.copy_location(ast.Call(ast.Name(BIND, ast.Load()),
                    [ast.Constant(binds[key]), n.func, *n.args], n.keywords), n)
            return n

    return Probe().visit(tree)
