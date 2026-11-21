"""Capture fresh Python subprocess dependencies during the pinned suite build."""

import os

if root := os.environ.get("BLAST_PROFILE_ROOT"):
    import atexit
    import json
    import sys
    from collections import defaultdict

    directory = os.environ["BLAST_PROFILE_CHILD"]
    hits = defaultdict(set)
    reads = set()
    complete = True
    prefix = root + "/"
    tool = sys.monitoring.COVERAGE_ID

    def line(code, number):
        name = code.co_filename
        if name.startswith(prefix):
            hits[name[len(prefix):]].add(number)
        return sys.monitoring.DISABLE

    def audit(event, args):
        global complete
        if event in {"subprocess.Popen", "os.fork", "os.posix_spawn", "os.exec"}:
            complete = False
        if event == "open" and isinstance(args[0], str):
            path = os.path.abspath(args[0])
            if path.startswith(prefix):
                reads.add(path[len(prefix):])

    @atexit.register
    def report():
        path = os.path.join(directory, str(os.getpid()))
        with open(path, "w", encoding="utf-8") as output:
            json.dump([complete, {path: sorted(lines) for path, lines in hits.items()}, sorted(reads)], output)

    sys.addaudithook(audit)
    sys.monitoring.use_tool_id(tool, "blast-radius-child")
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
