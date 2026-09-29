"""TypeScript's reader: runs ts/reader.cjs, which reads the project with TypeScript's own checker,
and turns what it prints into the same Package Python's reader builds."""

import collections
import json
import shutil
import subprocess
import sys
from pathlib import Path

from vibecheck.facts import Arg, Call, ClassFacts, Facts, FunctionFacts, LibraryCall
from vibecheck.graph import Module, Package, Symbol, hops_of, layers_of
from vibecheck.history import co_changes

READER = Path(__file__).parent / "ts" / "reader.cjs"


def read(package_dir: Path, keep: frozenset[str] = frozenset(), sinks: frozenset[str] = frozenset()) -> Package:
    """The TypeScript project whose sources are in `package_dir`, read whole."""
    project_dir = _project_dir(package_dir)
    raw = json.loads(_run(project_dir, package_dir))
    modules = {
        name: Module(name=name, path=Path(m["path"]), lines=m["lines"], imports=dict(m["imports"]), taken={k: set(v) for k, v in m["taken"].items()}, external=dict(m["external"]))
        for name, m in raw["modules"].items()
    }
    symbols = {
        sid: Symbol(
            id=sid, module=s["module"], name=s["name"], kind=s["kind"], doc=s["doc"], line=s["line"], lines=s["lines"],
            private=s["private"], owner=s["owner"], used_by=set(s["used_by"]), calls=set(s["calls"]),
            call_lines={k: set(v) for k, v in s["call_lines"].items()},
        )
        for sid, s in raw["symbols"].items()
    }
    facts = _facts(raw["facts"])
    entrypoints = frozenset(raw["entrypoints"])
    return Package(
        name=raw["name"],
        root=project_dir,
        modules=modules,
        symbols=symbols,
        functions=facts.functions,
        entrypoints=entrypoints,
        keep=keep,
        hops=hops_of(symbols, facts.functions, entrypoints, sinks),
        layers=layers_of(modules),
        read_names=collections.Counter(raw["read_names"]),
        tested_names=collections.Counter(raw["tested_names"]),
        test_uses={name: [tuple(u) for u in uses] for name, uses in raw["test_uses"].items()},
        tests_dir=Path(raw["tests_dir"]) if raw["tests_dir"] else None,
        facts=facts,
        co_changes=co_changes(package_dir),
        language="typescript",
    )


def _project_dir(package_dir: Path) -> Path:
    """The folder with the package.json and the tsconfig.json the sources belong to."""
    for directory in [package_dir, *package_dir.parents]:
        if (directory / "package.json").exists() and (directory / "tsconfig.json").exists():
            return directory
    sys.exit(f"vibecheck: no package.json with a tsconfig.json beside it above {package_dir}")


def _run(project_dir: Path, package_dir: Path) -> str:
    """Run the reader, installing its TypeScript first when it is not there yet."""
    node = shutil.which("node")
    if node is None:
        sys.exit("vibecheck: reading TypeScript needs node on the PATH")
    if not (READER.parent / "node_modules" / "typescript").exists():
        npm = shutil.which("npm") or sys.exit("vibecheck: reading TypeScript needs npm, once, to install its TypeScript")
        subprocess.run([npm, "install", "--silent", "--no-audit", "--no-fund"], cwd=READER.parent, check=True)
    done = subprocess.run([node, str(READER), str(project_dir), str(package_dir)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.exit(f"vibecheck: the TypeScript reader failed:\n{done.stderr.strip()[-2000:]}")
    return done.stdout


def _arg(raw: dict[str, object]) -> Arg:
    return Arg(
        text=str(raw["text"]), literal=bool(raw["literal"]), value=str(raw["value"]), boolean=bool(raw["boolean"]),
        name=raw["name"] if isinstance(raw["name"], str) else None,
        attr_of=raw["attr_of"] if isinstance(raw["attr_of"], str) else None,
        names=frozenset(raw["names"]),  # type: ignore[arg-type]
    )


def _call(raw: dict[str, object]) -> Call:
    return Call(
        name=str(raw["name"]), file=str(raw["file"]), line=int(raw["line"]),  # type: ignore[arg-type]
        args=tuple(_arg(a) for a in raw["args"]),  # type: ignore[attr-defined]
        keywords=tuple((k, _arg(a)) for k, a in raw["keywords"]),  # type: ignore[attr-defined]
        spreads=bool(raw["spreads"]), dropped=bool(raw["dropped"]), in_tests=bool(raw["in_tests"]),
    )


def _function(raw: dict[str, object]) -> FunctionFacts:
    first_if = raw["first_if"]
    sole = raw["sole_call"]
    return FunctionFacts(
        name=str(raw["name"]),
        params=tuple(raw["params"]),  # type: ignore[arg-type]
        positional=tuple(raw["positional"]),  # type: ignore[arg-type]
        defaults=tuple((str(n), str(d)) for n, d in raw["defaults"]),  # type: ignore[attr-defined]
        flags=frozenset(raw["flags"]),  # type: ignore[arg-type]
        decorated=bool(raw["decorated"]),
        stub=bool(raw["stub"]),
        returns_value=bool(raw["returns_value"]),
        loads=collections.Counter(raw["loads"]),  # type: ignore[arg-type]
        relayed=frozenset(raw["relayed"]),  # type: ignore[arg-type]
        sole_call=_call(sole) if isinstance(sole, dict) else None,
        first_if=(str(first_if[0]), int(first_if[1]), int(first_if[2])) if isinstance(first_if, list) else None,
        shape=raw["shape"] if isinstance(raw["shape"], str) else None,
        receiver_reads=collections.Counter(raw["receiver_reads"]),  # type: ignore[arg-type]
        annotations=dict(raw["annotations"]),  # type: ignore[arg-type]
        calls=tuple(_call(c) for c in raw["calls"]),  # type: ignore[attr-defined]
    )


def _facts(raw: dict[str, object]) -> Facts:
    functions = raw["functions"]
    classes = raw["classes"]
    assert isinstance(functions, dict) and isinstance(classes, dict)
    return Facts(
        functions={sid: _function(f) for sid, f in functions.items()},
        classes={
            sid: ClassFacts(
                fields=tuple((str(n), str(t)) for n, t in c["fields"]), bases=tuple(c["bases"]), library_base=bool(c["library_base"]),
                decorated=bool(c["decorated"]), abstract=bool(c["abstract"]),
            )
            for sid, c in classes.items()
        },
        calls=[_call(c) for c in raw["calls"]],  # type: ignore[attr-defined]
        handed={(str(m), str(n)) for m, n in raw["handed"]},  # type: ignore[attr-defined]
        handed_in_tests=set(raw["handed_in_tests"]),  # type: ignore[arg-type]
        attribute_reads=set(raw["attribute_reads"]),  # type: ignore[arg-type]
        matched_fields=set(raw["matched_fields"]),  # type: ignore[arg-type]
        listed_names=set(raw["listed_names"]),  # type: ignore[arg-type]
        called_names={m: set(n) for m, n in raw["called_names"].items()},  # type: ignore[attr-defined]
        library_calls=[LibraryCall(str(c["api"]), str(c["module"]), int(c["line"]), bool(c["raised_or_returned"])) for c in raw["library_calls"]],  # type: ignore[attr-defined]
    )
