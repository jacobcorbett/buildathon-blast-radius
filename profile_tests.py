"""Record pinned-suite line coverage and dialect use, keyed by pytest node ID."""

import ast
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from operand_profile import OperandProfile

ROOT = Path.cwd()
TOOL = sys.monitoring.COVERAGE_ID
EVENTS = sys.monitoring.events
DISABLE = sys.monitoring.DISABLE
PREFIXES = tuple(str(ROOT / package) + "/" for package in ("sqlglot", "tests"))


@dataclass
class Observation:
    settings: set = field(default_factory=set)
    hits: dict = field(default_factory=lambda: defaultdict(set))
    dialects: set = field(default_factory=set)
    reads: set = field(default_factory=set)
    source_reads: set = field(default_factory=set)
    expression_types: set = field(default_factory=set)
    external: bool = False
    subprocesses: int = 0
    child_paths: set = field(default_factory=set)

    def merge(self, other):
        self.settings.update(other.settings)
        for path, lines in other.hits.items():
            self.hits[path].update(lines)
        self.dialects.update(other.dialects)
        self.reads.update(other.reads)
        self.source_reads.update(other.source_reads)
        self.expression_types.update(other.expression_types)
        self.external |= other.external or other.subprocesses != 0
        self.child_paths.update(other.child_paths)


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
        if name.startswith(PREFIXES):
            return name.removeprefix(str(ROOT) + "/")
        return None

    def line(self, code, line):
        path = self.path(code.co_filename)
        if path:
            self.observation.hits[path].add(line)
        return DISABLE

    @lru_cache(maxsize=None)
    def ancestry(self, cls, package):
        return tuple(base for base in cls.__mro__ if base.__module__.startswith(package))

    def object_started(self, code, offset):
        cls = type(sys._getframe(1).f_locals["self"])
        if code is self.dialect_code:
            self.observation.dialects.update(
                base.__module__ for base in self.ancestry(cls, "sqlglot.dialects.")
            )
        else:
            self.observation.expression_types.add(cls)
        # Shared constructors run for several types in a single test.

    def audit(self, event, args):
        if not self.active:
            return
        if event == "subprocess.Popen":
            self.observation.subprocesses += 1
        if event in {"os.fork", "os.posix_spawn", "os.exec"}:
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
        from sqlglot.expressions.core import Expr

        self.dialect_code = Dialect.__init__.__code__
        sys.monitoring.register_callback(TOOL, EVENTS.PY_START, self.object_started)
        for code in (self.dialect_code, Expr.__init__.__code__):
            sys.monitoring.set_local_events(TOOL, code, EVENTS.PY_START)

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
        with TemporaryDirectory() as directory:
            environment = {
                "BLAST_PROFILE_ROOT": str(ROOT),
                "BLAST_PROFILE_CHILD": directory,
                "PYTHONPATH": "/build:.",
            }
            with patch.dict(os.environ, environment):
                result = yield
            self.merge_children(Path(directory))
        self.tests[item.nodeid] = self.observation
        return result

    def merge_children(self, directory):
        observation = self.observation
        reports = list(directory.iterdir())
        if len(reports) != observation.subprocesses:
            observation.external = True
            return
        for report in reports:
            try:
                complete, hits, reads = json.loads(report.read_text())
            except (OSError, ValueError):
                observation.external = True
                continue
            observation.external |= not complete
            for path, lines in hits.items():
                observation.hits[path].update(lines)
            observation.reads.update(reads)
            observation.child_paths.update(hits.keys() | set(reads))

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
    operands = OperandProfile(ROOT, profile)
    sys.meta_path.insert(0, operands)
    with patch.object(ProcessPoolExecutor, "submit", run_in_process):
        status = pytest.main(
            ["-q", "--tb=no", "-p", "no:cacheprovider", "--continue-on-collection-errors",
             "--timeout=90", "tests"],
            plugins=[profile],
        )
    if len(profile.tests) < 1500 or profile.collection_errors:
        raise SystemExit(f"incomplete profile: {len(profile.tests)}, {profile.collection_errors}")
    sys.meta_path.remove(operands)
    operands.enabled = False
    profile.active = False
    # Only use operand invariants when the instrumented suite itself passed.
    invariants = operands.invariants() if status == pytest.ExitCode.OK else ()
    eager = {operands.settings[i] for i in operands.eager_settings | profile.collection.settings}
    settings = {} if status != pytest.ExitCode.OK else {
        site: location for site, location in enumerate(operands.settings) if location not in eager
    }
    write_profile(profile, destination, invariants, settings)


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
                first, last = child.body[0].lineno, child.end_lineno
                yield first, last, child.body[0].col_offset, first, last
        elif isinstance(child, ast.ClassDef):
            yield from scopes(child)


def narrow_tokens(tree):
    """Pair isolated numeric/boolean tokens with the code that evaluates them."""
    def tokens(expression):
        pending = [expression]
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.Lambda, ast.GeneratorExp)):
                continue
            if isinstance(node, ast.Constant) and type(node.value) in (int, bool):
                yield node
            pending.extend(ast.iter_child_nodes(node))

    def statements(function):
        pending = list(function.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.If, ast.While)):
                expression = node.test
            elif isinstance(node, (ast.Return, ast.Expr, ast.Assign, ast.AnnAssign)):
                expression = node.value
            else:
                expression = None
            if expression is not None:
                first = node.lineno
                last = expression.end_lineno
                for token in tokens(expression):
                    yield token, first, last
            # Nested statement suites have independent coverage.
            pending.extend(child for child in ast.iter_child_nodes(node)
                           if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)))

    def declarations(scope):
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from statements(child)
            elif isinstance(child, ast.ClassDef):
                yield from declarations(child)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                # A lambda body is executable code even in a class dispatch table.
                # Only exact replacement of a literal token is eligible.
                for node in ast.walk(child):
                    if isinstance(node, ast.Lambda):
                        for token in tokens(node.body):
                            if token.lineno > node.lineno:
                                yield token, node.body.lineno, node.body.end_lineno

    yield from declarations(tree)


def expression_flags(trees, line_hits, type_usage):
    """Locate boolean values in ordinary expression classes' argument declarations."""
    flags = defaultdict(list)
    referenced = defaultdict(int)
    indirect = set()
    for path, tree in trees.items():
        if not path.startswith(("sqlglot/", "tests/")):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced[node.id] |= line_hits[path].get(node.lineno, 0)
            elif isinstance(node, ast.Attribute):
                referenced[node.attr] |= line_hits[path].get(node.lineno, 0)
                # Some modules read another class's metadata while initialising
                # registries. Such declarations keep the shared-source fallback.
                if node.attr in {"arg_types", "required_args"}:
                    if isinstance(node.value, ast.Name):
                        indirect.add(node.value.id)
                    elif isinstance(node.value, ast.Attribute):
                        indirect.add(node.value.attr)

    for path, tree in trees.items():
        if not path.startswith("sqlglot/expressions/") or path.endswith("/core.py"):
            continue
        for cls in tree.body:
            if not (
                isinstance(cls, ast.ClassDef)
                and not cls.decorator_list
                and not cls.keywords
                and cls.name not in indirect
                and len(cls.bases) == 1
                and isinstance(cls.bases[0], ast.Name)
                and cls.bases[0].id == "Expression"
            ):
                continue
            class_usage = type_usage.get((path, cls.name), 0) | referenced[cls.name]
            for assignment in cls.body:
                if not (
                    isinstance(assignment, ast.Assign)
                    and len(assignment.targets) == 1
                    and isinstance(assignment.targets[0], ast.Name)
                    and assignment.targets[0].id == "arg_types"
                    and isinstance(assignment.value, ast.Dict)
                    and all(isinstance(key, ast.Constant) and isinstance(key.value, str)
                            for key in assignment.value.keys)
                    and all(isinstance(value, ast.Constant) and type(value.value) is bool
                            for value in assignment.value.values)
                ):
                    continue
                for value in assignment.value.values:
                    flags[path].append((value.lineno, value.col_offset, value.end_col_offset, class_usage))
    return flags


def condition_lines(tree):
    """Only whole, single-line tests evaluated on entry to a conditional."""
    def visit(node, in_function):
        if isinstance(node, ast.ClassDef):
            in_function = False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        elif in_function and isinstance(node, (ast.If, ast.While)):
            if node.lineno == node.test.end_lineno:
                yield node.lineno
        for child in ast.iter_child_nodes(node):
            yield from visit(child, in_function)
    yield from visit(tree, False)


def indirect_settings(trees):
    """Exclude names used by reflective lookups or by cached/constructor code."""
    names = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isupper():
                names.add(node.value)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in {"__init__", "__new__"}
                or any("cache" in ast.unparse(d) for d in node.decorator_list)
            ):
                names.update(child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute))
    return names


def write_profile(profile, destination, invariants, settings):
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
    type_usage = defaultdict(int)
    child_usage = defaultdict(int)
    setting_hits = defaultdict(int)
    always = 0
    for index, test_id in enumerate(test_ids):
        bit = 1 << index
        observation = profile.tests[test_id]
        for site in observation.settings:
            setting_hits[site] |= bit
        if observation.external or test_id in profile.failed:
            always |= bit
        origin = test_id.split("::", 1)[0].removesuffix(".py").replace("/", ".")
        for path in reachable(origin):
            usage[path] |= bit
        for dialect in observation.dialects:
            for path in reachable(dialect):
                usage[path] |= bit
        for cls in observation.expression_types:
            for base in profile.ancestry(cls, "sqlglot.expressions."):
                path = base.__module__.replace(".", "/") + ".py"
                type_usage[path, base.__qualname__] |= bit
        for path in observation.child_paths:
            child_usage[path] |= bit
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

    flags = expression_flags(trees, line_hits, type_usage)
    introspection = 0
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if ((isinstance(node, (ast.Name, ast.Attribute)) and
                 getattr(node, "attr", getattr(node, "id", "")) in {
                     "__doc__", "getdoc", "doctest", "getsource", "getsourcefile", "help",
                     "format_exc", "format_exception", "extract_tb", "getcomments"})
                or isinstance(node, ast.Constant) and node.value == "__doc__"):
                introspection |= line_hits[path].get(node.lineno, 0)
    setting_records = defaultdict(lambda: defaultdict(int))
    indirect = indirect_settings(trees)
    for site, (path, line, start, end, name) in settings.items():
        if name not in indirect:
            setting_records[path][line, start, end] |= setting_hits[site]
    operands = defaultdict(list)
    for path, line, start, end, domains in invariants:
        operands[path].append((line, start, end, domains))
    with destination.open("w", encoding="utf-8") as out:
        for test_id in test_ids:
            print("T", test_id, sep="\t", file=out)
        print("U", f"{always:x}", sep="\t", file=out)
        print("I", f"{introspection:x}", sep="\t", file=out)
        for path in sorted(trees.keys() | usage.keys()):
            print("F", path, f"{usage[path]:x}", sep="\t", file=out)
            if path not in trees:
                continue
            lines = line_hits[path]
            if source_readers[path]:
                print("R", f"{source_readers[path]:x}", sep="\t", file=out)
            if child_usage[path]:
                print("C", f"{child_usage[path]:x}", sep="\t", file=out)
            for first, last, indent, coverage_start, coverage_end in sorted(scopes(trees[path])):
                tests = 0
                for line in range(coverage_start, coverage_end + 1):
                    tests |= lines.get(line, 0)
                print("S", first, last, indent, f"{tests:x}", sep="\t", file=out)
            if path.startswith("sqlglot/"):
                for (line, start, end), tests in sorted(setting_records[path].items()):
                    print("N", line, start, end, f"{tests:x}", sep="\t", file=out)
                for line in sorted(set(condition_lines(trees[path]))):
                    if line not in profile.collection.hits.get(path, ()):
                        print("B", line, f"{lines.get(line, 0):x}", sep="\t", file=out)
                for token, first, last in narrow_tokens(trees[path]):
                    tests = 0
                    for line in range(first, last + 1):
                        tests |= lines.get(line, 0)
                    if tests and not any(line in profile.collection.hits.get(path, ())
                               for line in range(first, last + 1)):
                        print("N", token.lineno, token.col_offset, token.end_col_offset,
                              f"{tests:x}", sep="\t", file=out)
            for line, start, end, domains in sorted(operands[path]):
                print("Q", line, start, end, domains, sep="\t", file=out)
            for line, start, end, tests in flags[path]:
                print("A", line, start, end, f"{tests:x}", sep="\t", file=out)


if __name__ == "__main__":
    main()
