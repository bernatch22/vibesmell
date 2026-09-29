"""Serve the page for one package, and read the package again whenever its files settle."""

import datetime
import json
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vibecheck.checks import CHECKS, findings
from vibecheck.finding import Finding
from vibecheck.graph import Package
from vibecheck.history import SOURCES
from vibecheck.project import Project, Skip
from vibecheck.reading import package_of
from vibecheck.score import MOST_HOPS, score

PAGE = Path(__file__).parent / "static" / "index.html"
# How often the files are looked at, and how long they must stay still before the package is read
# again: an editor or an agent saves many times a second, and one read at the end is enough.
LOOK_EVERY_S = 0.25
SETTLE_S = 0.6

type Signature = dict[str, tuple[int, int]]


def _payload(package: Package, found: list[Finding], excused: list[tuple[Skip, list[Finding]]], project: Project) -> dict[str, object]:
    """Everything the page draws, as one JSON object."""
    return {
        "package": package.name,
        "at": _now(),
        "modules": {
            name: {"file": package.file_of(name), "lines": module.lines, "layer": package.layers[name], "imports": dict(sorted(module.imports.items())), "taken": {m: sorted(n) for m, n in module.taken.items()}}
            for name, module in package.modules.items()
        },
        "symbols": {
            s.id: {
                "id": s.id, "module": s.module, "name": s.name, "kind": s.kind, "doc": s.doc,
                "line": s.line, "lines": s.lines, "private": s.private, "owner": s.owner,
                "usedBy": sorted(s.used_by), "calls": sorted(s.calls),
                "callLines": {callee: sorted(lines) for callee, lines in s.call_lines.items()},
            }
            for s in package.symbols.values()
            if not (s.kind == "constant" and s.private)
        },
        "findings": [f.row(package) for f in found],
        "skipped": [{"path": skip.path, "why": skip.why, "count": len(hidden)} for skip, hidden in excused if hidden],
        "checks": {check: {"title": title, "note": note} for check, (title, note) in CHECKS.items()},
        "score": score(package, found),
        "hops": package.hops,
        "mostHops": MOST_HOPS,
        "rules": {"layers": list(project.layers), "entries": sorted(project.entries)},
    }


class Watched:
    """The package as last read, and the last error; a thread reads it again when its files settle."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.lock = threading.Lock()
        self.version = 0
        self.body = b"{}"
        self.error: str | None = None
        self.at = ""
        # The flow the page was last told to play, and a number that grows with every telling.
        self.cue: list[str] = []
        self.tests_dir: Path | None = None
        self.cue_id = 0
        self.signature = self._signature()
        self.refresh()

    def refresh(self) -> None:
        """Read the package again; a file that does not parse leaves the last good page up, with the error."""
        try:
            package = package_of(self.project)
            found, excused = findings(package, self.project)
            body = json.dumps(_payload(package, found, excused, self.project), separators=(",", ":")).encode()
        except (SyntaxError, ValueError, OSError):
            with self.lock:
                self.error = traceback.format_exc(limit=1)
                self.version += 1
                self.at = _now()
            return
        with self.lock:
            self.body = body
            self.tests_dir = package.tests_dir.resolve() if package.tests_dir else None
            self.error = None
            self.version += 1
            self.at = _now()

    def watch(self) -> None:
        """Look at the files; once they have stayed still for SETTLE_S after a change, read again."""
        pending: Signature | None = None
        still_since = 0.0
        while True:
            time.sleep(LOOK_EVERY_S)
            now = self._signature()
            if now != (pending if pending is not None else self.signature):
                pending, still_since = now, time.monotonic()
                continue
            if pending is not None and time.monotonic() - still_since >= SETTLE_S:
                self.signature, pending = pending, None
                self.refresh()

    def _signature(self) -> Signature:
        found: Signature = {}
        for p in self.project.package_dir.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            found[str(p)] = (stat.st_mtime_ns, stat.st_size)
        return found


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def serve(project: Project, host: str, port: int) -> None:
    """Serve the page at http://host:port until interrupted."""
    watched = Watched(project)
    threading.Thread(target=watched.watch, daemon=True).start()
    package_dir = project.package_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/graph.json":
                with watched.lock:
                    body = watched.body
                self._send(body, "application/json")
            elif path == "/version":
                with watched.lock:
                    state = {
                        "tool": "vibecheck", "package": str(package_dir),
                        "version": watched.version, "error": watched.error, "at": watched.at, "cue": watched.cue_id, "flow": watched.cue,
                    }
                self._send(json.dumps(state).encode(), "application/json")
            elif path == "/source":
                asked = urllib.parse.parse_qs(query).get("file", [""])[0]
                wanted = (package_dir.parent / asked).resolve()
                # Only a Python file of the package or of its tests: the page never reads anything else.
                with watched.lock:
                    tests = watched.tests_dir
                inside = wanted.is_relative_to(package_dir) or (tests is not None and wanted.is_relative_to(tests))
                if wanted.suffix not in SOURCES or not inside or not wanted.is_file():
                    self.send_error(404)
                    return
                self._send(wanted.read_bytes(), "text/plain; charset=utf-8")
            elif path == "/diff":
                asked = urllib.parse.parse_qs(query)
                commit, name = asked.get("commit", [""])[0], asked.get("file", [""])[0]
                wanted = (package_dir.parent / name).resolve()
                # A short hash and a Python file of the package: nothing else reaches git.
                if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or wanted.suffix not in SOURCES or not wanted.is_relative_to(package_dir):
                    self.send_error(404)
                    return
                try:
                    diff = subprocess.run(["git", "show", "--format=", "--unified=3", commit, "--", str(wanted)], capture_output=True, cwd=package_dir, check=True, timeout=10).stdout
                except (OSError, subprocess.SubprocessError):
                    self.send_error(404)
                    return
                self._send(diff, "text/plain; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
            """`/play` with {"path": [...]}: the page opens that flow and animates it."""
            if self.path != "/play":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.send_error(400)
                return
            flow = body.get("path")
            if not isinstance(flow, list) or not flow:
                self.send_error(400)
                return
            with watched.lock:
                watched.cue = [str(x) for x in flow]
                watched.cue_id += 1
            self._send(b'{"ok":true}', "application/json")

        def _send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's own
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"vibecheck: {project.package_dir} at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
