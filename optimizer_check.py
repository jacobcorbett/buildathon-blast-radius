"""Replay a validated optimizer boundary only when it contains every observed changed call."""
import ast
import json
import os
import sys
import zlib
from pathlib import Path

from optimizer_state import decode, digest

WITNESSES = Path("/app/optimizer-calls")


def changed_function(path, original, updated):
    if not path.startswith("sqlglot/optimizer/"):
        return None
    changes = []

    def compare(a, b, scope="", owner=None):
        if type(a) is not type(b):
            if owner and isinstance(a, (ast.boolop, ast.cmpop)) and isinstance(b, type(a).__bases__[0]):
                changes.append(owner)
                return
            raise ValueError
        if isinstance(a, ast.Constant) and a.value != b.value:
            if (not owner or type(a.value) not in (int, str, bool)
                    or type(a.value) is not type(b.value)):
                raise ValueError
            changes.append(owner)
            return
        if isinstance(a, (ast.ClassDef, ast.FunctionDef)):
            scope += a.name + "."
            owner = scope[:-1] if isinstance(a, ast.FunctionDef) else None
        if isinstance(a, ast.AST):
            for field in a._fields:
                # Only body values and existing scalar defaults are eligible.
                current = None if field in ("decorator_list", "returns", "annotation") else owner
                compare(getattr(a, field), getattr(b, field), scope, current)
        elif isinstance(a, list) and len(a) == len(b):
            for x, y in zip(a, b):
                compare(x, y, scope, owner)
        elif a != b:
            raise ValueError

    try:
        compare(ast.parse(original), ast.parse(updated))
    except (SyntaxError, ValueError, RecursionError):
        return None
    return path + "::" + changes[0] if len(changes) == 1 else None


def replay(rows, ids, path, original, updated):
    func = changed_function(path, original, updated)
    if not func:
        return []
    from sqlglot.optimizer.optimizer import optimize

    if not Path(sys.modules[optimize.__module__].__file__).resolve().is_relative_to(Path.cwd()):
        return []
    observable = False
    checking = False

    def audit(event, args):
        nonlocal observable
        if checking and (event in {"os.system", "os.fork", "os.exec", "os.posix_spawn", "socket.connect",
                                   "subprocess.Popen", "os.rename", "os.remove", "sys.settrace", "sys.setprofile"}
                or event == "open" and len(args) > 2 and args[2] & (os.O_WRONLY | os.O_RDWR)):
            observable = True

    sys.addaudithook(audit)
    passed = []
    for test in ids:
        row = rows.get(test)
        if not row or func in row["outside"] or path in row["files"]:
            continue
        calls = [c for c in row["calls"] if func in c["seen"]]
        if not calls or sum(call["cpu"] for call in calls) > 4:
            continue
        for call in calls:
            args, kwargs = decode(zlib.decompress((WITNESSES / call["data"]).read_bytes()))
            if digest(None, args, kwargs) != call["before"]:
                break
            checking = True
            try:
                result = optimize(*args, **kwargs)
            finally:
                checking = False
            if observable or digest(result, args, kwargs) != call["expected"]:
                break
        else:
            passed.append(test)
    return passed
