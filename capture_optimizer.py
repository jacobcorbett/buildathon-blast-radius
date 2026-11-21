"""Capture validated optimizer call boundaries on expensive, passing pinned tests."""
import hashlib
import json
import os
import sys
import time
import zlib
from types import FunctionType
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from optimizer_state import decode, digest, encode
from worker_state import replay_job, submit

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from sqlglot.optimizer.optimizer import optimize


class Capture:
    def __init__(self, directory):
        self.directory = directory
        self.current = None
        self.inside = False
        self.tests = {}
        self.seen = set()
        self.failed = set()
        self.size = 0
        self.prefix = str(ROOT) + "/"
        self.shared = set()
        self.first_test = True
        self.checking = False
        self.worker = False
        self.worker_clean = True

    def started(self, code, offset):
        if code.co_filename.startswith(self.prefix + "sqlglot/"):
            self.seen.add(code.co_filename[len(self.prefix):] + "::" + code.co_qualname)
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.current is None or self.checking:
            return
        if event == "object.__getattr__" and not self.inside and isinstance(args[0], FunctionType):
            self.checking = True
            try:
                code = args[0].__code__
                if code.co_filename.startswith(self.prefix):
                    self.seen.add(code.co_filename[len(self.prefix):] + "::" + code.co_qualname)
            finally:
                self.checking = False
        if event in {"os.fork", "subprocess.Popen", "os.posix_spawn", "os.system",
                     "os.exec", "socket.connect", "sys.settrace", "sys.setprofile"}:
            self.current["unsafe"] = True
        if self.inside and event == "open" and len(args) > 2 and args[2] & (os.O_WRONLY | os.O_RDWR):
            self.current["unsafe"] = True
        if (self.worker and event == "open" and len(args) > 2 and args[2] & (os.O_WRONLY | os.O_RDWR)
                and not (isinstance(args[0], str) and args[0].startswith(str(self.directory) + "/"))):
            self.worker_clean = False
        if event == "open" and not self.inside and isinstance(args[0], str):
            path = os.path.abspath(args[0])
            if path.startswith(self.prefix + "sqlglot/") and path.endswith(".py"):
                caller = sys._getframe(1).f_globals.get("__name__", "")
                if caller not in {"importlib._bootstrap_external", "_frozen_importlib_external"}:
                    self.current["files"].add(path[len(self.prefix):])

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        if self.first_test:
            self.shared.update(self.seen)
            self.first_test = False
        row = {"outside": [], "files": set(), "calls": [], "unsafe": False}
        self.current = row
        self.seen = set()
        sys.monitoring.restart_events()
        try:
            return (yield)
        finally:
            row["outside"] = sorted(self.seen)
            row["files"] = sorted(row["files"])
            self.tests[item.nodeid] = row
            self.current = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope == "function":
            return (yield)
        previous = self.seen
        self.seen = set()
        sys.monitoring.restart_events()
        count = len(self.current["calls"]) if self.current else 0
        try:
            return (yield)
        finally:
            self.shared.update(self.seen)
            if self.current:
                for record in self.current["calls"][count:]:
                    self.shared.update(record["seen"])
            self.seen = previous
            sys.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)

    def call(self, *args, **kwargs):
        row = self.current
        if row is None or row["unsafe"]:
            return optimize(*args, **kwargs)
        if self.inside:
            row["unsafe"] = True
            return optimize(*args, **kwargs)
        try:
            data = encode(args, kwargs)
            before = digest(None, args, kwargs)
        except Exception:
            row["unsafe"] = True
            return optimize(*args, **kwargs)

        outer = self.seen
        self.seen = set()
        self.inside = True
        sys.monitoring.restart_events()
        start = time.process_time()
        try:
            result = optimize(*args, **kwargs)
        except BaseException:
            row["unsafe"] = True
            raise
        finally:
            elapsed = time.process_time() - start
            observed = self.seen
            self.seen = outer
            self.inside = False
            sys.monitoring.restart_events()

        try:
            key = hashlib.sha256(data).hexdigest()
            if len(data) > 4_000_000:
                raise ValueError("optimizer input is too large")
            path = self.directory / key
            if not path.exists():
                packed = zlib.compress(data, 1)
                if self.size + len(packed) > 68_000_000:
                    raise ValueError("optimizer witness budget")
                path.write_bytes(packed)
                self.size += len(packed)
            row["calls"].append({"data": key, "before": before, "expected": digest(result, args, kwargs),
                                 "seen": sorted(observed), "cpu": elapsed})
        except Exception:
            row["unsafe"] = True
        return result


def main():
    folder = Path(sys.argv[1])
    if sys.argv[2] == "--validate":
        rows = json.loads((folder / "index.json").read_text())
        jobs = {}
        for key, row in list(rows.items()):
            try:
                for call in row["calls"]:
                    args, kwargs = decode(zlib.decompress((folder / call["data"]).read_bytes()))
                    if digest(None, args, kwargs) != call["before"]:
                        raise ValueError("input state differs")
                    result = optimize(*args, **kwargs)
                    if digest(result, args, kwargs) != call["expected"]:
                        raise ValueError("result or argument state differs")
                    if job := call.get("job"):
                        identity = tuple(job[k] for k in ("init", "input", "output", "after"))
                        if identity not in jobs:
                            try:
                                jobs[identity] = replay_job(folder, job)
                            except Exception:
                                jobs[identity] = False
                        if not jobs[identity]:
                            del call["job"]
            except Exception:
                del rows[key]
        (folder / "index.json").write_text(json.dumps(rows))
        print(f"Validated {len(rows)} optimizer witness sets in a fresh interpreter")
        print(f"Validated {sum(jobs.values())} of {len(jobs)} isolated worker results and post-states")
        return
    folder.mkdir()
    costs = json.loads(Path(sys.argv[2]).read_text())
    ids = [key for key, value in costs.items() if value > 3]
    capture = Capture(folder)
    for module in tuple(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("sqlglot"):
            for name, fn in list(vars(module).items()):
                if fn is optimize:
                    setattr(module, name, capture.call)

    sys.addaudithook(capture.audit)
    tool = sys.monitoring.COVERAGE_ID
    sys.monitoring.use_tool_id(tool, "optimizer call boundaries")
    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, capture.started)
    sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
    def submit_job(pool, function, /, *args, **kwargs):
        return submit(capture, pool, function, *args, **kwargs)
    with patch.object(ProcessPoolExecutor, "submit", submit_job):
        status = pytest.main(["-q", "--tb=no", "-p", "no:cacheprovider", "--timeout=90", *ids],
                             plugins=[capture]) if ids else 0
    sys.monitoring.set_events(tool, 0)
    rows = {k: v for k,v in capture.tests.items() if status == 0 and k not in capture.failed
            and not v["unsafe"] and v["calls"]}
    for row in rows.values():
        row["outside"] = sorted(set(row["outside"]) | capture.shared)
    (folder / "index.json").write_text(json.dumps(rows))


if __name__ == "__main__":
    main()
