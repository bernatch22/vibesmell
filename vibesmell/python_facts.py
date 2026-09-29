"""Python's reader of facts: what each call, function and class of a Python package says to the checks."""

import ast
import collections
import copy
import hashlib
import os
import sys
from pathlib import Path

from vibesmell.facts import Arg, Call, ClassFacts, Facts, FunctionFacts, LibraryCall

# A body this small is a one-liner, and two alike are a pattern, not a copy.
SHAPE_NODES = 40


def facts_of(
    package: str,
    root: Path,
    trees: dict[str, tuple[Path, ast.Module]],
    tests: list[tuple[Path, ast.Module]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    classes: dict[str, ast.ClassDef],
) -> Facts:
    """The facts of every module, function and class of the package, and of the calls its tests make."""
    facts = Facts()
    parents: dict[int, ast.AST] = {}
    for module, (path, tree) in trees.items():
        parents |= _parents(tree)
        file = os.path.relpath(path, root)
        facts.calls += [_call(node, file, parents, in_tests=False) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        facts.handed |= {(module, name) for name in _handed(tree)}
        facts.called_names[module] = {c for node in ast.walk(tree) if isinstance(node, ast.Call) and (c := _call_name(node))}
        facts.library_calls += _library_calls(package, module, tree, parents)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                facts.attribute_reads.add(node.attr)
            elif isinstance(node, ast.MatchClass):
                facts.matched_fields |= set(node.kwd_attrs)
                facts.listed_names.add(_base_name(node.cls))
            elif isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                items = [*node.keys, *node.values] if isinstance(node, ast.Dict) else list(node.elts)
                facts.listed_names |= {i.id for i in items if isinstance(i, ast.Name)} | {i.attr for i in items if isinstance(i, ast.Attribute)}
    for path, tree in tests:
        test_parents = _parents(tree)
        file = os.path.relpath(path, root)
        facts.calls += [_call(node, file, test_parents, in_tests=True) for node in ast.walk(tree) if isinstance(node, ast.Call)]
        facts.handed_in_tests |= _handed(tree)
    for sid, node in functions.items():
        file = os.path.relpath(trees[sid.split(":")[0]][0], root)
        facts.functions[sid] = _function(node, file, parents)
    for sid, node in classes.items():
        facts.classes[sid] = _class(sid, node, classes)
    return facts


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _call_name(call: ast.Call) -> str | None:
    """The last name a call is made through: `f` for `f(...)`, `m` for `x.y.m(...)`."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _arg(node: ast.expr) -> Arg:
    literal = isinstance(node, ast.Constant)
    value = node.value if isinstance(node, ast.Constant) else None
    return Arg(
        text=ast.unparse(node),
        literal=literal,
        value=repr((type(value).__name__, value)) if literal else "",
        boolean=isinstance(value, bool),
        name=node.id if isinstance(node, ast.Name) else None,
        attr_of=node.value.id if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) else None,
        names=frozenset(n.id for n in ast.walk(node) if isinstance(n, ast.Name)),
    )


def _call(node: ast.Call, file: str, parents: dict[int, ast.AST], *, in_tests: bool) -> Call:
    up = parents.get(id(node))
    if isinstance(up, ast.Await):
        up = parents.get(id(up))
    return Call(
        name=_call_name(node) or "",
        file=file,
        line=node.lineno,
        args=tuple(_arg(a) for a in node.args if not isinstance(a, ast.Starred)),
        keywords=tuple((k.arg, _arg(k.value)) for k in node.keywords if k.arg),
        spreads=any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords),
        dropped=isinstance(up, ast.Expr),
        in_tests=in_tests,
    )


def _handed(tree: ast.Module) -> set[str]:
    """Names read without being called: given to a framework, a table, a callback slot."""
    called = {id(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in called:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and id(node) not in called:
            found.add(node.attr)
    return found


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    args = node.args
    return tuple(a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs] if a.arg not in ("self", "cls"))


def _statements(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """The body without its docstring."""
    return [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]


def _function(node: ast.FunctionDef | ast.AsyncFunctionDef, file: str, parents: dict[int, ast.AST]) -> FunctionFacts:
    args = node.args
    everyone = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    positional = tuple(a.arg for a in [*args.posonlyargs, *args.args] if a.arg not in ("self", "cls"))
    loads: collections.Counter[str] = collections.Counter()
    as_argument: set[int] = set()
    calls: list[Call] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            as_argument |= {id(a) for a in [*inner.args, *(k.value for k in inner.keywords)] if isinstance(a, ast.Name)}
            calls.append(_call(inner, file, parents, in_tests=False))
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
            loads[inner.id] += 1
    once = {
        n.id for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and loads[n.id] == 1 and id(n) in as_argument
    }
    body = _statements(node)
    only = _only_call(body)
    return FunctionFacts(
        name=node.name,
        params=_params(node),
        positional=positional,
        defaults=tuple((name, ast.unparse(default)) for name, default in _defaults(node)),
        flags=frozenset(_flags(node)),
        decorated=bool(node.decorator_list),
        stub=_is_stub(body),
        returns_value=_returns_a_value(node),
        loads=loads,
        relayed=frozenset(p for p in _params(node) if p in once),
        sole_call=_call(only, file, parents, in_tests=False) if only is not None else None,
        first_if=_first_if(body),
        shape=_shape(node, body),
        receiver_reads=collections.Counter(
            n.value.id for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name)
        ),
        annotations={a.arg: name for a in everyone if (name := _annotated(a.annotation))},
        calls=tuple(calls),
    )


def _only_call(body: list[ast.stmt]) -> ast.Call | None:
    """The call a body is made of, when it is one `return f(...)`, `return await f(...)` or `f(...)`."""
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
        return None
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    return value if isinstance(value, ast.Call) else None


def _defaults(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.expr]]:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    pairs = list(zip([a.arg for a in positional[len(positional) - len(args.defaults):]], args.defaults, strict=False))
    pairs += [(a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False) if d is not None]
    return [(name, default) for name, default in pairs if name not in ("self", "cls")]


def _flags(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The parameters annotated `bool` or defaulting to one."""
    args = node.args
    everyone = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    defaults = dict(zip([a.arg for a in everyone[len(everyone) - len(args.defaults):]], args.defaults, strict=False))
    defaults |= {a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False) if d is not None}
    return {
        a.arg
        for a in everyone
        if (isinstance(a.annotation, ast.Name) and a.annotation.id == "bool")
        or (isinstance(defaults.get(a.arg), ast.Constant) and isinstance(defaults[a.arg].value, bool))
    }


def _is_stub(body: list[ast.stmt]) -> bool:
    return len(body) == 1 and (
        isinstance(body[0], ast.Pass)
        or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and body[0].value.value is Ellipsis)
        or (isinstance(body[0], ast.Raise) and "NotImplementedError" in ast.unparse(body[0]))
    )


def _returns_a_value(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether some `return` of the function itself, not of one inside it, gives something other than None or self."""
    stack = list(node.body)
    while stack:
        inner = stack.pop()
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(inner, ast.Return) and inner.value is not None and not (isinstance(inner.value, ast.Constant) and inner.value.value is None):
            # `return self` is a fluent interface: its callers chain, or not, by taste.
            return not (isinstance(inner.value, ast.Name) and inner.value.id == "self")
        if isinstance(inner, (ast.Yield, ast.YieldFrom)):
            return False
        stack.extend(ast.iter_child_nodes(inner))
    return False


def _first_if(body: list[ast.stmt]) -> tuple[str, int, int] | None:
    """When the body starts with `if name:` or `if not name:`, the name and the lines of that `if`."""
    first = body[0] if body else None
    if not isinstance(first, ast.If):
        return None
    test = first.test.operand if isinstance(first.test, ast.UnaryOp) else first.test
    return (test.id, first.lineno, first.end_lineno or first.lineno) if isinstance(test, ast.Name) else None


def _shape(node: ast.FunctionDef | ast.AsyncFunctionDef, body: list[ast.stmt]) -> str | None:
    """The body as a tree with every local renamed by first appearance; None for a small body."""
    if not body or sum(1 for _ in ast.walk(ast.Module(body=body, type_ignores=[]))) < SHAPE_NODES:
        return None
    local = {a: f"p{i}" for i, a in enumerate(_params(node))}
    for inner in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store) and inner.id not in local:
            local[inner.id] = f"v{len(local)}"

    class Rename(ast.NodeTransformer):
        def visit_Name(self, name: ast.Name) -> ast.Name:  # noqa: N802 - the visitor's own name
            return ast.Name(id=local.get(name.id, name.id), ctx=name.ctx)

    # On a copy: the package's own tree is read for every other fact.
    tree = Rename().visit(ast.Module(body=copy.deepcopy(body), type_ignores=[]))
    return hashlib.sha1(ast.dump(tree, annotate_fields=False).encode()).hexdigest()


def _annotated(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value
    return None


def _class(sid: str, node: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> ClassFacts:
    bases = {_base_name(b) for b in node.bases}
    marked = any(
        _base_name(d) == "abstractmethod"
        for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) for d in m.decorator_list
    )
    return ClassFacts(
        fields=tuple(
            (m.target.id, ast.unparse(m.annotation))
            for m in node.body
            if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name) and _base_name(m.annotation) != "ClassVar"
        ),
        bases=tuple(_base_name(b) for b in node.bases),
        library_base=_library_base(sid, classes),
        decorated=bool(node.decorator_list),
        abstract="ABC" in bases or marked,
    )


def _library_base(sid: str, classes: dict[str, ast.ClassDef]) -> bool:
    """Whether a class descends, however far up, from a class that is not the package's: its methods may be hooks."""
    seen: set[str] = set()

    def of(cid: str) -> bool:
        node = classes.get(cid)
        if node is None or cid in seen:
            return False
        seen.add(cid)
        module = cid.split(":")[0]
        for base in node.bases:
            candidate = f"{module}:{_base_name(base)}"
            if candidate not in classes or of(candidate):
                return True
        return False

    return of(sid)


def _library_calls(package: str, module: str, tree: ast.Module, parents: dict[int, ast.AST]) -> list[LibraryCall]:
    """Calls, inside function bodies, through a name imported from a library outside the standard one."""
    outside = {alias: root for alias, root in _outside_names(tree).items() if root not in sys.stdlib_module_names and root != package}
    found: list[LibraryCall] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # The body only: a default like `Query(...)` declares something to a framework, it asks for no work.
        for call in (c for stmt in scope.body for c in ast.walk(stmt)):
            if isinstance(call, ast.Call) and (api := _api_of(call, outside)):
                built = isinstance(parents.get(id(call)), (ast.Raise, ast.Return)) and api.split(".")[-1][:1].isupper()
                found.append(LibraryCall(api, module, call.lineno, built))
    return found


def _outside_names(tree: ast.Module) -> dict[str, str]:
    """Each name a module binds by importing from outside, and the library it comes from."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                found[alias.asname or alias.name] = node.module.split(".")[0]
    return found


def _api_of(call: ast.Call, outside: dict[str, str]) -> str | None:
    """`httpx.post` for `httpx.post(...)`, `sql.SQL` for `sql.SQL(...)`: a call through a name imported from outside."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in outside:
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name) and func.id in outside:
        return func.id
    return None
