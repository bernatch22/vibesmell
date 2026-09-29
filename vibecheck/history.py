"""What git remembers of the package: which of its files change together."""

import collections
import itertools
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The files a commit is read for: Python and TypeScript sources.
SOURCES = (".py", ".ts", ".tsx", ".mts", ".cts")
# How far back to read, and a commit touching more files than this is a sweep, not a change.
COMMITS = 1000
SWEEP = 20


@dataclass(frozen=True)
class Commit:
    """One commit: its short hash and its subject line."""

    hash: str
    subject: str


@dataclass(frozen=True)
class CoChange:
    """Two files of the package, how often each changed, and the commits that changed both, newest first."""

    a: str
    b: str
    changes_a: int
    changes_b: int
    commits: tuple[Commit, ...]

    @property
    def together(self) -> int:
        return len(self.commits)


def co_changes(package_dir: Path) -> list[CoChange]:
    """Every pair of the package's files committed together at least once; empty without git."""
    package_dir = package_dir.resolve()
    try:
        out = subprocess.run(
            ["git", "log", "--name-only", "--format=%x00%h %s", f"-n{COMMITS}", "--", str(package_dir)],
            capture_output=True, text=True, cwd=package_dir, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=package_dir, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return []
    prefix = str(package_dir.relative_to(Path(top))) + "/"
    # Paths as the package prints them: from the directory the package sits in, not from git's top.
    root = package_dir.parent
    commits: list[tuple[Commit, set[str]]] = []
    for chunk in out.split("\0"):
        head, *lines = chunk.strip().splitlines() or [""]
        if not head:
            continue
        short, _, subject = head.partition(" ")
        files = {str((Path(top) / line).relative_to(root)) for line in lines if line.startswith(prefix) and line.endswith(SOURCES)}
        if 0 < len(files) <= SWEEP:
            commits.append((Commit(short, subject), files))
    changes: collections.Counter[str] = collections.Counter(f for _, files in commits for f in files)
    together: dict[tuple[str, str], list[Commit]] = collections.defaultdict(list)
    for commit, files in commits:
        for a, b in itertools.combinations(sorted(files), 2):
            together[(a, b)].append(commit)
    return [CoChange(a, b, changes[a], changes[b], tuple(both)) for (a, b), both in together.items()]
