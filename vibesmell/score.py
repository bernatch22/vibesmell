"""The package's score: six attributes from 0 to 100, like a player card, and their average."""

from dataclasses import dataclass

from vibesmell.finding import Finding
from vibesmell.graph import Package

# The rule the hops are measured against, and the hops at which an entrypoint scores nothing.
MOST_HOPS = 3
HOPELESS_HOPS = 12
# A function this long, or a module this long, is one thing too many.
LONG_FUNCTION = 50
LONG_MODULE = 500


@dataclass(frozen=True)
class Attribute:
    """One axis of the card: its value, what it counts, and the checks behind it."""

    key: str
    label: str
    value: int
    detail: str
    checks: tuple[str, ...] = ()

    def row(self) -> dict[str, object]:
        """The attribute as the CLI prints it."""
        return {"key": self.key, "label": self.label, "value": self.value, "detail": self.detail, "checks": list(self.checks)}


def score(package: Package, findings: list[Finding]) -> dict[str, object]:
    """The six attributes of the package and the overall, each from 0 to 100."""
    count: dict[str, int] = {}
    for f in findings:
        count[f.check] = count.get(f.check, 0) + 1
    functions = [s for s in package.symbols.values() if s.kind in ("function", "method")]
    named = [s for s in package.symbols.values() if s.kind != "method"]
    public = [s for s in package.symbols.values() if not s.private]
    classes = [s for s in package.symbols.values() if s.kind == "class"]
    attributes = [
        _direct(package.hops),
        _rate("alive", "Alive", count.get("dead", 0) + count.get("tests_only", 0) // 2, len(named) + len(functions) // 2, 0.15, "{n} dead of {of} symbols, tests-only counted half", ("dead", "tests_only")),
        _rate(
            "lean", "Lean",
            count.get("forward", 0) * 2 + count.get("tunnel", 0) + count.get("duplicate", 0) * 2
            + count.get("ignored_return", 0) + count.get("unused_param", 0) + count.get("unused_default", 0),
            len(functions), 0.2, "{n} pieces of work for nobody in {of} functions",
            ("forward", "tunnel", "duplicate", "ignored_return", "unused_param", "unused_default"),
        ),
        _rate("private", "Private", count.get("private_abroad", 0) * 2 + count.get("home_only_public", 0), len(public), 0.3, "{n} names in the wrong place of {of} public", ("private_abroad", "home_only_public")),
        _rate("shaped", "Shaped", count.get("bool_flag", 0) * 2 + count.get("twin_shapes", 0) + count.get("unread_field", 0) + count.get("clump", 0) * 2
            + count.get("feature_envy", 0) + count.get("single_implementation", 0) * 2,
            max(1, len(classes) + len(functions) // 10), 0.5, "{n} misshapen records and functions",
            ("bool_flag", "twin_shapes", "unread_field", "clump", "feature_envy", "single_implementation")),
        _focused(package, count),
    ]
    overall = round(sum(a.value for a in attributes) / len(attributes))
    return {"overall": overall, "attributes": [a.row() for a in attributes]}


def _direct(hops: list[dict[str, object]]) -> Attribute:
    if not hops:
        return Attribute("direct", "Direct", 100, "no entrypoints to measure")
    depths = [int(str(h["hops"])) for h in hops]
    each = [1.0 if d <= MOST_HOPS else max(0.0, 1 - (d - MOST_HOPS) / (HOPELESS_HOPS - MOST_HOPS)) for d in depths]
    over = sum(1 for d in depths if d > MOST_HOPS)
    value = round(100 * sum(each) / len(each))
    return Attribute("direct", "Direct", value, f"{over} of {len(depths)} entrypoints over {MOST_HOPS} hops, deepest {max(depths)}")


def _rate(key: str, label: str, n: int, of: int, zero_at: float, detail: str, checks: tuple[str, ...]) -> Attribute:
    """100 with nothing wrong, 0 when the wrong ones reach `zero_at` of what they are counted in."""
    rate = n / of if of else 0.0
    value = round(100 * max(0.0, 1 - rate / zero_at))
    return Attribute(key, label, value, detail.format(n=n, of=of), checks)


def _focused(package: Package, count: dict[str, int]) -> Attribute:
    """Short functions, short modules, and modules that keep to themselves: no hub, no ring, no raw library everywhere."""
    functions = [s for s in package.symbols.values() if s.kind in ("function", "method")]
    long_functions = sum(1 for f in functions if f.lines > LONG_FUNCTION)
    long_modules = sum(1 for m in package.modules.values() if m.lines > LONG_MODULE)
    function_part = max(0.0, 1 - (long_functions / len(functions) if functions else 0) / 0.1)
    module_part = max(0.0, 1 - (long_modules / len(package.modules) if package.modules else 0) / 0.4)
    tangles = count.get("hub", 0) * 2 + count.get("cycle", 0) * 3 + count.get("shotgun", 0) + count.get("scattered_api", 0) + count.get("unstable_dependency", 0)
    tangle_part = max(0.0, 1 - (tangles / len(package.modules) if package.modules else 0) / 0.3)
    value = round(100 * (function_part + module_part + tangle_part) / 3)
    detail = f"{long_functions} functions over {LONG_FUNCTION} lines, {long_modules} modules over {LONG_MODULE}, {tangles} tangles"
    return Attribute("focused", "Focused", value, detail, ("hub", "cycle", "shotgun", "scattered_api", "unstable_dependency"))
