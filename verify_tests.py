"""Verify inexpensive selected tests in a fresh copy of the pinned pytest environment."""
import json
import os
import signal
import subprocess
import sys
import tempfile
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
                if original == SOURCE / changed:
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


def run_tests(ids, output):
    import pytest

    root = Path.cwd()
    sys.path.insert(0, str(root))
    results = Results()
    code = pytest.main(
        ["-q", "--tb=no", "--import-mode=importlib", "-p", "no:cacheprovider",
         "--rootdir=.", *ids], plugins=[results]
    )
    if code not in (0, 1) or None in results.bad:
        return
    for name, mod in sys.modules.items():
        if name.split(".", 1)[0] == "sqlglot" and getattr(mod, "__file__", None):
            if not Path(mod.__file__).resolve().is_relative_to(root):
                return
    proved = [key for key in ids if key not in results.bad and
              results.phases.get(key) == {"setup", "call", "teardown"}]
    output.write_text(json.dumps(proved))


def verify():
    path, source, *nodes = sys.stdin.buffer.read().split(b"\0")
    changed = Path(os.fsdecode(path))
    if (changed.is_absolute() or ".." in changed.parts or changed.parts[:1] != ("sqlglot",)
            or changed.suffix != ".py" or not (SOURCE / changed).is_file()):
        return
    ids = [node.decode() for node in nodes]
    costs = json.loads(Path("/build/cpu.json").read_text())
    if not ids or sum(costs.get(node, float("inf")) for node in ids) > 0.85:
        return
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "source"
        root.mkdir()
        source_overlay(root, changed, source)
        if (root / changed).read_bytes() != source:
            return
        output = base / "passed.json"
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        child = subprocess.Popen(
            [sys.executable, __file__, str(output), *ids], cwd=root, env=env,
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            status = child.wait(timeout=1.9)
        except subprocess.TimeoutExpired:
            status = None
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        if status == 0 and output.exists():
            for node in json.loads(output.read_text()):
                if node in ids:
                    print(node)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        verify()
    else:
        run_tests(sys.argv[2:], Path(sys.argv[1]))
