"""Record concrete lookup keys and comparison operands, independent of mutations."""
import ast
import operator

HELPER = "_blast_radius_value_observer"


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
    if None not in observed:
        if kind=="get":
            receiver=getattr(args[0],"__self__",None)
            if type(receiver) is dict and len(receiver)<=80 and all(type(k) is str for k in receiver):
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
