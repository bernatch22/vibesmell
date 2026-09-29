"""Read a Python package whole: its symbols, who uses each, its module layers, its call graph."""

import ast
import collections
import collections.abc
import os
from dataclasses import dataclass, field
from pathlib import Path

from vibecheck.facts import Facts, FunctionFacts
from vibecheck.history import CoChange, co_changes
from vibecheck.python_facts import facts_of

# A decorator whose last name is one of these marks a function as an entrypoint: a web door.
ENTRY_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "websocket", "route", "api_route"})


@dataclass
class Symbol:
    """One named thing of the package: a class, a method, a function, a type alias or a constant."""

    id: str
    module: str
    name: str
    kind: str
    doc: str
    line: int
    lines: int
    private: bool
    owner: str | None = None
    used_by: set[str] = field(default_factory=set[str])
    calls: set[str] = field(default_factory=set[str])
    call_lines: dict[str, set[int]] = field(default_factory=dict[str, set[int]])


@dataclass
class Module:
    """One file of the package: where it is, how long, and what it imports."""

    name: str
    path: Path
    lines: int
    # The package's modules it imports, and every top-level name it imports from outside, each at its first line.
    imports: dict[str, int] = field(default_factory=dict[str, int])
    # The names it takes of each module of the package, as its `from x import a, b` says.
    taken: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    external: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(kw_only=True)
class Source(Module):
    """A Python file as the reader holds it: the module, and its syntax tree, which only the reader reads."""

    tree: ast.Module


@dataclass(frozen=True)
class Package:
    """A package read whole: what the checks and the score work on."""

    name: str
    root: Path
    modules: dict[str, Module]
    symbols: dict[str, Symbol]
    functions: dict[str, FunctionFacts]
    entrypoints: frozenset[str]
    keep: frozenset[str]
    hops: list[dict[str, object]]
    layers: dict[str, int]
    # Every name read anywhere, as a bare name or an attribute: by the package, and by its tests.
    read_names: collections.Counter[str]
    tested_names: collections.Counter[str]
    # Where the tests name each bare name: (file from the root, line, first and last line of the test around it).
    test_uses: dict[str, list[tuple[str, int, int, int]]]
    # The folder of those tests, the only files beside the package the page may show.
    tests_dir: Path | None
    # What each call, function and class says: the checks read these, never a syntax tree.
    facts: Facts
    # Files of the package committed together, from git; empty without it.
    co_changes: list[CoChange]
    # The language it is written in: "python" or "typescript".
    language: str = "python"

    def module_of(self, file: str) -> str | None:
        """The module a path from the project's root names, if it is one of the package's."""
        return next((name for name in self.modules if self.file_of(name) == file), None)

    def file_of(self, module: str) -> str:
        """The path of a module, from the project's root."""
        return str(self.modules[module].path.relative_to(self.root))


def read(
    package_dir: Path,
    entry_names: frozenset[str] = frozenset(),
    keep: frozenset[str] = frozenset(),
    sinks: frozenset[str] = frozenset(),
) -> Package:
    """The package whole; entrypoints are routed functions and those named in `entry_names`."""
    modules = _modules(package_dir)
    symbols = _symbols(modules)
    names = {name: _names_of(module, modules, symbols) for name, module in modules.items()}
    _aliases(modules, symbols, names)
    attributes = _attribute_types(modules, symbols, names)
    returns = _return_types(modules, symbols, names)
    _uses_and_calls(modules, symbols, names, attributes, returns)
    _module_imports(modules)
    entrypoints = _entrypoints(modules, entry_names)
    functions = _functions(modules)
    tests = _test_modules(package_dir)
    classes = {f"{name}:{node.name}": node for name, m in modules.items() for node in m.tree.body if isinstance(node, ast.ClassDef)}
    facts = facts_of(
        package_dir.name, package_dir.parent,
        {name: (m.path, m.tree) for name, m in modules.items()}, [(t.path, t.tree) for t in tests],
        functions, classes,
    )
    return Package(
        name=package_dir.name,
        root=package_dir.parent,
        modules=modules,
        symbols=symbols,
        functions=facts.functions,
        entrypoints=frozenset(entrypoints),
        keep=keep,
        hops=hops_of(symbols, functions, entrypoints, sinks),
        layers=layers_of(modules),
        read_names=_read_names(modules.values()),
        tested_names=_read_names(tests),
        test_uses=_uses_in(tests, package_dir.parent),
        tests_dir=_tests_dir(package_dir),
        facts=facts,
        co_changes=co_changes(package_dir),
    )


def _read_names(modules: "collections.abc.Iterable[Source]") -> collections.Counter[str]:
    """Every name read, as a bare name or as an attribute, with how often: the fallback of the dead check."""
    found: collections.Counter[str] = collections.Counter()
    for module in modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found[node.id] += 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                found[node.attr] += 1
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    found[alias.name] += 1
    return found


def _uses_in(tests: list[Source], root: Path) -> dict[str, list[tuple[str, int, int, int]]]:
    """Every bare name the tests read or import, with where: the file, the line, and the test around it."""
    found: dict[str, list[tuple[str, int, int, int]]] = collections.defaultdict(list)
    for module in tests:
        file = os.path.relpath(module.path, root)
        # The innermost function around a line is the test that line belongs to.
        bodies = sorted(
            ((n.lineno, n.end_lineno or n.lineno) for n in ast.walk(module.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
            key=lambda span: span[1] - span[0],
        )
        for node in ast.walk(module.tree):
            names = (
                [node.id] if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                else [node.attr] if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                else [a.name for a in node.names] if isinstance(node, ast.ImportFrom)
                else []
            )
            if not names:
                continue
            line = node.lineno
            start, end = next(((a, b) for a, b in bodies if a <= line <= b), (line, line))
            for name in names:
                found[name].append((file, line, start, end))
    return {name: sorted(set(uses)) for name, uses in found.items()}


def _tests_dir(package_dir: Path) -> Path | None:
    """The tests/ folder beside the package, at the project's root or one up; None when there is none."""
    return next((root for root in (package_dir.parent / "tests", package_dir.parent.parent / "tests") if root.is_dir()), None)


def _test_modules(package_dir: Path) -> list[Source]:
    """The test files beside the package, parsed; none when there are none."""
    root = _tests_dir(package_dir)
    found: list[Source] = []
    for path in root.rglob("*.py") if root else ():
        if "__pycache__" in path.parts or "fixtures" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            found.append(Source(name=path.stem, path=path, tree=ast.parse(source), lines=len(source.splitlines())))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
    return found


def _modules(package_dir: Path) -> dict[str, Source]:
    found: dict[str, Source] = {}
    for path in sorted(package_dir.rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        name = ".".join(path.relative_to(package_dir).with_suffix("").parts)
        source = path.read_text(encoding="utf-8")
        found[name] = Source(name=name, path=path, tree=ast.parse(source), lines=len(source.splitlines()))
    return found


def _first_line(node: ast.AST) -> str:
    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    doc = ast.get_docstring(node)
    return doc.splitlines()[0] if doc else ""


def _symbols(modules: dict[str, Source]) -> dict[str, Symbol]:
    found: dict[str, Symbol] = {}
    for name, module in modules.items():
        for node in module.tree.body:
            if isinstance(node, ast.ClassDef):
                cls = _symbol(name, node.name, "class", node)
                found[cls.id] = cls
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method = _symbol(name, f"{node.name}.{member.name}", "method", member)
                        method.owner = cls.id
                        method.private = member.name.startswith("_")
                        found[method.id] = method
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = _symbol(name, node.name, "function", node)
                found[function.id] = function
            elif isinstance(node, ast.TypeAlias):
                alias = _symbol(name, node.name.id, "type", node)
                found[alias.id] = alias
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                        constant = _symbol(name, target.id, "constant", node)
                        found[constant.id] = constant
    return found


def _symbol(module: str, name: str, kind: str, node: ast.stmt) -> Symbol:
    end = node.end_lineno or node.lineno
    return Symbol(
        id=f"{module}:{name}", module=module, name=name, kind=kind, doc=_first_line(node),
        line=node.lineno, lines=end - node.lineno + 1, private=name.split(".")[-1].startswith("_"),
    )


@dataclass
class _Names:
    symbols: dict[str, str]
    modules: dict[str, str]


def _names_of(module: Source, modules: dict[str, Source], symbols: dict[str, Symbol]) -> _Names:
    """What each name a module reads means: one of the package's symbols, or one of its modules."""
    package = _package_prefix(modules)
    by_name = {s.name: s.id for s in symbols.values() if s.module == module.name and s.kind != "method"}
    imported_modules: dict[str, str] = {}
    for node in module.tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        target = _relative_target(node, module.name) or _package_relative(node.module, package)
        if target is None:
            continue
        for alias in node.names:
            local = alias.asname or alias.name
            if f"{target}:{alias.name}" in symbols:
                by_name[local] = f"{target}:{alias.name}"
            elif _join(target, alias.name) in modules:
                imported_modules[local] = _join(target, alias.name)
    return _Names(by_name, imported_modules)


def _package_prefix(modules: dict[str, Source]) -> str:
    any_module = next(iter(modules.values()))
    depth = len(any_module.name.split("."))
    return any_module.path.parents[depth - 1].name


def _package_relative(dotted: str, package: str) -> str | None:
    if dotted == package:
        return ""
    return dotted.removeprefix(package + ".") if dotted.startswith(package + ".") else None


def _relative_target(node: ast.ImportFrom, importer: str) -> str | None:
    if not node.level:
        return None
    base = importer.split(".")[: -node.level]
    return ".".join([*base, *(node.module.split(".") if node.module else [])])


def _join(package: str, name: str) -> str:
    return f"{package}.{name}" if package else name


# What each alias of the package names, when it names one of its classes: filled by analyze.
_ALIASES: dict[str, str] = {}


def _class_of(annotation: ast.expr | None, names: _Names, symbols: dict[str, Symbol]) -> str | None:
    if annotation is None:
        return None
    for node in ast.walk(annotation):
        key = node.id if isinstance(node, ast.Name) else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
        target = names.symbols.get(key) if key else None
        if target and symbols[target].kind == "class":
            return target
        if target and target in _ALIASES:
            return _ALIASES[target]
    return None


def _aliases(modules: dict[str, Source], symbols: dict[str, Symbol], names: dict[str, "_Names"]) -> None:
    """Resolve every module-level alias of a class: `Dep = Annotated[Gateway, ...]`, `type T = Gateway`."""
    _ALIASES.clear()
    for name, module in modules.items():
        for node in module.tree.body:
            target: ast.expr | None = None
            alias: str | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                alias, target = node.targets[0].id, node.value
            elif isinstance(node, ast.TypeAlias):
                alias, target = node.name.id, node.value
            if alias is None or target is None or f"{name}:{alias}" in _ALIASES:
                continue
            cls = _class_of(target, names[name], symbols)
            if cls:
                _ALIASES[f"{name}:{alias}"] = cls


def _attribute_types(modules: dict[str, Source], symbols: dict[str, Symbol], names: dict[str, _Names]) -> dict[str, dict[str, str]]:
    """The attributes of each class whose type is one of the package's classes, as annotated."""
    types: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for name, module in modules.items():
        for node in module.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            owner = f"{name}:{node.name}"
            for member in node.body:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    cls = _class_of(member.annotation, names[name], symbols)
                    if cls:
                        types[owner][member.target.id] = cls
                if isinstance(member, ast.FunctionDef) and member.name == "__init__":
                    params = {a.arg: _class_of(a.annotation, names[name], symbols) for a in member.args.args + member.args.kwonlyargs}
                    for statement in ast.walk(member):
                        target = statement.targets[0] if isinstance(statement, ast.Assign) and len(statement.targets) == 1 else None
                        if isinstance(statement, ast.AnnAssign):
                            target = statement.target
                        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                            continue
                        value = statement.value
                        cls = _class_of(statement.annotation, names[name], symbols) if isinstance(statement, ast.AnnAssign) else None
                        if cls is None and isinstance(value, ast.Name):
                            cls = params.get(value.id)
                        if cls is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                            called = names[name].symbols.get(value.func.id)
                            cls = called if called and symbols[called].kind == "class" else None
                        if cls:
                            types[owner][target.attr] = cls
    return types


def _return_types(modules: dict[str, Source], symbols: dict[str, Symbol], names: dict[str, _Names]) -> dict[str, str]:
    """For each function or method that says it returns one of the package's classes, that class."""
    found: dict[str, str] = {}
    for name, module in modules.items():
        for scope, node, _ in _scopes(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = _class_of(node.returns, names[name], symbols)
                if cls:
                    found[scope] = cls
    return found


def _scopes(module: Source) -> list[tuple[str, ast.AST, str | None]]:
    """Every place code runs: each function, each method, each class body, and the module body."""
    found: list[tuple[str, ast.AST, str | None]] = []
    for node in module.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((f"{module.name}:{node.name}", node, None))
        elif isinstance(node, ast.ClassDef):
            found.append((f"{module.name}:{node.name}", node, node.name))
            found += [(f"{module.name}:{node.name}.{m.name}", m, node.name) for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            found.append((f"{module.name}:<module>", node, None))
    return found


def _uses_and_calls(
    modules: dict[str, Source],
    symbols: dict[str, Symbol],
    names: dict[str, _Names],
    attributes: dict[str, dict[str, str]],
    returns: dict[str, str],
) -> None:
    for name, module in modules.items():
        known = names[name]
        for scope, node, cls in _scopes(module):
            owner = f"{name}:{cls}" if cls else None
            local = _local_types(node, known, symbols)

            def typed(expr: ast.expr) -> str | None:
                if isinstance(expr, ast.Name):
                    return owner if expr.id in ("self", "cls") else local.get(expr.id)
                if isinstance(expr, ast.Attribute):
                    base = typed(expr.value)
                    return attributes.get(base, {}).get(expr.attr) if base else None
                if isinstance(expr, ast.Await):
                    return typed(expr.value)
                if isinstance(expr, ast.IfExp):
                    left, right = typed(expr.body), typed(expr.orelse)
                    return left if left == right or right is None else None
                if isinstance(expr, ast.Call):
                    target = resolve(expr.func)
                    if target and symbols[target].kind == "class":
                        return target
                    return returns.get(target) if target else None
                return None

            def resolve(expr: ast.expr) -> str | None:
                if isinstance(expr, ast.Name):
                    return known.symbols.get(expr.id)
                if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id in known.modules:
                    candidate = f"{known.modules[expr.value.id]}:{expr.attr}"
                    return candidate if candidate in symbols else None
                if isinstance(expr, ast.Attribute):
                    base = typed(expr.value)
                    candidate = f"{base}.{expr.attr}" if base else None
                    return candidate if candidate in symbols else None
                return None

            # Two passes: a local's type may come from a call whose receiver is typed later in the body.
            for _ in range(2):
                for statement in ast.walk(node):
                    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                        found = typed(statement.value)
                        if found:
                            local[statement.targets[0].id] = found
                    if isinstance(statement, (ast.With, ast.AsyncWith)):
                        for item in statement.items:
                            if isinstance(item.optional_vars, ast.Name):
                                found = typed(item.context_expr)
                                if found:
                                    local[item.optional_vars.id] = found
                    if isinstance(statement, (ast.For, ast.AsyncFor)) and isinstance(statement.target, ast.Name):
                        found = typed(statement.iter)
                        if found:
                            local[statement.target.id] = found

            roots = [node] if not isinstance(node, ast.ClassDef) else [m for m in node.body if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))] + list(node.bases)
            for root in roots:
                for inner in ast.walk(root):
                    if isinstance(inner, (ast.Name, ast.Attribute)) and isinstance(getattr(inner, "ctx", None), ast.Load):
                        target = resolve(inner)
                        if target and target != scope:
                            symbols[target].used_by.add(scope)
                    if isinstance(inner, ast.Call):
                        target = resolve(inner.func)
                        if target and symbols[target].kind in ("function", "method", "class") and scope in symbols:
                            symbols[scope].calls.add(target)
                            symbols[scope].call_lines.setdefault(target, set()).add(inner.lineno)


def _local_types(node: ast.AST, known: _Names, symbols: dict[str, Symbol]) -> dict[str, str]:
    types: dict[str, str] = {}
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return types
    for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
        cls = _class_of(arg.annotation, known, symbols)
        if cls:
            types[arg.arg] = cls
    for statement in ast.walk(node):
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            cls = _class_of(statement.annotation, known, symbols)
            if cls:
                types[statement.target.id] = cls
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            value = statement.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                called = known.symbols.get(value.func.id)
                if called and symbols[called].kind == "class":
                    types[statement.targets[0].id] = called
    return types


def _module_imports(modules: dict[str, Source]) -> None:
    """What each module imports: of the package, by module; from outside, by top-level name. A lazy import counts."""
    package = _package_prefix(modules)
    for name, module in modules.items():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] != package:
                        module.external.setdefault(alias.name.split(".")[0], node.lineno)
                continue
            if not isinstance(node, ast.ImportFrom) or not node.module and not node.level:
                continue
            target = _relative_target(node, name) if node.level else _package_relative(node.module or "", package)
            if target is None:
                module.external.setdefault((node.module or "").split(".")[0], node.lineno)
                continue
            for candidate in [target, *(_join(target, a.name) for a in node.names)]:
                if candidate in modules and candidate != name:
                    module.imports.setdefault(candidate, node.lineno)
            if target in modules and target != name:
                module.taken.setdefault(target, set()).update(a.name for a in node.names if _join(target, a.name) not in modules)


def layers_of(modules: "collections.abc.Mapping[str, Module]") -> dict[str, int]:
    """Layer 0 imports nothing of the package; every other sits one above the highest it imports."""
    layer: dict[str, int] = {}

    def of(name: str, path: tuple[str, ...]) -> int:
        if name in layer:
            return layer[name]
        if name in path:
            return 0
        below = [of(other, (*path, name)) for other in modules[name].imports]
        layer[name] = 1 + max(below) if below else 0
        return layer[name]

    for name in modules:
        of(name, ())
    return layer


def _entrypoints(
    modules: dict[str, Source], entry_names: frozenset[str]
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found = {
        scope: node
        for scope, node in _functions(modules).items()
        if scope.rsplit(".", 1)[-1].rsplit(":", 1)[-1] in entry_names
    }
    for name, module in modules.items():
        for node in module.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(call, ast.Attribute) and call.attr in ENTRY_DECORATORS:
                    found[f"{name}:{node.name}"] = node
    return found


def _functions(modules: dict[str, Source]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for module in modules.values():
        for scope, node, _ in _scopes(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[scope] = node
    return found


def hops_of(
    symbols: dict[str, Symbol],
    functions: "collections.abc.Collection[str]",
    entrypoints: "collections.abc.Collection[str]",
    sinks: frozenset[str],
) -> list[dict[str, object]]:
    """The longest chain of the package's own functions under each entrypoint, ending at a sink."""
    ours = {sid: sorted(c for c in symbols[sid].calls if symbols[c].kind in ("function", "method")) for sid in functions if sid in symbols}
    # A sink is named by its tail (`Log.append`, `logs:Log.append`): calling it is the effect.
    for sid in ours:
        if any(sid == sink or sid.endswith(":" + sink) or sid.endswith("." + sink) for sink in sinks):
            ours[sid] = []

    def deepest(sid: str, path: tuple[str, ...]) -> list[str]:
        best: list[str] = []
        for callee in ours.get(sid, []):
            if callee not in path and callee != sid:
                below = deepest(callee, (*path, sid))
                if len(below) > len(best):
                    best = below
        return [sid, *best]

    # Every call of ours under the entrypoint, in the order the body makes them; one already read is a leaf.
    def tree(sid: str, lines: list[int], read: set[str]) -> dict[str, object]:
        if sid in read:
            return {"id": sid, "lines": lines, "seen": True, "kids": []}
        read.add(sid)
        at = symbols[sid].call_lines
        order = sorted(ours.get(sid, []), key=lambda callee: (min(at.get(callee, {0})), callee))
        return {"id": sid, "lines": lines, "seen": False, "kids": [tree(callee, sorted(at.get(callee, ())), read) for callee in order]}

    paths = sorted((deepest(sid, ()) for sid in entrypoints), key=len, reverse=True)
    return [{"entry": path[0], "hops": len(path) - 1, "path": path, "tree": tree(path[0], [], set())} for path in paths]
