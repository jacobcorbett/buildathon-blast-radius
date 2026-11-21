"""Follow scalar tokenizer configuration through the pinned Python core to actual reads."""
import ast
import inspect
from pathlib import Path

FIELD = "_blast_tokenizer_field"
ATTACH = "_blast_tokenizer_attach"
READ = "_blast_tokenizer_read"
SLOT = "_blast_tokenizer_origins"


def call(helper, args, node):
    return ast.copy_location(ast.Call(ast.Name(helper, ast.Load()), args, []), node)


def methods(tree, name):
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


class TokenizerFlow:
    def __init__(self, root, descriptor):
        self.descriptor = descriptor
        tokens = ast.parse((Path(root) / "sqlglot/tokens.py").read_bytes())
        core = ast.parse((Path(root) / "sqlglot/tokenizer_core.py").read_bytes())
        construct = methods(core, "TokenizerCore")["__init__"]
        boolean = {a.arg for a in construct.args.args if isinstance(a.annotation, ast.Name)
                   and a.annotation.id == "bool"}
        slots = {}
        for stmt in construct.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                    isinstance(stmt.targets[0], ast.Attribute) and
                    isinstance(stmt.value, ast.Name) and stmt.value.id in boolean):
                param = stmt.value.id
                if sum(isinstance(n, ast.Name) and n.id == param and isinstance(n.ctx, ast.Load)
                       for n in ast.walk(construct)) == 1:
                    slots[param] = stmt.targets[0].attr
        factory = methods(tokens, "Tokenizer")["_init_core"]
        self.fields = {}
        for n in ast.walk(factory):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "TokenizerCore":
                for k in n.keywords:
                    v = k.value
                    if (k.arg in slots and isinstance(v, ast.Attribute) and
                            isinstance(v.value, ast.Name) and v.value.id == "self" and v.attr.isupper()):
                        self.fields[slots[k.arg]] = v.attr

    def setting(self, obj, name):
        member = inspect.getattr_static(obj, name, None)
        if isinstance(member, self.descriptor) and type(obj).__getattribute__ is object.__getattribute__:
            return member
        return None

    def field(self, obj, name):
        member = self.setting(obj, name)
        return member.value if member is not None else getattr(obj, name)

    def attach(self, obj, core):
        origins = {}
        for slot, name in self.fields.items():
            member = self.setting(obj, name)
            if member is not None:
                origins[slot] = member
        try:
            setattr(core, SLOT, origins)
        except AttributeError:
            for member in origins.values():
                member.owner.eager_settings.add(member.site)
        return core

    def read(self, core, slot, value):
        member = getattr(core, SLOT, {}).get(slot)
        if member is not None:
            member.__get__(core, type(core))
        return value

    def instrument(self, tree, path):
        if path == "sqlglot/tokens.py":
            method = methods(tree, "Tokenizer")["_init_core"]
            for stmt in method.body:
                node = getattr(stmt, "value", None)
                if not (isinstance(stmt, ast.Return) and isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "TokenizerCore"):
                    continue
                for k in node.keywords:
                    v = k.value
                    if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                            and v.value.id == "self" and v.attr == self.fields.get(k.arg)):
                        k.value = call(FIELD, [v.value, ast.Constant(v.attr)], v)
                stmt.value = call(ATTACH, [ast.Name("self", ast.Load()), node], node)
        if path != "sqlglot/tokenizer_core.py":
            return tree
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TokenizerCore")
        for stmt in cls.body:
            if (isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__slots__"
                    for t in stmt.targets) and isinstance(stmt.value, ast.Tuple)):
                stmt.value.elts.append(ast.Constant(SLOT))
        for method in methods(tree, "TokenizerCore").values():
            if method.name != "__init__":
                self.instrument_reads(method)
        return tree

    def instrument_reads(self, method):
        nodes = list(ast.walk(method))
        if not method.args.args:
            return
        receiver = method.args.args[0].arg
        def field(node):
            return (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and node.attr in self.fields and isinstance(node.value, ast.Name)
                    and node.value.id == receiver)
        delayed = {}
        ignore = set()
        for stmt in method.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                    isinstance(stmt.targets[0], ast.Name) and field(stmt.value)):
                name = stmt.targets[0].id
                if sum(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
                       for n in nodes) == 1 and not any(isinstance(n, ast.Name) and n.id
                       in {"locals", "eval", "vars", "exec"} for n in nodes):
                    delayed[name] = stmt.value.attr
                    ignore.add(id(stmt.value))

        class Reads(ast.NodeTransformer):
            def visit(self, node):
                key = (node.attr if field(node) and id(node) not in ignore else
                       delayed.get(node.id) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) else None)
                node = super().visit(node)
                if key is None:
                    return node
                return call(READ, [ast.Name(receiver, ast.Load()), ast.Constant(key), node], node)
        Reads().visit(method)
