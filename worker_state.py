"""Bounded snapshots of isolated, pinned process-pool jobs and initializer state."""
import ast
import hashlib
import inspect
import pickle
import time
import types
import zlib
from functools import lru_cache
from multiprocessing.reduction import ForkingPickler

from pool_observer import run_in_process


def fingerprint(value):
    return hashlib.sha256(ForkingPickler.dumps(value)).hexdigest()


@lru_cache(maxsize=128)
def isolated(initializer, function):
    if not all(type(f) is types.FunctionType and not f.__closure__ for f in (initializer, function)):
        return False
    if initializer.__module__ != function.__module__ or not function.__module__.startswith("tests."):
        return False
    try:
        init = ast.parse(inspect.getsource(initializer)).body[0]
        entry = ast.parse(inspect.getsource(function)).body[0]
    except (OSError, SyntaxError):
        return False
    arguments = {a.arg for a in init.args.posonlyargs + init.args.args}
    assigned = {}
    declared = set()
    for stmt in init.body:
        if isinstance(stmt, ast.Global):
            declared.update(stmt.names)
        elif (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
              isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Name)
              and stmt.value.id in arguments):
            assigned[stmt.targets[0].id] = stmt.value.id
        else:
            return False
    if not assigned or declared != assigned.keys() or set(assigned.values()) != arguments:
        return False
    if any(isinstance(n, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom))
           for n in ast.walk(entry)):
        return False
    for name, value in inspect.getclosurevars(function).globals.items():
        if name not in declared and not isinstance(value, (types.ModuleType, types.FunctionType,
                                                           type, str, bytes, int, bool, type(None))):
            return False
    return True


def save(capture, value):
    raw = ForkingPickler.dumps(value)
    if len(raw) > 16_000_000:
        raise ValueError("worker input budget")
    key = hashlib.sha256(raw).hexdigest()
    path = capture.directory / key
    if not path.exists():
        compressed = zlib.compress(raw, 1)
        if capture.size + len(compressed) > 64_000_000:
            raise ValueError("worker witness budget")
        path.write_bytes(compressed)
        capture.size += len(compressed)
    return key


def submit(capture, pool, function, /, *args, **kwargs):
    row = capture.current
    if not row or row["unsafe"] or not isolated(pool._initializer, function):
        return run_in_process(pool, function, *args, **kwargs)
    try:
        initial = save(capture, (pool._initializer, pool._initargs))
        input_key = save(capture, (function, args, kwargs))
    except Exception:
        return run_in_process(pool, function, *args, **kwargs)
    start = time.process_time()
    offset = len(row["calls"])
    capture.worker = True
    capture.worker_clean = True
    try:
        future = run_in_process(pool, function, *args, **kwargs)
    finally:
        capture.worker = False
    if capture.worker_clean and len(row["calls"]) > offset:
        try:
            job = {"init": initial, "input": input_key, "output": fingerprint(future.result()),
                   "after": fingerprint(pool._initargs), "cpu": time.process_time() - start}
            for call in row["calls"][offset:]:
                call["job"] = job
        except Exception:
            pass
    return future


def replay_job(folder, job):
    def read(key):
        return pickle.loads(zlib.decompress((folder / key).read_bytes()))
    initializer, state = read(job["init"])
    function, args, kwargs = read(job["input"])
    if not isolated(initializer, function):
        return False
    initializer(*state)
    result = function(*args, **kwargs)
    return fingerprint(result) == job["output"] and fingerprint(state) == job["after"]
