"""Measure pinned, uninstrumented pytest CPU demand for optional selective verification."""
import json
import os
import resource
import sys
from pathlib import Path

import pytest


def cpu():
    totals = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    return sum(r.ru_utime + r.ru_stime for r in totals)


class Calibration:
    def __init__(self):
        self.results = {}
        self.failed = set()
        self.setup = {}

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        start = cpu()
        try:
            return (yield)
        finally:
            self.results[item.nodeid] = round(cpu()-start, 6)

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)
        if report.when == "setup":
            group = report.nodeid.rpartition("::")[0]
            self.setup[group] = max(self.setup.get(group, 0), report.duration)


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    plugin = Calibration()
    status = pytest.main(["-q","--tb=no","--timeout=90","-p","no:cacheprovider","tests"],plugins=[plugin])
    if status != 0 or len(plugin.results) < 1500:
        raise SystemExit("incomplete calibration")
    Path(sys.argv[1]).write_text(json.dumps({
        k: v + plugin.setup.get(k.rpartition("::")[0], 0)
        for k, v in plugin.results.items() if k not in plugin.failed
    }))
