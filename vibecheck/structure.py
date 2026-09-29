"""The checks of modules and folders: cycles, layers, forbidden imports, hubs, and what git says changes together."""

import statistics

from vibecheck.finding import Finding
from vibecheck.graph import Package
from vibecheck.nodes import folder

# A hub is imported by this many, imports this many, exports this many, and each importer uses this share.
HUB_DEGREE = 10
HUB_NAMES = 10
HUB_SLIVER = 0.2
# Two files are one thing when committed together this often, and in this share of the rarer one's commits.
SHOTGUN_TOGETHER = 5
SHOTGUN_SHARE = 0.75


def cycles(package: Package) -> list[Finding]:
    """Every set of modules that import each other, reported once, at its first module."""
    found: list[Finding] = []
    for members in _strongly_connected({name: set(m.imports) for name, m in package.modules.items()}):
        if len(members) < 2:
            continue
        first, *rest = sorted(members)
        back = next(m for m in rest if first in package.modules[m].imports) if any(first in package.modules[m].imports for m in rest) else rest[0]
        ring = ", ".join(rest)
        found.append(Finding("cycle", first, f"Imports {ring}, and is imported back.", f"Move what `{back}` needs of `{first}` below both, or merge them.", tuple(rest), line=package.modules[first].imports.get(rest[0], 1)))
    return found


def _strongly_connected(edges: dict[str, set[str]]) -> list[set[str]]:
    """Tarjan's components, iteratively: the sets of modules reachable from one another."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[set[str]] = []
    counter = 0
    for start in edges:
        if start in index:
            continue
        work = [(start, iter(edges[start]))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, kids = work[-1]
            kid = next(kids, None)
            if kid is not None:
                if kid not in index:
                    index[kid] = low[kid] = counter
                    counter += 1
                    stack.append(kid)
                    on_stack.add(kid)
                    work.append((kid, iter(edges.get(kid, set()))))
                elif kid in on_stack:
                    low[node] = min(low[node], index[kid])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                found.append(component)
    return found


def layers(package: Package, layers: tuple[str, ...]) -> list[Finding]:
    """A module of one layer importing a module of a layer above it."""
    rank = {folder: i for i, folder in enumerate(layers)}
    found: list[Finding] = []
    for name, module in package.modules.items():
        mine = rank.get(folder(name))
        if mine is None:
            continue
        for other, line in sorted(module.imports.items(), key=lambda kv: kv[1]):
            theirs = rank.get(folder(other))
            if theirs is not None and theirs > mine:
                up = theirs - mine
                found.append(Finding("layer", name, f"Imports `{other}`, {up} layer{'s' if up > 1 else ''} up.", f"Move what `{name}` needs into `{folder(name)}/` or below, or hand it in from above.", (other,), key=other, line=line))
    return found


def forbidden(package: Package, forbid: dict[str, tuple[str, ...]]) -> list[Finding]:
    """A module importing a name its folder may not: of the package, or from outside."""
    found: list[Finding] = []
    for name, module in package.modules.items():
        banned = forbid.get(folder(name), ())
        if not banned:
            continue
        imported = {**module.external, **{f"{package.name}.{m}": line for m, line in module.imports.items()}}
        for target, line in sorted(imported.items(), key=lambda kv: kv[1]):
            hit = next((b for b in banned if target == b or target.startswith(b + ".")), None)
            if hit:
                found.append(Finding("forbid", name, f"Imports `{target}`, which `{folder(name)}/` may not.", f"Take `{hit}` out of `{name}`, or out of the rule.", (), key=target, line=line))
    return found


def hubs(package: Package) -> list[Finding]:
    """A module in the middle of everything that is several modules: its importers each take a sliver."""
    found: list[Finding] = []
    for name, module in package.modules.items():
        importers = [other for other, m in package.modules.items() if name in m.imports]
        public = [s for s in package.symbols.values() if s.module == name and s.kind != "method" and not s.private]
        if len(importers) < HUB_DEGREE or len(module.imports) < HUB_DEGREE or len(public) < HUB_NAMES:
            continue
        # What an importer takes is what its `from hub import ...` names; a whole-module import, what it uses.
        used_by_each = [
            len(package.modules[other].taken.get(name) or {s.id for s in public if any(u.split(":")[0] == other for u in s.used_by)})
            for other in importers
        ]
        typical = statistics.median(used_by_each)
        if typical / len(public) > HUB_SLIVER:
            continue
        found.append(Finding("hub", name, f"Imported by {len(importers)} modules that use {typical:g} of its {len(public)} names each, and imports {len(module.imports)}.", f"Split `{name}` along what its importers use; each piece imports less.", tuple(sorted(importers)[:5]), line=1))
    return found


def shotgun(package: Package) -> list[Finding]:
    """Two files of different folders that git commits together nearly every time the rarer one changes."""
    found: list[Finding] = []
    for pair in package.co_changes:
        if pair.together < SHOTGUN_TOGETHER:
            continue
        rarer, other = (pair.a, pair.b) if pair.changes_a <= pair.changes_b else (pair.b, pair.a)
        changes = min(pair.changes_a, pair.changes_b)
        if pair.together / changes < SHOTGUN_SHARE:
            continue
        mine, theirs = package.module_of(rarer), package.module_of(other)
        if mine is None or theirs is None or folder(mine) == folder(theirs):
            continue
        commits = tuple((c.hash, c.subject) for c in pair.commits)
        found.append(Finding("shotgun", mine, f"Committed with `{theirs}` in {pair.together} of its {changes} changes.", "They are one thing in two folders: put them together, or cut the seam they share.", (theirs,), key=theirs, line=1, commits=commits))
    return found
