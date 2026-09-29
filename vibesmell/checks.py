"""The checks: every finding names one symbol, says what is wrong, and says the fix.

Only checks that are right nearly every time live here. A finding is never accepted away: when
one is wrong, the check is wrong, and the check gets fixed.
"""

import collections
import fnmatch
import itertools

from vibesmell.facts import Arg, Call
from vibesmell.finding import Finding
from vibesmell.graph import Package
from vibesmell.nodes import folder, is_dunder, short
from vibesmell.project import Project, Skip
from vibesmell.smells import duplicates, feature_envy, ignored_returns, scattered_apis, single_implementations, unstable_dependencies, unused_defaults, unused_params
from vibesmell.structure import cycles, forbidden, hubs, layers, shotgun

# A value handed through two functions is composition; through three it is carried for nothing.
TUNNEL_CROSSINGS = 3
# This many parameters seen together in this many signatures are one object.
CLUMP_SIZE = 3
CLUMP_FUNCTIONS = 3
# Names that mean nothing on their own, never part of a clump.
CLUMP_NEVER = frozenset({"args", "kwargs", "self", "cls"})

# check -> (title, what it means), in the order they are printed.
CHECKS: dict[str, tuple[str, str]] = {
    "dead": ("Dead code", "Nothing in the package or its tests names it."),
    "forward": ("Forwards", "One statement handing its own arguments to another function of the package: a layer with no work in it."),
    "tunnel": ("Tunnels", "A parameter that crosses three or more functions without being read: the first should hand it to the last."),
    "private_abroad": ("Private used abroad", "A _name used from another module: make it public or move it."),
    "bool_flag": ("Boolean flags", "A bool parameter tested first, and handed a literal by a caller: two functions."),
    "cycle": ("Import cycles", "Modules that import each other, lazily or not: neither can be read alone."),
    "layer": ("Layers crossed", "A folder importing a folder above it in `layers`."),
    "forbid": ("Forbidden imports", "A folder importing a name `forbid` says it may not."),
    "clump": ("Data clumps", "Three or more parameters that travel together through three or more functions: an object nobody wrote."),
    "constant_param": ("Constant parameters", "A parameter every caller gives the same literal: a constant with a seat in the signature."),
    "hub": ("Hubs", "A module many import, that imports many, and whose importers each use a sliver of it: several modules in one."),
    "shotgun": ("Shotgun surgery", "Two files in different folders that git nearly always commits together: one thing in two places."),
    "tests_only": ("Used by tests only", "The package never uses it; only its tests do. Waiting for its caller, or dead with a test."),
    "home_only_public": ("Public, used at home only", "No other module, no test and no framework names it: a _name says so."),
    "unread_field": ("Unread fields", "A field no code reads, matches on, or serializes: the record carries it for nothing."),
    "duplicate": ("Duplicate bodies", "Two or more functions with one body, locals renamed alike: the copy code written fast leaves most."),
    "ignored_return": ("Ignored returns", "A function that returns a value that every call drops: the value is work for nobody."),
    "unused_default": ("Defaults nobody overrides", "A parameter with a default that no call gives: generality nobody asked for."),
    "unused_param": ("Unused parameters", "A parameter the function never reads, that its callers still pass."),
    "scattered_api": ("Scattered library calls", "A library called raw from many modules in several folders: no module of ours stands in front of it."),
    "unstable_dependency": ("Unstable dependencies", "A module many depend on, depending on one that changes for more reasons: the stable leans on the unstable."),
    "feature_envy": ("Feature envy", "A method that reads another object's attributes far more than its own: it lives in the wrong class."),
    "single_implementation": ("Single implementations", "An abstract class with one implementation: an abstraction nothing varies."),
    "twin_shapes": ("Twin shapes", "Two records of the package's own, in one folder, with the same fields, neither a tag: one of them."),
}
# Calls that read a record whole: every field of the argument is read.
SERIALIZERS = frozenset({"asdict", "astuple", "vars", "replace", "fields", "model_dump", "dict"})


def findings(package: Package, rules: Project) -> tuple[list[Finding], list[tuple[Skip, list[Finding]]]]:
    """Every finding of every check over the package, and the ones a skip excused, per skip."""
    ours = _own_calls(package)
    users = _users(package, ours)
    found: list[Finding] = []
    found += _dead(package, users)
    found += _forwards(package, ours, users)
    found += _tunnels(package, ours)
    found += _private_abroad(package, users)
    found += _bool_flags(package)
    found += cycles(package)
    found += layers(package, rules.layers)
    found += forbidden(package, rules.forbid or {})
    found += _clumps(package)
    found += _constant_params(package)
    found += hubs(package)
    found += shotgun(package)
    found += _home_only_public(package, users)
    found += _unread_fields(package)
    found += _twin_shapes(package)
    for smell in (duplicates, ignored_returns, unused_defaults, unused_params, scattered_apis, unstable_dependencies, feature_envy, single_implementations):
        found += smell(package)
    order = list(CHECKS)
    found.sort(key=lambda f: (order.index(f.check), f.module(), f.row(package)["line"]))
    kept: list[Finding] = []
    excused: list[tuple[Skip, list[Finding]]] = [(skip, []) for skip in rules.skips]
    for f in found:
        file = package.file_of(f.module())
        by = next((row for row in excused if _excuses(row[0], file, f.check)), None)
        (by[1] if by else kept).append(f)
    return kept, excused


def _excuses(skip: Skip, file: str, check: str) -> bool:
    return fnmatch.fnmatch(file, skip.path) and (not skip.checks or check in skip.checks)


def _own_calls(package: Package) -> dict[str, list[str]]:
    """For each function, the package's own functions and methods it calls."""
    return {
        sid: sorted(c for c in package.symbols[sid].calls if package.symbols[c].kind in ("function", "method"))
        for sid in package.functions
        if sid in package.symbols
    }


def _users(package: Package, ours: dict[str, list[str]]) -> dict[str, set[str]]:
    """Every scope that names or calls each symbol."""
    callers: dict[str, set[str]] = collections.defaultdict(set)
    for sid, called in ours.items():
        for callee in called:
            callers[callee].add(sid)
    return {sid: set(s.used_by) | callers[sid] for sid, s in package.symbols.items()}


def _dead(package: Package, users: dict[str, set[str]]) -> list[Finding]:
    """A symbol nobody names: not the package, not its tests, not by any bare name anywhere."""
    found: list[Finding] = []
    for sid, symbol in package.symbols.items():
        if users[sid] or sid in package.entrypoints or is_dunder(sid):
            continue
        if short(sid) in package.keep or symbol.name in package.keep:
            continue
        bare = symbol.name.split(".")[-1]
        # Resolution may miss a use through an untyped receiver, a framework or a test: a name
        # read anywhere is not dead. Its own definition reads it once when it is a constant's target.
        if package.read_names[bare] > 0:
            continue
        if package.tested_names[bare] > 0:
            if symbol.kind in ("function", "method", "class"):
                uses = package.test_uses.get(bare, [])
                files = len({u[0] for u in uses})
                where = f" ({files} test file{'s' if files > 1 else ''})" if uses else ""
                found.append(Finding("tests_only", sid, f"Only the tests use it{where}.", f"Delete `{short(sid)}` and its test, or give it the caller it waits for.", sites=tuple(uses[:12])))
            continue
        if symbol.kind in ("function", "method"):
            fn = package.functions[sid]
            # A decorator registers the function somewhere we cannot see; `main` is the program.
            if fn.decorated or fn.name == "main":
                continue
        owner = package.facts.classes.get(symbol.owner or "")
        if symbol.kind == "method" and owner and owner.library_base:
            continue
        cls = package.facts.classes.get(sid)
        if symbol.kind == "class" and cls and cls.decorated and cls.library_base:
            continue
        what = {"class": "instantiates or names", "function": "calls", "method": "calls", "constant": "reads", "type": "uses"}[symbol.kind]
        found.append(Finding("dead", sid, f"Nothing {what} it.", f"Delete `{short(sid)}`."))
    return found


def _forwards(package: Package, ours: dict[str, list[str]], users: dict[str, set[str]]) -> list[Finding]:
    """A function whose whole body is one call handing every one of its parameters on."""
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if sid in package.entrypoints or is_dunder(sid) or len(ours.get(sid, [])) != 1:
            continue
        # A method that overrides one of a base keeps an interface: forwarding is how it does so.
        if _overrides(package, sid):
            continue
        call, own = fn.sole_call, fn.params
        # A call that spreads `*args` hands on what nobody can see: an adapter, not a forward.
        if call is None or call.spreads or not own or not _hands_over(call, set(own)) or not _passes_all(call, own):
            continue
        target = ours[sid][0]
        count = len(users[sid])
        who = "nothing calls it either" if count == 0 else ("its one user calls" if count == 1 else f"its {count} users call") + f" `{short(target)}` directly"
        found.append(Finding("forward", sid, f"Forwards its arguments to `{short(target)}`.", f"Delete `{short(sid)}`; {who}.", (target,)))
    return found


def _overrides(package: Package, sid: str) -> bool:
    """Whether a method's class has a base that is a library's, or a base of ours with a method of that name."""
    symbol = package.symbols[sid]
    owner = package.facts.classes.get(symbol.owner or "")
    if owner is None:
        return False
    if owner.library_base:
        return True
    name = symbol.name.split(".")[-1]
    bases = {s.id for s in package.symbols.values() if s.kind == "class" and s.name in owner.bases}
    return any(package.symbols.get(f"{base.split(':')[0]}:{base.split(':')[1]}.{name}") or _overrides_in(package, base, name) for base in bases)


def _overrides_in(package: Package, cls: str, name: str) -> bool:
    """Whether a class of ours, or one of its bases, defines a method of this name."""
    facts = package.facts.classes.get(cls)
    if facts is None:
        return False
    if f"{cls}.{name}" in package.symbols:
        return True
    return any(_overrides_in(package, s.id, name) for s in package.symbols.values() if s.kind == "class" and s.name in facts.bases and s.id != cls)


def _arguments(call: Call) -> list[Arg]:
    return [*call.args, *(arg for _, arg in call.keywords)]


def _passes_all(call: Call, own: tuple[str, ...]) -> bool:
    """Whether the call hands on every parameter: one that drops some is an adapter, not a forward."""
    return set(own) <= {name for a in _arguments(call) for name in a.names}


def _hands_over(call: Call, own: set[str]) -> bool:
    """Whether every argument is a parameter as it came, a constant, or an attribute of one."""
    return all(a.literal or a.name in own or (a.attr_of is not None and a.attr_of in own | {"self"}) for a in _arguments(call))


def _tunnels(package: Package, ours: dict[str, list[str]]) -> list[Finding]:
    """A parameter only ever handed on, through this function and the next two: named by its name."""
    tunneled: dict[str, set[str]] = {}
    for sid, fn in package.functions.items():
        if is_dunder(sid) or not ours.get(sid):
            continue
        # Handed to one call and nothing else: passed to two or more, it is what the function works on.
        tunneled[sid] = {p for p in fn.relayed if not p.startswith("_")}
    found: list[Finding] = []
    for sid, handed in tunneled.items():
        for p in sorted(handed):
            path = [sid]
            while True:
                onward = [c for c in ours.get(path[-1], []) if c in tunneled and p in tunneled[c] and c not in path]
                if not onward:
                    break
                path.append(onward[0])
            if len(path) < TUNNEL_CROSSINGS:
                continue
            last = [c for c in ours.get(path[-1], []) if c in package.functions and p in package.functions[c].params]
            if not last:
                continue
            end = short(last[0])
            crossed = " → ".join(short(s) for s in path)
            fix = f"Let the caller of `{short(sid)}` hand `{p}` to `{end}`, or keep `{p}` on the object `{short(sid)}` already holds."
            # The related are the crossing and then the function that reads it: the whole way the value goes.
            found.append(Finding("tunnel", sid, f"`{p}` crosses {crossed} without being read, on its way to `{end}`.", fix, (*path[1:], last[0]), key=p))
    # A tunnel is reported once, where the parameter enters: not again at each function along it.
    midway = {(r, f.key) for f in found for r in f.related}
    entering = [f for f in found if (f.symbol, f.key) not in midway]
    # One value carried to one place by many ways in is one tunnel: told once, at its first way in.
    ways: dict[tuple[str, str], list[Finding]] = collections.defaultdict(list)
    for f in entering:
        ways[(f.key, f.related[-1])].append(f)
    told: list[Finding] = []
    for (param, end), each in ways.items():
        first, *others = sorted(each, key=lambda f: (package.symbols[f.symbol].module, package.symbols[f.symbol].line))
        if not others:
            told.append(first)
            continue
        also = ", ".join(short(f.symbol) for f in others[:5]) + (f" and {len(others) - 5} more" if len(others) > 5 else "")
        message = f"{first.message[:-1]}; {len(others)} more ways in carry it the same: {also}."
        fix = f"`{param}` is threaded through {len(each)} ways in only to reach `{short(end)}`: keep it on one object they all hold, or let `{short(end)}` find it."
        told.append(Finding("tunnel", first.symbol, message, fix, first.related, key=param))
    return told


def _private_abroad(package: Package, users: dict[str, set[str]]) -> list[Finding]:
    """A _name used from a module that is not its own."""
    found: list[Finding] = []
    for sid, symbol in package.symbols.items():
        if not symbol.private or is_dunder(sid) or not users[sid]:
            continue
        abroad = sorted({u.split(":")[0] for u in users[sid]} - {symbol.module})
        if abroad:
            found.append(Finding("private_abroad", sid, f"Used from {', '.join(abroad[:3])}.", f"Drop the underscore of `{short(sid)}`, or move it next to its users.", tuple(sorted(users[sid])[:5])))
    return found


def _bool_flags(package: Package) -> list[Finding]:
    """A bool parameter tested by the first statement, that some caller sets with a literal: two functions.

    A caller writing `f(x, fast=True)` chooses a mode, and there are two functions in one. A caller
    handing a variable it holds is passing a fact; splitting would only move its `if`.
    """
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if sid in package.entrypoints or is_dunder(sid) or fn.first_if is None:
            continue
        flag, start, end = fn.first_if
        if flag not in fn.flags:
            continue
        # Every call of a function of this name, in the package, that gives the flag as True or False.
        literal = [
            (c.file, c.line, c.line, c.line) for c in package.facts.calls
            if not c.in_tests and c.name == fn.name and (given := c.given(flag, fn.positional)) and given.boolean
        ]
        if not literal:
            continue
        # The first site is the `if` itself, where the function splits; the rest are the callers that pick a side.
        split = (package.file_of(package.symbols[sid].module), start, start, end)
        found.append(Finding("bool_flag", sid, f"`{flag}` picks the branch before anything else, and a caller sets it by hand.", f"Split `{short(sid)}` into two functions, one per branch.", key=flag, sites=(split, *literal)))
    return found


def _clumps(package: Package) -> list[Finding]:
    """Parameters handed on together, by name, from function to function: three names, three functions or more.

    Sharing a signature is not enough: route handlers all take the same doors. A clump is a group a
    caller passes whole to a callee that passes it whole again: some function in the middle only relays it.
    """
    names_of = {sid: set(fn.params) - CLUMP_NEVER for sid, fn in package.functions.items()}
    edges: dict[frozenset[str], list[tuple[str, str]]] = collections.defaultdict(list)
    for sid, fn in package.functions.items():
        if is_dunder(sid):
            continue
        for call in fn.calls:
            callee = _callee_of(package, sid, call)
            if callee is None or callee == sid:
                continue
            handed = {p for p in _handed_by_name(call) if p in names_of[sid] and p in names_of[callee] and not p.startswith("_")}
            if len(handed) < CLUMP_SIZE:
                continue
            for size in range(CLUMP_SIZE, len(handed) + 1):
                for group in itertools.combinations(sorted(handed), size):
                    edges[frozenset(group)].append((sid, callee))
    clumps: dict[tuple[frozenset[str], frozenset[str]], None] = {}
    for names, pairs in edges.items():
        relays = {callee for _, callee in pairs} & {caller for caller, _ in pairs}
        for members in _components(pairs):
            if len(members) >= CLUMP_FUNCTIONS and members & relays:
                clumps[(names, frozenset(members))] = None
    maximal = [
        (names, sids) for names, sids in clumps
        if not any(names < other and sids <= others for other, others in clumps if (other, others) != (names, sids))
    ]
    found: list[Finding] = []
    for names, sids in maximal:
        ordered = sorted(sids, key=lambda s: (package.symbols[s].module, package.symbols[s].line))
        first = ordered[0]
        listed = ", ".join(short(s) for s in ordered[:6]) + (f" and {len(ordered) - 6} more" if len(ordered) > 6 else "")
        together = ", ".join(sorted(names))
        found.append(Finding("clump", first, f"`{together}` are handed on together through {len(ordered)} functions: {listed}.", f"Make `{together}` one object and hand that on instead.", tuple(ordered[1:]), key="+".join(sorted(names))))
    return found


def _callee_of(package: Package, caller: str, call: Call) -> str | None:
    """Which function of ours a call reaches, when the caller's resolved calls name exactly one of that name."""
    if not call.name or caller not in package.symbols:
        return None
    matches = [c for c in package.symbols[caller].calls if c in package.functions and c.split(".")[-1].split(":")[-1] == call.name]
    return matches[0] if len(matches) == 1 else None


def _handed_by_name(call: Call) -> set[str]:
    """The parameter names a call passes through as they came: `f(x, y=y)` hands on x and y."""
    return {a.name for a in call.args if a.name} | {k for k, a in call.keywords if a.name == k}


def _components(pairs: list[tuple[str, str]]) -> list[set[str]]:
    """The connected sets of a list of edges."""
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[root(a)] = root(b)
    groups: dict[str, set[str]] = collections.defaultdict(set)
    for x in parent:
        groups[root(x)].add(x)
    return list(groups.values())


def _constant_params(package: Package) -> list[Finding]:
    """A parameter that every call in the package sets to one same literal; the function's name must be unique."""
    by_name: collections.Counter[str] = collections.Counter(fn.name for fn in package.functions.values())
    calls_by_name: dict[str, list[Call]] = collections.defaultdict(list)
    for call in package.facts.calls:
        if call.name and not call.in_tests:
            calls_by_name[call.name].append(call)
    found: list[Finding] = []
    for sid, fn in package.functions.items():
        if sid in package.entrypoints or is_dunder(sid) or by_name[fn.name] > 1:
            continue
        calls = calls_by_name.get(fn.name, [])
        if len(calls) < 2:
            continue
        for param in fn.params:
            given = [call.given(param, fn.positional) for call in calls]
            # Every call gives it, every one as a literal, and all of them the same literal.
            if not all(g is not None and g.literal for g in given) or len({g.value for g in given if g}) != 1:
                continue
            literal = given[0].text  # type: ignore[union-attr]
            sites = tuple((call.file, call.line, call.line, call.line) for call in calls)
            found.append(Finding("constant_param", sid, f"Every one of its {len(calls)} callers passes `{param}={literal}`.", f"Drop `{param}` from `{short(sid)}` and use `{literal}` inside.", (), key=f"{param}={literal}", sites=sites))
    return found


def _home_only_public(package: Package, users: dict[str, set[str]]) -> list[Finding]:
    """A public function only its own module calls: not another module, not a test, not a framework it is handed to."""
    found: list[Finding] = []
    for sid, symbol in package.symbols.items():
        if symbol.private or symbol.kind != "function" or not users[sid] or sid in package.entrypoints:
            continue
        if package.functions[sid].decorated or symbol.name in package.keep:
            continue
        if {u.split(":")[0] for u in users[sid]} - {symbol.module}:
            continue
        # A test imports it by its public name; a framework is given it as a value: both need it public.
        if package.tested_names[symbol.name] > 0 or (symbol.module, symbol.name) in package.facts.handed:
            continue
        # In Python a leading underscore says "this file only"; in TypeScript, not exporting it does.
        fix = f"Stop exporting `{symbol.name}`." if package.language == "typescript" else f"Rename it `_{symbol.name}`."
        found.append(Finding("home_only_public", sid, "Only its own module calls it.", fix))
    return found


def _unread_fields(package: Package) -> list[Finding]:
    """A field of the package's own record that nothing reads as `.name`, matches as `Cls(name=...)`, or reads whole."""
    facts = package.facts
    whole = {module for module, called in facts.called_names.items() if called & SERIALIZERS}
    found: list[Finding] = []
    for sid, symbol in package.symbols.items():
        cls = facts.classes.get(sid)
        # A class that is a door is read by whoever holds it: its fields are what it offers.
        if symbol.kind != "class" or cls is None or cls.library_base or sid in package.entrypoints:
            continue
        # A module that serializes records reads every field of the records it holds: the wire does.
        if symbol.module in whole:
            continue
        for name, _ in cls.fields:
            if name.startswith("_") or name in facts.attribute_reads or name in facts.matched_fields:
                continue
            found.append(Finding("unread_field", sid, f"No code reads `.{name}`.", f"Drop `{name}` from `{symbol.name}`.", key=name))
    return found


def _twin_shapes(package: Package) -> list[Finding]:
    """Two records of our own, in one folder, with the same fields, when neither is a tag in a registry.

    A class with a library base is a contract with the outside (a wire model, an ORM row): its shape
    is not ours to merge. A class listed in a dict, list or tuple, or matched by `case Cls(...)`, is a
    tag: its name is what tells it apart.
    """
    shapes: dict[tuple[str, frozenset[tuple[str, str]]], list[str]] = collections.defaultdict(list)
    for sid, symbol in package.symbols.items():
        cls = package.facts.classes.get(sid)
        if symbol.kind != "class" or cls is None or cls.library_base or symbol.name in package.facts.listed_names:
            continue
        if len(cls.fields) >= 2:
            shapes[(folder(symbol.module), frozenset(cls.fields))].append(sid)
    found: list[Finding] = []
    for members in shapes.values():
        first, *rest = members
        for sid in rest:
            label = short(first) if short(first) != short(sid) else first.replace(":", ".")
            found.append(Finding("twin_shapes", sid, f"Same fields as `{label}`.", f"Keep one of `{label}` and `{short(sid)}`.", (first,)))
    return found
