"""Serialize pinned optimizer call inputs and compare their externally visible object graph."""
import enum
import hashlib
import io
import json
import pickle
import sys
import types


def make(cls):
    return object.__new__(cls)


def slots(cls):
    for base in cls.__mro__:
        names = vars(base).get("__slots__", ())
        for name in (names,) if isinstance(names, str) else names:
            if name not in {"__dict__", "__weakref__"}:
                yield name


def encode(args, kwargs):
    from sqlglot import exp
    hashed = []

    class InputPickler(pickle.Pickler):
        def reducer_override(self, value):
            if not isinstance(value, exp.Expr):
                return NotImplemented
            state = {k: getattr(value, k) for k in slots(type(value)) if hasattr(value, k)}
            if state.get("_hash") is not None:
                hashed.append(value)
            state["_hash"] = None
            return make, (type(value),), (None, state)

    stream = io.BytesIO()
    writer = InputPickler(stream, protocol=5)
    writer.dump((args, kwargs))
    writer.dump(hashed)
    return stream.getvalue()


def decode(data):
    reader = pickle.Unpickler(io.BytesIO(data))
    args, kwargs = reader.load()
    for value in reader.load():
        hash(value)
    return args, kwargs


def check_hashes(caches):
    """A cached salted hash must equal the value freshly computed in this interpreter."""
    try:
        for node, _ in caches:
            node._hash = None
        for node, original in caches:
            if original is not None and hash(node) != original:
                raise ValueError("stale expression hash")
    finally:
        for node, original in caches:
            node._hash = original


def digest(result, args, kwargs):
    from sqlglot import exp

    seen = {}
    caches = []

    def pack(value):
        cls = type(value)
        name = cls.__module__ + "." + cls.__qualname__
        if value is None or cls in (str, int, bool):
            return value
        if cls is float:
            return ["float", value.hex()]
        if cls is bytes:
            return ["bytes", value.hex()]
        if isinstance(value, enum.Enum):
            return ["enum", name, value.name]
        if isinstance(value, type) or cls is types.FunctionType:
            resolved = sys.modules.get(value.__module__)
            for part in value.__qualname__.split("."):
                resolved = getattr(resolved, part, None)
            if resolved is not value or cls is types.FunctionType and value.__closure__:
                raise ValueError("non-global callable")
            return ["class" if isinstance(value, type) else "function", value.__module__, value.__qualname__]

        ident = id(value)
        if ident in seen:
            return ["ref", seen[ident]]
        seen[ident] = len(seen)
        if cls is range:
            return [name, value.start, value.stop, value.step]
        if cls in (tuple, list):
            return [name, [pack(item) for item in value]]
        if cls in (set, frozenset):
            if len(value) > 1 and any(type(v) not in (int, bool) and
                    not isinstance(v, enum.IntEnum) for v in value):
                raise ValueError("set iteration can depend on the process hash seed")
            return [name, [pack(item) for item in value]]
        if cls is dict:
            return [name, [[pack(k), pack(v)] for k, v in value.items()]]
        if not name.startswith("sqlglot."):
            raise ValueError("unsupported optimizer result type " + name)
        attributes = dict(vars(value)) if hasattr(value, "__dict__") else {}
        attributes.update((k, getattr(value, k)) for k in slots(cls) if hasattr(value, k))
        if isinstance(value, exp.Expr) and "_hash" in attributes:
            caches.append((value, attributes["_hash"]))
            attributes["_hash"] = attributes["_hash"] is not None
        return [name, [[k, pack(v)] for k, v in sorted(attributes.items())]]

    graph = pack((result, args, kwargs))
    check_hashes(caches)
    encoded = json.dumps(graph, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
