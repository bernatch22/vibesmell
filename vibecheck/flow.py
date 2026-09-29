"""A flow: the one path of the package's own calls from a function, through the ones named, to the last."""

import sys
from collections import deque

from vibecheck.graph import Package


def _resolve(package: Package, name: str) -> str:
    """The one function or method a name means: `open_call`, `Store.append`, or `log.store:Store.append`."""
    matches = [
        sid for sid in package.functions
        if sid == name or sid.endswith(":" + name) or sid.split(":", 1)[1].endswith("." + name)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"vibecheck: no function named {name}")
    sys.exit(f"vibecheck: {len(matches)} functions match {name}; say which: " + ", ".join(matches[:8]))


def flow(package: Package, names: list[str]) -> list[str]:
    """Every hop from the first name to the last, through each in between; the shortest way at each leg."""
    stops = [_resolve(package, n) for n in names]
    path = [stops[0]]
    for start, end in zip(stops, stops[1:], strict=False):
        leg = _shortest(package, start, end)
        if leg is None:
            sys.exit(f"vibecheck: no path of the package's own calls from {_short(start)} to {_short(end)}")
        path += leg[1:]
    return path


def reaching(package: Package, name: str) -> list[list[str]]:
    """Every entrypoint that reaches a function, each with its shortest chain of our calls to it; shortest first."""
    target = _resolve(package, name)
    callers: dict[str, set[str]] = {}
    for sid in package.functions:
        for callee in package.symbols[sid].calls if sid in package.symbols else ():
            callers.setdefault(callee, set()).add(sid)
    # Breadth first up the callers: the first time a function is met is its shortest way down to the target.
    toward: dict[str, str | None] = {target: None}
    queue = deque([target])
    while queue:
        here = queue.popleft()
        for caller in sorted(callers.get(here, ())):
            if caller not in toward:
                toward[caller] = here
                queue.append(caller)
    paths = []
    for door in sorted(package.entrypoints & set(toward)):
        path = [door]
        while toward[path[-1]] is not None:
            path.append(toward[path[-1]])  # type: ignore[arg-type]
        paths.append(path)
    return sorted(paths, key=lambda p: (len(p), p[0]))


def _shortest(package: Package, start: str, end: str) -> list[str] | None:
    """Breadth first over the calls of ours, so the way shown is the most direct one."""
    came_from: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here == end:
            back = [end]
            while came_from[back[-1]] is not None:
                back.append(came_from[back[-1]])  # type: ignore[arg-type]
            return back[::-1]
        for callee in sorted(package.symbols[here].calls):
            if callee in package.functions and callee not in came_from:
                came_from[callee] = here
                queue.append(callee)
    return None


def text(package: Package, path: list[str]) -> str:
    """The flow as lines: each hop with its file, line, and what it says it does."""
    lines = []
    for i, sid in enumerate(path):
        s = package.symbols[sid]
        at = f"{package.file_of(s.module)}:{s.line}"
        call_at = ""
        if i:
            before = package.symbols[path[i - 1]]
            where = sorted(before.call_lines.get(sid, ()))
            call_at = f"  ← called at {package.file_of(before.module)}:{where[0]}" if where else ""
        lines.append(f"{i:2}  {_short(sid)}  {at}{call_at}")
        if s.doc:
            lines.append(f"      {s.doc}")
    return "\n".join(lines)


def _short(sid: str) -> str:
    return sid.split(":", 1)[1]
