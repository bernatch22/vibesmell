"""The smells code written fast leaves across files: bodies written twice, results nobody reads,
parameters nobody passes or nobody reads, a library called raw from everywhere, dependencies on
what moves more, a method that works on another's data, an abstraction with one implementation.
"""

import collections

from vibesmell.facts import Call, FunctionFacts
from vibesmell.finding import Finding
from vibesmell.graph import Package
from vibesmell.nodes import folder, is_dunder, short

# A library called raw from this many modules, in this many folders, wants one module of ours in front.
SCATTERED_MODULES = 5
SCATTERED_FOLDERS = 2
# A stable module (this many import it) leaning on one this much less stable.
UNSTABLE_FAN_IN = 5
UNSTABLE_MARGIN = 0.4
# A method reading this many attributes of one other object, and more than twice its own.
ENVY_READS = 4
# Calls that stand for a module's own setup, not for a library it should wrap.
SETUP_CALLS = frozenset({"getLogger", "TypeVar", "NewType", "Field", "field", "dataclass", "APIRouter", "ContextVar"})


def duplicates(package: Package) -> list[Finding]:
    """Two or more functions whose bodies are one body: the same tree, locals renamed alike."""
    by_shape: dict[str, list[str]] = collections.defaultdict(list)
    for sid, fn in package.functions.items():
        if not is_dunder(sid) and fn.shape:
            by_shape[fn.shape].append(sid)
    found: list[Finding] = []
    for sids in by_shape.values():
        if len(sids) < 2:
            continue
        first, *rest = sorted(sids, key=lambda s: (package.symbols[s].module, package.symbols[s].line))
        where = ", ".join(short(s) for s in rest[:4]) + (f" and {len(rest) - 4} more" if len(rest) > 4 else "")
        found.append(Finding("duplicate", first, f"Its body is written again in {where}.", f"Keep `{short(first)}` and let the others call it.", tuple(rest)))
    return found


def _calls_named(package: Package) -> dict[str, list[Call]]:
    """Every call in the package and its tests, by the last name it is made through."""
    found: dict[str, list[Call]] = collections.defaultdict(list)
    for call in package.facts.calls:
        if call.name:
            found[call.name].append(call)
    return found


def _checkable(package: Package, sid: str, fn: FunctionFacts, names: collections.Counter[str], handed: set[str]) -> bool:
    """A function whose calls can all be found by its name: unique, not a door, not registered, not a hook, not handed on."""
    symbol = package.symbols.get(sid)
    owner = package.facts.classes.get(symbol.owner or "") if symbol else None
    return bool(
        symbol
        and names[fn.name] == 1
        and sid not in package.entrypoints
        and not is_dunder(sid)
        and not fn.decorated
        and fn.name not in handed
        and not (owner and owner.library_base)
    )


def _handed(package: Package) -> set[str]:
    """Names read anywhere, in the package or its tests, without being called: given to a framework, a table, a slot."""
    return {name for _, name in package.facts.handed} | package.facts.handed_in_tests


def _sites(calls: list[Call]) -> tuple[tuple[str, int, int, int], ...]:
    return tuple((c.file, c.line, c.line, c.line) for c in calls)


def ignored_returns(package: Package) -> list[Finding]:
    """A function that returns a value, and every call of it, in the package and its tests, drops it."""
    names = collections.Counter(fn.name for fn in package.functions.values())
    handed = _handed(package)
    calls = _calls_named(package)
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if not _checkable(package, sid, fn, names, handed) or not fn.returns_value:
            continue
        sites = calls.get(fn.name, [])
        if not sites or not all(call.dropped for call in sites):
            continue
        where = _sites(sites)
        found.append(Finding("ignored_return", sid, f"Returns a value, and all {len(sites)} of its calls drop it.", f"Return nothing from `{short(sid)}`, or use what it returns.", sites=where))
    return found


def unused_defaults(package: Package) -> list[Finding]:
    """A parameter with a default that no call, in the package or its tests, ever gives: generality nobody asked for."""
    names = collections.Counter(fn.name for fn in package.functions.values())
    handed = _handed(package)
    calls = _calls_named(package)
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if not _checkable(package, sid, fn, names, handed):
            continue
        sites = calls.get(fn.name, [])
        # A call that spreads *args or **kwargs may give anything: nothing can be said.
        if not sites or any(c.spreads for c in sites):
            continue
        for name, default in fn.defaults:
            if any(call.given(name, fn.positional) is not None for call in sites):
                continue
            found.append(Finding("unused_default", sid, f"No call gives `{name}`: all {len(sites)} take the default, `{default}`.", f"Drop `{name}` from `{short(sid)}` and use `{default}` inside.", key=name, sites=_sites(sites)))
    return found


def unused_params(package: Package) -> list[Finding]:
    """A parameter the function never reads, when the function is ours alone to change."""
    names = collections.Counter(fn.name for fn in package.functions.values())
    handed = _handed(package)
    calls = _calls_named(package)
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if not _checkable(package, sid, fn, names, handed) or fn.stub:
            continue
        where = _sites(calls.get(fn.name, []))
        # With no call of it in sight, it may be a callback whose signature somebody else sets.
        if not where:
            continue
        for name in fn.params:
            if name.startswith("_") or fn.loads[name]:
                continue
            found.append(Finding("unused_param", sid, f"Never reads `{name}`.", f"Drop `{name}` from `{short(sid)}` and from its calls.", key=name, sites=where))
    return found


def scattered_apis(package: Package) -> list[Finding]:
    """A library function called raw from many modules in several folders: there is no module of ours in front of it."""
    sites: dict[str, dict[str, list[int]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for call in package.facts.library_calls:
        # An exception raised, or a value built to be returned, is the library's vocabulary, not its work.
        if call.api.split(".")[-1] in SETUP_CALLS or call.raised_or_returned:
            continue
        sites[call.api][call.module].append(call.line)
    found: list[Finding] = []
    for api, where in sorted(sites.items()):
        folders = {folder(m) for m in where}
        if len(where) < SCATTERED_MODULES or len(folders) < SCATTERED_FOLDERS:
            continue
        first = sorted(where)[0]
        spread = tuple((package.file_of(m), lines[0], lines[0], lines[0]) for m, lines in sorted(where.items()))
        found.append(Finding("scattered_api", first, f"`{api}` is called raw from {len(where)} modules in {len(folders)} folders.", f"Put `{api}` behind one function of ours, and call that.", key=api, line=spread[0][1], sites=spread))
    return found


def unstable_dependencies(package: Package) -> list[Finding]:
    """A module many depend on, depending on one that changes for more reasons than it does (Martin's instability)."""
    fan_in = collections.Counter(other for module in package.modules.values() for other in module.imports)
    instability = {
        name: len(m.imports) / (len(m.imports) + fan_in[name]) if m.imports or fan_in[name] else 0.0
        for name, m in package.modules.items()
    }
    found: list[Finding] = []
    for name, module in package.modules.items():
        if fan_in[name] < UNSTABLE_FAN_IN:
            continue
        worst = max(module.imports, key=lambda o: instability[o], default=None)
        if worst is None or instability[worst] - instability[name] < UNSTABLE_MARGIN:
            continue
        found.append(Finding(
            "unstable_dependency", name,
            f"{fan_in[name]} modules lean on it (instability {instability[name]:.2f}), and it leans on `{worst}` ({instability[worst]:.2f}), which imports {len(package.modules[worst].imports)}.",
            f"Depend on something as stable as `{name}`: move what it needs of `{worst}` down, or invert the dependency.",
            (worst,), key=worst, line=module.imports[worst],
        ))
    return found


def feature_envy(package: Package) -> list[Finding]:
    """A method that reads one other object's attributes far more than its own, when that object is a class
    of ours with behaviour: reading a record's fields is what records are for."""
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        symbol = package.symbols.get(sid)
        owner = package.facts.classes.get(symbol.owner or "") if symbol else None
        if not symbol or not symbol.owner or is_dunder(sid) or (owner and owner.library_base):
            continue
        reads = fn.receiver_reads
        own = reads["self"]
        others = [
            (p, reads[p], cls) for p in fn.params
            if reads[p] >= ENVY_READS and reads[p] > 2 * own and (cls := _behaving_class(package, fn.annotations.get(p)))
        ]
        if not others:
            continue
        envied, count, cls = max(others, key=lambda x: x[1])
        found.append(Finding("feature_envy", sid, f"Reads {count} attributes of `{envied}`, a `{short(cls)}`, and {own} of its own.", f"Move `{short(sid)}` to `{short(cls)}`, or give `{short(cls)}` the method it wants.", (cls,), key=envied))
    return found


def _behaving_class(package: Package, name: str | None) -> str | None:
    """The class of ours an annotation names, when it is one and has methods of its own and no library base."""
    matches = [sid for sid, s in package.symbols.items() if s.kind == "class" and s.name == name]
    cls = package.facts.classes.get(matches[0]) if len(matches) == 1 else None
    if cls is None or cls.library_base:
        return None
    methods = [s for s in package.symbols.values() if s.owner == matches[0] and s.kind == "method" and not is_dunder(s.id)]
    return matches[0] if methods else None


def single_implementations(package: Package) -> list[Finding]:
    """An abstract class of ours with exactly one class of ours implementing it: an abstraction nothing varies."""
    by_name = {s.name: sid for sid, s in reversed(list(package.symbols.items())) if s.kind == "class"}
    children: dict[str, list[str]] = collections.defaultdict(list)
    for sid, cls in package.facts.classes.items():
        for base in cls.bases:
            if parent := by_name.get(base):
                children[parent].append(sid)
    found: list[Finding] = []
    for parent, kids in children.items():
        cls = package.facts.classes.get(parent)
        # An abstract class that is a door is there for others to implement.
        if cls is None or len(kids) != 1 or not cls.abstract or parent in package.entrypoints:
            continue
        found.append(Finding("single_implementation", parent, f"Abstract, and only `{short(kids[0])}` implements it.", f"Fold `{short(parent)}` into `{short(kids[0])}` until a second one exists.", (kids[0],)))
    return found
