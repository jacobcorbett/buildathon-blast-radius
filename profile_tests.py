"""Record pinned-suite line coverage and dialect use, keyed by pytest node ID."""

import ast
import os
import sys
from dataclasses import dataclass, field
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor
from functools import lru_cache
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path.cwd()
TOOL = sys.monitoring.COVERAGE_ID
EVENTS = sys.monitoring.events
DISABLE = sys.monitoring.DISABLE
PREFIX = str(ROOT / "sqlglot") + "/"


@dataclass
class Observation:
    hits: dict = field(default_factory=lambda: defaultdict(set))
    dialects: set = field(default_factory=set)
    reads: set = field(default_factory=set)
    source_reads: set = field(default_factory=set)
    external: bool = False

    def merge(self, other):
        for path, lines in other.hits.items():
            self.hits[path].update(lines)
        self.dialects.update(other.dialects)
        self.reads.update(other.reads)
        self.source_reads.update(other.source_reads)
        self.external |= other.external


class Profile:
    def __init__(self):
        self.observation = Observation()
        self.tests = {}
        self.failed = set()
        self.fixtures = {}
        self.active = False
        self.collection_errors = []

    @lru_cache(maxsize=None)
    def path(self, name):
        if name.startswith(PREFIX):
            return name.removeprefix(str(ROOT) + "/")
        return None

    def line(self, code, line):
        path = self.path(code.co_filename)
        if path:
            self.observation.hits[path].add(line)
        return DISABLE

    def dialect(self, code, offset):
        instance = sys._getframe(1).f_locals["self"]
        self.observation.dialects.update(
            cls.__module__ for cls in type(instance).__mro__
            if cls.__module__.startswith("sqlglot.dialects.")
        )
        # Different dialects share the same constructor in a single test.

    def audit(self, event, args):
        if not self.active:
            return
        if event in {"subprocess.Popen", "os.fork", "os.posix_spawn"}:
            self.observation.external = True
        if event == "open" and isinstance(args[0], str):
            path = args[0].removeprefix(str(ROOT) + "/")
            if not path.startswith(("/", "<")) and not path.endswith((".pyc", ".so")):
                path = os.path.normpath(path)
                self.observation.reads.add(path)
                if path.startswith("sqlglot/") and path.endswith(".py"):
                    caller = sys._getframe(1).f_globals.get("__name__", "")
                    if caller.startswith(("tests.", "pathlib", "inspect", "linecache")):
                        self.observation.source_reads.add(path)

    def pytest_sessionstart(self):
        self.active = True
        sys.addaudithook(self.audit)
        sys.monitoring.use_tool_id(TOOL, "blast-radius")
        sys.monitoring.register_callback(TOOL, EVENTS.LINE, self.line)
        sys.monitoring.set_events(TOOL, EVENTS.LINE)

    def pytest_collection_finish(self, session):
        self.collection = self.observation
        from sqlglot.dialects.dialect import Dialect

        sys.monitoring.register_callback(TOOL, EVENTS.PY_START, self.dialect)
        sys.monitoring.set_local_events(TOOL, Dialect.__init__.__code__, EVENTS.PY_START)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope == "function":
            return (yield)
        parent = self.observation
        self.observation = Observation()
        sys.monitoring.restart_events()
        try:
            return (yield)
        finally:
            self.fixtures[fixturedef] = self.observation
            parent.merge(self.observation)
            self.observation = parent
            sys.monitoring.restart_events()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.observation = Observation()
        sys.monitoring.restart_events()
        result = yield
        self.tests[item.nodeid] = self.observation
        return result

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        if item._request:
            for fixture in item._request._fixture_defs.values():
                if fixture.scope != "function" and fixture in self.fixtures:
                    self.observation.merge(self.fixtures[fixture])

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)


def roundtrip(value):
    return ForkingPickler.loads(ForkingPickler.dumps(value))


def run_in_process(pool, function, /, *args, **kwargs):
    """Measure the Python SQL work normally submitted to a process pool."""
    future = Future()
    try:
        if pool._initializer and not getattr(pool, "_profile_initialised", False):
            pool._initializer(*pool._initargs)
            pool._profile_initialised = True
        # A real process pool serializes both sides, exercising e.g. Expr reducers.
        function, args, kwargs = roundtrip((function, args, kwargs))
        future.set_result(roundtrip(function(*args, **kwargs)))
    except Exception as error:
        future.set_exception(error)
    return future


def main():
    destination = Path(sys.argv[1])
    profile = Profile()
    with patch.object(ProcessPoolExecutor, "submit", run_in_process):
        pytest.main(
            ["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
             "--timeout=90", "tests"],
            plugins=[profile],
        )
    if len(profile.tests) < 1500 or profile.collection_errors:
        raise SystemExit(f"incomplete profile: {len(profile.tests)}, {profile.collection_errors}")
    profile.active = False
    write_profile(profile, destination)


def dependencies(path):
    tree = ast.parse(path.read_bytes(), filename=str(path))
    modules = set()
    package = path.parent.parts
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        pending.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package[: len(package) + 1 - node.level] if node.level else ()
            module = ".".join((*parts, *(node.module or "").split("."))).strip(".")
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names)
    return tree, modules


def scopes(node):
    """Module/class function bodies. A nested function is covered by its outer body."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.body[0].lineno > child.lineno:
                yield child.body[0].lineno, child.end_lineno, child.body[0].col_offset
        elif isinstance(child, ast.ClassDef):
            yield from scopes(child)


def write_profile(profile, destination):
    sources = sorted((*Path("sqlglot").rglob("*.py"), *Path("tests").rglob("*.py")))
    modules = {}
    trees = {}
    for path in sources:
        module = ".".join(path.with_suffix("").parts).removesuffix(".__init__")
        tree, imports = dependencies(path)
        modules[module] = (str(path), imports)
        trees[str(path)] = tree

    @lru_cache(maxsize=None)
    def reachable(origin):
        pending = [origin]
        seen = set()
        files = set()
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            if "." in module:
                pending.append(module.rsplit(".", 1)[0])
            if module in modules:
                path, imports = modules[module]
                files.add(path)
                pending.extend(imports - seen)
        return files

    test_ids = sorted(profile.tests)
    line_hits = defaultdict(lambda: defaultdict(int))
    usage = defaultdict(int)
    source_readers = defaultdict(int)
    always = 0
    for index, test_id in enumerate(test_ids):
        bit = 1 << index
        observation = profile.tests[test_id]
        if observation.external or (test_id in profile.failed and not observation.hits):
            always |= bit
        origin = test_id.split("::", 1)[0].removesuffix(".py").replace("/", ".")
        for path in reachable(origin):
            usage[path] |= bit
        for dialect in observation.dialects:
            for path in reachable(dialect):
                usage[path] |= bit
        for path in observation.source_reads:
            source_readers[path] |= bit
        for path in observation.reads:
            usage[path] |= bit
        for path, lines in observation.hits.items():
            usage[path] |= bit
            for line in lines:
                line_hits[path][line] |= bit

    # Collection-time reads may belong to fixtures shared by several test files.
    all_tests = (1 << len(test_ids)) - 1
    for path in profile.collection.source_reads:
        source_readers[path] |= all_tests
    for path in profile.collection.reads:
        if not path.endswith(".py") and Path(path).is_file():
            usage[path] |= all_tests

    with destination.open("w", encoding="utf-8") as out:
        for test_id in test_ids:
            print("T", test_id, sep="\t", file=out)
        print("U", f"{always:x}", sep="\t", file=out)
        for path in sorted(trees.keys() | usage.keys()):
            print("F", path, f"{usage[path]:x}", sep="\t", file=out)
            if path not in trees or not path.startswith("sqlglot/"):
                continue
            lines = line_hits[path]
            if source_readers[path]:
                print("R", f"{source_readers[path]:x}", sep="\t", file=out)
            for first, last, indent in sorted(scopes(trees[path])):
                tests = 0
                for line in range(first, last + 1):
                    tests |= lines.get(line, 0)
                print("S", first, last, indent, f"{tests:x}", sep="\t", file=out)
            for line, tests in sorted(lines.items()):
                print("L", line, f"{tests:x}", sep="\t", file=out)


if __name__ == "__main__":
    main()
