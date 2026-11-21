"""Summarize uses of immutable integer defaults within direct, same-module calls.

Any read outside a known comparison or a verified forwarding edge is opaque.
The profile records an interval containing the original default for each test.
"""
import ast
import operator
import logging
import sys

OPS={ast.Lt:operator.lt,ast.LtE:operator.le,ast.Gt:operator.gt,ast.GtE:operator.ge,
     ast.Eq:operator.eq,ast.NotEq:operator.ne}
CALL="_blast_parameter_flow"
BOUND=2**53


def roots_for(tree, path, profile):
    funcs={f.name:f for f in tree.body if isinstance(f,ast.FunctionDef) and not f.decorator_list}
    parent={id(c):n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    consumers={}
    for f in funcs.values():
        args=f.args.posonlyargs+f.args.args
        for arg,val in [*zip(reversed(args),reversed(f.args.defaults)),*zip(f.args.kwonlyargs,f.args.kw_defaults)]:
            if not (isinstance(val,ast.Constant) and type(val.value) is int and 0<=val.value<BOUND):
                continue
            root=len(profile.parameter_sites)
            profile.parameter_sites.append((path,val.lineno,val.col_offset,val.end_col_offset,val.value,f.lineno))
            todo=[(f,arg.arg)];seen=set();events=[];valid=True
            while todo:
                fn,name=todo.pop()
                if (fn.name,name) in seen:continue
                seen.add((fn.name,name))
                for n in ast.walk(fn):
                    if isinstance(n,ast.Attribute) and n.attr in {"_getframe","currentframe","f_locals"}:
                        valid=False
                    if isinstance(n,ast.Name) and n.id in {"locals","eval","exec","vars","super"}:
                        valid=False
                    if not isinstance(n,ast.Name) or n.id!=name or not isinstance(n.ctx,ast.Load):
                        continue
                    p=parent[id(n)]
                    if isinstance(p,ast.Compare) and len(p.ops)==1 and type(p.ops[0]) in OPS:
                        events.append((id(p),("compare",int(n is not p.left)),root))
                        continue
                    kw=p.arg if isinstance(p,ast.keyword) else None
                    call=parent[id(p)] if isinstance(p,ast.keyword) else p
                    if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id in funcs:
                        target=funcs[call.func.id]
                        positional=target.args.posonlyargs+target.args.args
                        forwarded=kw
                        if n in call.args and not any(isinstance(a,ast.Starred) for a in call.args):
                            i=call.args.index(n)
                            if i<len(positional):forwarded=positional[i].arg
                        if forwarded in {a.arg for a in positional+target.args.kwonlyargs}:
                            todo.append((target,forwarded))
                            events.append((id(call.func),("forward",target.name),root))
                            continue
                    opaque=("opaque",0)
                    if isinstance(p,ast.FormattedValue) and p.value is n and p.format_spec is None:
                        msg=parent.get(id(p))
                        use=parent.get(id(msg))
                        if (isinstance(msg,ast.JoinedStr) and isinstance(use,ast.Call)
                                and msg in use.args and isinstance(use.func,ast.Attribute)
                                and use.func.attr in {"info","debug","warning","error","critical"}
                                and isinstance(use.func.value,ast.Name)):
                            opaque=("log",(use.func.value.id,use.func.attr))
                    events.append((id(n),opaque,root))
            if valid:
                for ident,kind,root in events:
                    consumers.setdefault(ident,{}).setdefault(kind,set()).add(root)
                profile.safe_parameters.add(root)
    return consumers


class ReadFlow(ast.NodeTransformer):
    def __init__(self,tree,path,profile,source):
        self.profile=profile
        self.path=path
        self.lines=source.splitlines()
        self.consumers=roots_for(tree,path,profile)

    def visit(self,node):
        rules=self.consumers.get(id(node))
        node=super().visit(node)
        if not rules:return node
        rec=len(self.profile.parameter_rules)
        boundary=None
        if isinstance(node,ast.Compare):
            op=type(node.ops[0]).__name__
            spelling={"Lt":"<","Gt":">","LtE":"<=","GtE":">="}.get(op)
            x,y=node.left,node.comparators[0]
            if spelling and x.end_lineno==y.lineno:
                middle=self.lines[y.lineno-1][x.end_col_offset:y.col_offset]
                if middle.strip()==spelling.encode():
                    start=x.end_col_offset+middle.index(spelling.encode())
                    boundary=len(self.profile.settings)
                    self.profile.settings.append((self.path,y.lineno,start,start+len(spelling),("order",)))
                    self.profile.orders[boundary]=ORDER[op]
            self.profile.parameter_rules.append((self.path,rules,boundary))
            return ast.copy_location(ast.Call(ast.Name(CALL,ast.Load()),
                    [ast.Constant(rec),ast.Constant(op),node.left,node.comparators[0]],[]),node)
        self.profile.parameter_rules.append((self.path,rules,None))
        return ast.copy_location(ast.Call(ast.Name(CALL,ast.Load()),[ast.Constant(rec),ast.Constant(""),node],[]),node)


ORDER={k.__name__:v for k,v in OPS.items()}


def observe(profile, rec, op, left, right=None):
    path,rules,boundary=profile.parameter_rules[rec]
    ranges=(profile.eager_ranges if profile.loading else profile.execution.observation.ranges)
    for (mode,side),roots in rules.items():
        if mode=="log":
            name,method=side
            caller=sys._getframe(1)
            logger=caller.f_locals.get(name,caller.f_globals.get(name))
            if (type(logger) is logging.Logger and getattr(getattr(logger,method),"__func__",None)
                    is getattr(logging.Logger,method) and not logger.isEnabledFor(getattr(logging,method.upper()))):
                continue
        if mode=="forward":
            if getattr(left,"__module__",None)==path[:-3].replace("/",".") and getattr(left,"__qualname__",None)==side:
                continue
        for root in roots:
            old=profile.parameter_sites[root][4]
            value=right if mode=="compare" and side else left
            if mode!="forward" and (type(value) is not int or value!=old):
                continue
            lo=hi=old
            if mode=="compare":
                other=left if side else right
                if type(other) is int and -BOUND<other<BOUND:
                    if old!=other:
                        # Stay strictly on the same side, valid for all six comparisons.
                        lo,hi=(-BOUND,other-1) if old<other else (other+1,BOUND)
                    elif op in {"Lt","Gt","LtE","GtE"}:
                        # Equality and one adjacent half-line yield the same comparison result.
                        lower=(op in {"Lt","GtE"}) != bool(side)
                        lo,hi=(old,BOUND) if lower else (-BOUND,old)
            a,b=ranges.get(root,(-BOUND,BOUND))
            ranges[root]=(max(a,lo),min(b,hi))
    if boundary is not None:
        return profile.order(boundary,left,right)
    return ORDER[op](left,right) if op else left
