"""Verify inexpensive selected tests in a fresh copy of the pinned pytest environment."""
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

SOURCE = Path("/upstream/source")


def source_overlay(root, changed, updated):
    # Keep Python modules and tests at real paths under a single import root.
    # Fixture directories are read-only. Checked-hash pycs validate the new bytes.
    for path in SOURCE.iterdir():
        dest = root / path.name
        if path.name not in ("tests", "sqlglot"):
            dest.symlink_to(path)
            continue
        for folder, dirs, files in os.walk(path):
            folder = Path(folder)
            target = root / folder.relative_to(SOURCE)
            target.mkdir()
            for name in dirs.copy():
                if name in {"fixtures", "__pycache__"}:
                    (target / name).symlink_to(folder / name)
                    dirs.remove(name)
            for name in files:
                original = folder / name
                if changed is not None and original == SOURCE / changed:
                    (target / name).write_bytes(updated)
                elif name.endswith(".py"):
                    (target / name).write_bytes(original.read_bytes())
                else:
                    (target / name).symlink_to(original)


class Results:
    def __init__(self):
        self.phases = {}
        self.bad = set()

    def pytest_runtest_logreport(self, report):
        if not report.passed or hasattr(report, "wasxfail"):
            self.bad.add(report.nodeid)
        if report.passed:
            self.phases.setdefault(report.nodeid, set()).add(report.when)

    def pytest_collectreport(self, report):
        if report.failed:
            self.bad.add(None)


class Session(Results):
    """Fork before collection so every request runs normal pytest with fresh package state."""
    def __init__(self, base, requests, responses):
        super().__init__()
        self.base = base
        self.root = base / "source"
        self.requests = requests
        self.responses = responses
        self.costs = json.loads(Path("/build/cpu.json").read_text())
        self.child = False
        self.ids = []
        self.collection = False
        self.complete = False

    def pytest_sessionstart(self, session):
        import pytest

        while header := self.requests.readline():
            length = int(header)
            if length > 12_000_000:
                break
            payload = self.requests.read(length)
            if len(payload) != length:
                break
            reply = b"?\n"
            try:
                mode, path, source, *nodes = payload.split(b"\0")
                collection = mode == b"C"
                changed = Path(os.fsdecode(path))
                ids = [node.decode() for node in nodes]
                cheap = ids and sum(self.costs.get(node, float("inf")) for node in ids) <= 0.85
                if (not changed.is_absolute() and ".." not in changed.parts
                        and changed.parts and changed.parts[0] in {"sqlglot", "tests"}
                        and changed.suffix == ".py" and (SOURCE / changed).is_file()
                        and (cheap or collection)):
                    os.chdir(self.base)
                    shutil.rmtree(self.root)
                    self.root.mkdir()
                    source_overlay(self.root, changed, source)
                    os.chdir(self.root)
                    if (self.root / changed).read_bytes() == source:
                        pid = os.fork()
                        if pid == 0:
                            os.setsid()
                            self.child = True
                            self.collection = collection
                            self.ids = ids if cheap else []
                            session.config.args = ["tests"] if collection else ids
                            session.config.option.collectonly = not self.ids
                            return
                        passed = self.wait(pid, 4.0 if collection else 1.9)
                        if passed is not None:
                            reply = ("OK " + " ".join(str(i) for i, node in enumerate(ids)
                                                       if node in passed) + "\n").encode()
            except (OSError, ValueError):
                if self.child:
                    os._exit(1)
            self.responses.write(reply)
            self.responses.flush()
        pytest.exit("batch complete", returncode=0)

    def wait(self, pid, timeout):
        deadline = time.monotonic() + timeout
        status = None
        while time.monotonic() < deadline:
            finished, value = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = value
                break
            time.sleep(0.01)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        output = self.base / f"passed-{pid}"
        if status == 0 and output.exists():
            passed = json.loads(output.read_text())
            output.unlink()
            return passed
        return None

    def pytest_collection_modifyitems(self, items):
        if self.collection:
            self.complete = self.costs.keys() <= {item.nodeid for item in items}
            if self.ids:
                wanted = set(self.ids)
                items[:] = [item for item in items if item.nodeid in wanted]

    def pytest_sessionfinish(self, session, exitstatus):
        if (not self.child or exitstatus not in (0, 1) or None in self.bad
                or self.collection and not self.complete):
            return
        for name, mod in sys.modules.items():
            if name.split(".", 1)[0] == "sqlglot" and getattr(mod, "__file__", None):
                if not Path(mod.__file__).resolve().is_relative_to(self.root):
                    return
        passed = [key for key in self.ids if key not in self.bad
                  and self.phases.get(key) == {"setup", "call", "teardown"}]
        (self.base / f"passed-{os.getpid()}").write_text(json.dumps(passed))


def serve():
    requests = os.fdopen(os.dup(0), "rb")
    responses = os.fdopen(os.dup(1), "wb")
    with open(os.devnull, "wb") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    import pytest

    with tempfile.TemporaryDirectory() as directory:
        session = Session(Path(directory), requests, responses)
        session.root.mkdir()
        source_overlay(session.root, None, None)
        os.chdir(session.root)
        sys.path.insert(0, str(session.root))
        os.environ["PYTHONPATH"] = str(session.root)
        code = pytest.main(["-q", "--tb=no", "--import-mode=importlib", "-p",
                            "no:cacheprovider", "--rootdir=.", "tests"], plugins=[session])
        if session.child:
            os._exit(int(code not in (0, 1)))
        os.chdir("/app")


if __name__ == "__main__":
    serve()
