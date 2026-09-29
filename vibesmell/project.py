"""What a project tells vibesmell: the package, its language, and the rules it sets: `[tool.vibesmell]` in
pyproject.toml for Python, the `vibesmell` key of package.json for TypeScript."""

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

SKIPPED_DIRS = frozenset({"tests", "test", "docs", "scripts", "examples", "node_modules"})


@dataclass(frozen=True)
class Skip:
    """Findings a folder is excused from, and the reason, which is printed every time."""

    path: str
    checks: frozenset[str]
    why: str


@dataclass(frozen=True)
class Project:
    """A package to read, and the project's rules."""

    package_dir: Path
    # "python", or "typescript" for a source folder beside a tsconfig.json.
    language: str = "python"
    # Names that mark an entrypoint, beyond routed functions and `[project.scripts]`.
    entries: frozenset[str] = frozenset()
    # Symbols never reported dead: a plugin hook, a name the package exports for others.
    keep: frozenset[str] = frozenset()
    # A library: every public name is a door, since the code that uses it lives in other projects.
    library: bool = False
    # Functions whose call is the effect: the hops stop there (a write to the log, a send).
    sinks: frozenset[str] = frozenset()
    # Folders from the bottom up: one may import only from those before it.
    layers: tuple[str, ...] = ()
    # Folder -> names it may not import, of the package or from outside.
    forbid: dict[str, tuple[str, ...]] | None = None
    skips: tuple[Skip, ...] = ()


def project_for(given: str | None) -> Project:
    """The project of the package named, or of the one package under the current directory."""
    package_dir = Path(given).resolve() if given else _find_package(Path.cwd())
    if _is_typescript(package_dir):
        return _typescript_project(package_dir)
    pyproject = _up_to(package_dir, "pyproject.toml")
    settings: dict[str, object] = {}
    scripts: dict[str, object] = {}
    if pyproject:
        try:
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            settings = parsed.get("tool", {}).get("vibesmell", {})
            scripts = parsed.get("project", {}).get("scripts", {})
        except (OSError, ValueError):
            pass
    # `pkg.cli:main` in [project.scripts] names an entrypoint as surely as a route decorator does.
    script_entries = {str(v).split(":")[-1] for v in scripts.values() if ":" in str(v)}
    forbid = settings.get("forbid")
    return Project(
        package_dir=package_dir,
        entries=frozenset(_strings(settings.get("entries"))) | frozenset(script_entries),
        keep=frozenset(_strings(settings.get("keep"))),
        library=settings.get("library") is True,
        sinks=frozenset(_strings(settings.get("sinks"))),
        layers=tuple(_strings(settings.get("layers"))),
        forbid={str(k): tuple(_strings(v)) for k, v in forbid.items()} if isinstance(forbid, dict) else None,
        skips=tuple(_skips(settings.get("skip"))),
    )


def uses_vibesmell(package_dir: Path) -> bool:
    """Whether the project says it uses vibesmell: a `[tool.vibesmell]` table in its pyproject.toml, empty or
    not, or a `vibesmell` key in its package.json. The hook speaks only in a project that says so."""
    pyproject = _up_to(package_dir, "pyproject.toml")
    if pyproject:
        try:
            if "vibesmell" in tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}):
                return True
        except (OSError, ValueError):
            pass
    manifest = _up_to(package_dir, "package.json")
    if manifest:
        try:
            return "vibesmell" in json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return False


def _is_typescript(package_dir: Path) -> bool:
    """A folder with no __init__.py, TypeScript in it, and a tsconfig.json beside it or above."""
    if (package_dir / "__init__.py").exists() or not _up_to(package_dir, "tsconfig.json"):
        return False
    return any(package_dir.rglob("*.ts"))


def _typescript_project(package_dir: Path) -> Project:
    """A TypeScript project: its doors come from package.json's `main` and `exports`, its rules from its `vibesmell` key."""
    manifest = _up_to(package_dir, "package.json")
    settings: dict[str, object] = {}
    if manifest:
        try:
            found = json.loads(manifest.read_text(encoding="utf-8")).get("vibesmell", {})
            settings = found if isinstance(found, dict) else {}
        except (OSError, ValueError):
            pass
    forbid = settings.get("forbid")
    return Project(
        package_dir=package_dir,
        language="typescript",
        library=settings.get("library") is True,
        entries=frozenset(_strings(settings.get("entries"))),
        keep=frozenset(_strings(settings.get("keep"))),
        sinks=frozenset(_strings(settings.get("sinks"))),
        layers=tuple(_strings(settings.get("layers"))),
        forbid={str(k): tuple(_strings(v)) for k, v in forbid.items()} if isinstance(forbid, dict) else None,
        skips=tuple(_skips(settings.get("skip"))),
    )


def _skips(value: object) -> list[Skip]:
    if not isinstance(value, list):
        return []
    found: list[Skip] = []
    for row in value:
        if not isinstance(row, dict) or not row.get("path") or not row.get("why"):
            sys.exit("vibesmell: every [[tool.vibesmell.skip]] needs a `path`, the `checks` it skips, and a `why`")
        found.append(Skip(str(row["path"]), frozenset(_strings(row.get("checks"))), str(row["why"])))
    return found


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _up_to(start: Path, name: str) -> Path | None:
    for directory in [start, *start.parents]:
        if (directory / name).exists():
            return directory / name
    return None


def _find_package(cwd: Path) -> Path:
    # A TypeScript project is read from its src/, when it has a tsconfig.json and no Python package.
    if (cwd / "tsconfig.json").exists() and (cwd / "src").is_dir() and not list(cwd.glob("*/__init__.py")):
        return (cwd / "src").resolve()
    candidates = [
        p.parent
        for pattern in ("*/__init__.py", "src/*/__init__.py")
        for p in cwd.glob(pattern)
        if p.parent.name not in SKIPPED_DIRS and not p.parent.name.startswith(".")
    ]
    if len(candidates) != 1:
        names = ", ".join(sorted(c.name for c in candidates)) or "none"
        sys.exit(f"vibesmell: say which package to read (found: {names})")
    return candidates[0].resolve()
