"""The Claude Code hook: after every save of a Python file, what vibesmell finds in it now.

It only talks. The file is saved, the edit stands, nothing is blocked; the agent reads the note
with the next step. What was already said about a file is not said again, and what an edit fixed
is said once, so the agent hears its own progress.
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

from vibesmell.checks import findings
from vibesmell.graph import Package
from vibesmell.history import SOURCES
from vibesmell.project import SKIPPED_DIRS, project_for
from vibesmell.reading import package_of

MATCHER = "Edit|Write|MultiEdit"
# A note longer than this is the summary's job, not a save's.
MOST_LINES = 8


def run() -> int:
    """Read the hook's JSON from stdin, and print the note for the agent; always exit 0."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = event.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path.endswith(SOURCES):
        return 0
    package_dir = _package_of(Path(path))
    if package_dir is None:
        return 0
    try:
        note = _note(package_dir, Path(path).resolve())
    except (SyntaxError, ValueError, OSError):
        # A file half-written does not parse; the next save will.
        return 0
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))
    return 0


def _package_of(path: Path) -> Path | None:
    """The package a saved file belongs to: the topmost folder above it with an __init__.py, or the src/ of
    the TypeScript project (a package.json with a tsconfig.json) it sits in."""
    if path.suffix != ".py":
        for directory in path.resolve().parents:
            if (directory / "tsconfig.json").exists() and (directory / "package.json").exists():
                source = directory / "src"
                return source if source.is_dir() and path.resolve().is_relative_to(source) else None
        return None
    directory = path.resolve().parent
    if not (directory / "__init__.py").exists():
        return None
    while (directory.parent / "__init__.py").exists():
        directory = directory.parent
    return None if directory.name in SKIPPED_DIRS else directory


def _note(package_dir: Path, saved: Path) -> str:
    """What the package's findings say about the saved file, minus what was said before."""
    project = project_for(str(package_dir))
    package = package_of(project)
    found, _ = findings(package, project)
    file = str(saved.relative_to(package.root))
    now = {row["id"]: row for row in (f.row(package) for f in found) if row["file"] == file or file in _files_of(package, row)}
    told = _told(package_dir, file)
    new = [row for key, row in now.items() if key not in told]
    fixed = sorted(told - set(now))
    _tell(package_dir, file, set(now))
    lines: list[str] = []
    if new:
        lines.append(f"vibesmell: {len(new)} new in {file}. The file is saved; fix them in the next edit, or say why one is wrong:")
        lines += [f"  {r['file']}:{r['line']}  [{r['check']}] {r['message']}  Fix: {r['fix']}" for r in new[:MOST_LINES]]
        if len(new) > MOST_LINES:
            lines.append(f"  ... and {len(new) - MOST_LINES} more: vibesmell check")
    if fixed:
        lines.append(f"vibesmell: this edit fixed {len(fixed)} in {file}: " + ", ".join(k.split(":", 1)[0] for k in fixed[:MOST_LINES]) + ".")
    return "\n".join(lines)


def _files_of(package: Package, row: dict[str, object]) -> set[str]:
    """The other files a finding reaches: a clump or a tunnel is about each function it names."""
    related = row["related"]
    assert isinstance(related, list)
    return {package.file_of(package.symbols[r].module) for r in related if r in package.symbols}


def _memory(package_dir: Path, file: str) -> Path:
    key = hashlib.sha1(f"{package_dir}:{file}".encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "vibesmell" / f"{key}.json"


def _told(package_dir: Path, file: str) -> set[str]:
    """The findings of this file the agent was already told about."""
    try:
        return set(json.loads(_memory(package_dir, file).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _tell(package_dir: Path, file: str, ids: set[str]) -> None:
    path = _memory(package_dir, file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except OSError:
        return


def install(settings_path: Path) -> str:
    """Add the hook to a Claude Code settings file, keeping everything else there; say what was done."""
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError:
        return f"{settings_path} is not valid JSON; nothing written"
    after = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    if any("vibesmell" in json.dumps(entry) and " hook" in json.dumps(entry) for entry in after):
        return f"already installed in {settings_path}"
    after.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": _command(), "timeout": 30}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"installed in {settings_path}: after every save of a Python file, Claude Code hears what vibesmell finds in it"


def uninstall(settings_path: Path) -> str:
    """Take the hook out of a Claude Code settings file, and nothing else."""
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"nothing to remove in {settings_path}"
    hooks = settings.get("hooks", {})
    after = hooks.get("PostToolUse", [])
    kept = [entry for entry in after if not ("vibesmell" in json.dumps(entry) and " hook" in json.dumps(entry))]
    if len(kept) == len(after):
        return f"nothing to remove in {settings_path}"
    if kept:
        hooks["PostToolUse"] = kept
    else:
        del hooks["PostToolUse"]
    if not hooks:
        settings.pop("hooks", None)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"removed from {settings_path}"


def _command() -> str:
    """The hook line: this very executable by its absolute path, so PATH does not matter."""
    executable = Path(sys.argv[0]).resolve()
    return f"{executable} hook" if executable.name == "vibesmell" else "vibesmell hook"
