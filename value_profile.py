"""Record concrete lookup keys and comparison operands, independent of mutations."""
import ast
import operator
from types import BuiltinMethodType

HELPER = "_blast_radius_value_observer"


def trace_global_keys(tree, path, profile):
    """Observe dictionary domains for closed module constants used as lookup keys."""
    literals = {
        s.targets[0].id: s.value for s in tree.body if isinstance(s, ast.Assign)
        and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name) and s.targets[0].id.isupper()
        and isinstance(s.value, ast.Constant) and type(s.value.value) is str
    }
    calls = {}
    sites = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in literals
                and not n.keywords):
            name = n.args[0].id
            if name not in sites:
                token = literals[name]
                location = path, token.lineno, token.col_offset, token.end_col_offset
                sites[name] = len(profile.value_sites)
                profile.value_sites.append(location)
                profile.global_keys.add(location)
            calls[id(n)] = sites[name]
    if not sites:
        return tree

    class Trace(ast.NodeTransformer):
        def visit_Call(self, node):
            site = calls.get(id(node))
            if site is None:
                return self.generic_visit(node)
            args = [self.visit(node.func), node.args[0], *map(self.visit, node.args[1:])]
            return ast.copy_location(ast.Call(ast.Name(HELPER, ast.Load()),
                [ast.Constant(site), ast.Constant("get"), *args], []), node)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load) and node.id in sites:
                return ast.copy_location(ast.Call(ast.Name(HELPER, ast.Load()),
                    [ast.Constant(sites[node.id]), ast.Constant("opaque"), node], []), node)
            return node

    return Trace().visit(tree)


class ValueSites(ast.NodeTransformer):
    def __init__(self, profile, path):
        self.profile = profile
        self.path = path

    def observe(self, literal, kind, args, keywords, old):
        site = len(self.profile.value_sites)
        self.profile.value_sites.append((self.path, literal.lineno, literal.col_offset, literal.end_col_offset))
        return ast.copy_location(ast.Call(ast.Name(HELPER,ast.Load()),
                            [ast.Constant(site),ast.Constant(kind),*args],keywords),old)

    def visit_Call(self, node):
        self.generic_visit(node)
        if (not isinstance(node.func, ast.Attribute) or not node.args
                or any(k.arg in {None,"profile","site","kind"} for k in node.keywords)):
            return node
        literal=node.args[0]
        if not (isinstance(literal, ast.Constant) and type(literal.value) is str
                and literal.lineno == literal.end_lineno):
            return node
        name=node.func.attr
        if name not in {"get","_match_text_seq"}:
            return node
        return self.observe(literal,name,[node.func,*node.args],node.keywords,node)

    def visit_Compare(self,node):
        self.generic_visit(node)
        if len(node.ops)!=1 or not isinstance(node.ops[0],(ast.Eq,ast.NotEq)):
            return node
        left,right=node.left,node.comparators[0]
        for side,literal in enumerate((left,right)):
            if isinstance(literal,ast.Constant) and type(literal.value) is str and literal.lineno==literal.end_lineno:
                kind=("eq" if isinstance(node.ops[0],ast.Eq) else "ne")+str(side)
                return self.observe(literal,kind,[left,right],[],node)
        return node


def sample(profile, site, kind, *args, **kwargs):
    observed=(profile.eager_values if profile.loading else profile.execution.observation.domains)[site]
    if kind == "opaque":
        observed.add(None)
        return args[0]
    if None not in observed:
        if kind=="get":
            receiver=getattr(args[0],"__self__",None)
            if (type(args[0]) is BuiltinMethodType and args[0].__name__ == "get"
                    and type(receiver) is dict and len(receiver)<=80 and all(type(k) is str for k in receiver)):
                observed.update(receiver)
            else:
                observed.add(None)
        elif kind=="_match_text_seq":
            method=args[0]
            if getattr(method,"__qualname__",None)=="Parser._match_text_seq" and method.__module__=="sqlglot.parser":
                token=method.__self__._curr
                value=token.text
                if type(value) is str:
                    observed.add(value.upper())
                else:
                    observed.add(None)
            else:
                observed.add(None)
        else:
            other=args[1-int(kind[-1])]
            if type(other) is str:
                observed.add(other)
            elif type(other) not in (int,float,bool,bytes,type(None)):
                observed.add(None)
        if len(observed)>128:
            observed.clear()
            observed.add(None)
    if kind in {"get","_match_text_seq"}:
        return args[0](*args[1:],**kwargs)
    return (operator.eq if kind.startswith("eq") else operator.ne)(*args)
