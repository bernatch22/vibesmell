"""vibecheck: the findings and the score of a Python package, from nothing but its source."""

import argparse
import dataclasses
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from vibecheck import hook
from vibecheck.checks import CHECKS, findings
from vibecheck.flow import flow, reaching, text
from vibecheck.project import project_for
from vibecheck.reading import package_of
from vibecheck.score import score
from vibecheck.server import serve
from vibecheck.summary import summary


def main(argv: list[str] | None = None) -> int:
    """The command line: `check`, `score`, `summary`, `serve`, and the Claude Code hook."""
    parser = argparse.ArgumentParser(prog="vibecheck", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook", help="Run as Claude Code's PostToolUse hook: reads its JSON on stdin.")
    for name, doc in (("install-hook", "Add the hook to Claude Code's settings."), ("uninstall-hook", "Take the hook out of Claude Code's settings.")):
        command = commands.add_parser(name, help=doc)
        where = command.add_mutually_exclusive_group()
        where.add_argument("--project", action="store_true", help="this project's .claude/settings.json, not ~/.claude/settings.json")
        where.add_argument("--dir", help="another Claude Code config directory, e.g. ~/.claude-work")
    playing = commands.add_parser("play", help="Animate a flow on the page: from a function, through the ones named, to the last.")
    playing.add_argument("names", nargs="+", metavar="function", help="`open_call`, `Store.append`, or `log.store:Store.append`")
    playing.add_argument("--package", dest="package_dir", help="the package directory; found alone when there is one")
    playing.add_argument("--text", action="store_true", help="print the flow instead of playing it")
    playing.add_argument("--port", type=int, default=8765)
    playing.add_argument("--library", action="store_true", help="a library: every public name is a door")
    reach = commands.add_parser("reach", help="Every entrypoint that reaches a function, with its chain.")
    reach.add_argument("name", metavar="function")
    reach.add_argument("--package", dest="package_dir", help="the package directory; found alone when there is one")
    reading = (
        ("check", "Every finding; exit 1 when there is one."),
        ("score", "Six attributes from 0 to 100."),
        ("summary", "The whole report as Markdown, for an agent to act on."),
        ("serve", "The page, read again on every change."),
    )
    for name, doc in reading:
        command = commands.add_parser(name, help=doc)
        command.add_argument("package", nargs="?", help="the package directory; found alone when there is one")
        command.add_argument("--library", action="store_true", help="a library: every public name is a door, as `library = true` says")
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
        elif name != "summary":
            command.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return hook.run()
    if args.command in ("install-hook", "uninstall-hook"):
        target = Path(args.dir).expanduser() / "settings.json" if args.dir else (Path.cwd() if args.project else Path.home()) / ".claude" / "settings.json"
        print(hook.install(target) if args.command == "install-hook" else hook.uninstall(target))
        return 0
    project = project_for(getattr(args, "package", None) or getattr(args, "package_dir", None))
    if getattr(args, "library", False):
        project = dataclasses.replace(project, library=True)
    if args.command == "reach":
        package = package_of(project)
        paths = reaching(package, args.name)
        for path in paths:
            print(f"{len(path) - 1:2} hops  " + " → ".join(p.split(":", 1)[1] for p in path))
        print(f"vibecheck: {len(paths)} entrypoints reach it; play one with: vibecheck play <entry> {args.name}", file=sys.stderr)
        return 0
    if args.command == "play":
        return _play(project, args.names, args.port, as_text=args.text)
    if args.command == "serve":
        serve(project, args.host, args.port)
        return 0
    package = package_of(project)
    found, excused = findings(package, project)
    if args.command == "check":
        return _check(found, excused, package, as_json=args.as_json)
    if args.command == "summary":
        print(summary(package, found, excused, project))
        return 0
    _print_score(score(package, found), as_json=args.as_json)
    return 0


def _play(project, names: list[str], port: int, *, as_text: bool) -> int:
    """Resolve the flow, then hand it to the page; start the page first when it is not up."""
    package = package_of(project)
    path = flow(package, names)
    if as_text:
        print(text(package, path))
        return 0
    url = f"http://127.0.0.1:{port}"
    link = url + "/#play=" + ">".join(path)
    there = _listening(url)
    if there is None:
        subprocess.Popen([sys.argv[0], "serve", str(project.package_dir), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            time.sleep(0.2)
            if _listening(url) is not None:
                break
        else:
            sys.exit(f"vibecheck: the page did not come up at {url}")
        webbrowser.open(link)
    elif there.get("tool") != "vibecheck":
        sys.exit(f"vibecheck: port {port} is taken by a server that is not vibecheck (another tool's server?). Stop it, or pass --port.")
    elif there.get("package") != str(project.package_dir.resolve()):
        sys.exit(f"vibecheck: the page at {url} shows {there.get('package')}, not {project.package_dir}. Stop it, or pass --port.")
    else:
        request = urllib.request.Request(url + "/play", data=json.dumps({"path": path}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=5).read()
    print(f"{len(path) - 1} hops: " + " → ".join(p.split(":", 1)[1] for p in path))
    print(link)
    return 0


def _listening(url: str) -> dict[str, object] | None:
    """What answers at the page's address: its /version, or None when nothing listens."""
    try:
        answer = json.loads(urllib.request.urlopen(url + "/version", timeout=1).read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return answer if isinstance(answer, dict) else {}


def _check(found: list, excused: list, package, *, as_json: bool) -> int:
    rows = [f.row(package) for f in found]
    if as_json:
        print(json.dumps({"findings": rows, "skipped": [{"path": s.path, "why": s.why, "findings": [f.row(package) for f in fs]} for s, fs in excused]}, indent=1))
    else:
        for r in rows:
            print(f"{r['file']}:{r['line']}  [{r['check']}] {r['message']}  Fix: {r['fix']}")
        by_check = {c: sum(1 for r in rows if r["check"] == c) for c in CHECKS}
        summary = ", ".join(f"{n} {CHECKS[c][0].lower()}" for c, n in by_check.items() if n)
        print(f"vibecheck: {summary or 'nothing to fix'}", file=sys.stderr)
        # A skip is never silent: what it hides is counted out loud, with its reason, every run.
        for skip, hidden in excused:
            if hidden:
                print(f"vibecheck: skipped {len(hidden)} in {skip.path}: {skip.why}", file=sys.stderr)
    return 1 if rows else 0


def _print_score(result: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=1))
        return
    print(f"overall  {result['overall']:3}")
    for a in result["attributes"]:
        print(f"{a['label']:8} {a['value']:3}   {a['detail']}")


if __name__ == "__main__":
    sys.exit(main())
