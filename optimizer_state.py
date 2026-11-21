import enum, hashlib, json, types
"""Store expression state with shared references and parent links for offline replay."""
import io,pickle
def make(cls):
    return object.__new__(cls)

class Pickler(pickle.Pickler):
    def reducer_override(self, obj):
        from sqlglot import exp
        if isinstance(obj,exp.Expr):
            state={k:getattr(obj,k) for base in type(obj).__mro__ for k in vars(base).get("__slots__",())
                   if k not in ("__dict__","__weakref__") and hasattr(obj,k)}
            state["_hash"]=None
            return make,(type(obj),),(None,state)
        return NotImplemented

def encode(args,kw):
    from sqlglot import exp
    marked=[]
    class Capture(Pickler):
        def reducer_override(self,obj):
            if isinstance(obj,exp.Expr) and getattr(obj,"_hash",None) is not None:marked.append(obj)
            return super().reducer_override(obj)
    stream=io.BytesIO();p=Capture(stream,protocol=5);p.dump((args,kw));p.dump(marked)
    return stream.getvalue()

def decode(data):
    p=pickle.Unpickler(io.BytesIO(data))
    args,kw=p.load()
    for item in p.load():hash(item)
    return args,kw

def digest(result,args,kwargs):
    seen={}
    def pack(value):
        t=type(value); name=t.__module__+"."+t.__qualname__
        if value is None or t in (str,int,bool):return value
        if t is float:return ["float",value.hex()]
        if t is bytes:return ["bytes",value.hex()]
        if isinstance(value,enum.Enum):return ["enum",name,value.name]
        if isinstance(value,type):return ["class",value.__module__,value.__qualname__]
        if t is types.FunctionType:return ["function",value.__module__,value.__qualname__]
        if t in (tuple,range):return [name,[pack(v) for v in value]]
        if t in (set,frozenset):
            if any(type(v) not in (str,int,bool) and not isinstance(v,enum.Enum) for v in value):
                raise ValueError("unhandled set of objects")
            return [name,sorted((pack(v) for v in value),key=repr)]
        ident=id(value)
        if ident in seen:return ["ref",seen[ident]]
        seen[ident]=len(seen)
        if t is dict:return [name,[[pack(k),pack(v)] for k,v in value.items()]]
        if t is list:return [name,[pack(v) for v in value]]
        if not name.startswith("sqlglot."):raise ValueError("unhandled type "+name)
        attributes={}
        if hasattr(value,"__dict__"):attributes.update(vars(value))
        for base in t.__mro__:
            slots=vars(base).get("__slots__",())
            if isinstance(slots,str):slots=(slots,)
            for k in slots:
                if k not in {"__dict__","__weakref__"} and hasattr(value,k):
                    attributes[k]=getattr(value,k)
        if "_hash" in attributes and name.startswith("sqlglot.expressions."):
            # Expression hashes include Python's random process salt.
            attributes["_hash"]=attributes["_hash"] is not None
        return [name,[[k,pack(v)] for k,v in sorted(attributes.items())]]
    data=pack((result,args,kwargs))
    return hashlib.sha256(json.dumps(data,ensure_ascii=True,separators=(",",":")).encode()).hexdigest()
