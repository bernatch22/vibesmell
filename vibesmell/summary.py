"""The whole report as one Markdown text an agent can read and act on."""

from vibesmell.checks import CHECKS
from vibesmell.finding import Finding
from vibesmell.graph import Package
from vibesmell.project import Project, Skip
from vibesmell.score import MOST_HOPS, score

# The deepest doors worth naming: past these the list is the page's job.
DOORS_NAMED = 8


def summary(package: Package, found: list[Finding], excused: list[tuple[Skip, list[Finding]]], project: Project) -> str:
    """The score, every finding under its check's note, the skips, the deepest doors, and how to check again."""
    card = score(package, found)
    lines = [f"# vibesmell: {package.name}", ""]
    lines.append(f"{len(package.modules)} modules, {sum(1 for s in package.symbols.values() if s.kind in ('function', 'method'))} functions. Score **{card['overall']}** of 100.")
    lines.append("")
    for a in card["attributes"]:
        lines.append(f"- {a['label']} {a['value']}: {a['detail']}")
    lines.append("")
    if not found:
        lines.append("Nothing to fix: every check passed.")
    else:
        lines.append(f"## {len(found)} to fix")
        lines.append("")
        lines.append("Each check below says what it looks for. A finding you believe is wrong: say which and why, do not work around it.")
        for check, (title, note) in CHECKS.items():
            of_check = [f for f in found if f.check == check]
            if not of_check:
                continue
            lines += ["", f"### {title} ({len(of_check)})", "", note, ""]
            for f in of_check:
                row = f.row(package)
                lines.append(f"- `{row['file']}:{row['line']}` {f.message}")
                lines.append(f"  Fix: {f.fix}")
    hidden = [(skip, fs) for skip, fs in excused if fs]
    if hidden:
        lines += ["", "## Skipped by the project's rules", ""]
        lines += [f"- {len(fs)} in `{skip.path}`: {skip.why}" for skip, fs in hidden]
    deep = [h for h in package.hops if int(str(h["hops"])) > MOST_HOPS]
    if deep:
        lines += ["", f"## Deepest doors ({len(deep)} of {len(package.hops)} over {MOST_HOPS} hops)", ""]
        lines.append("The longest chain of the package's own calls under each entrypoint. Tunnels and forwards on these paths are the first to cut.")
        lines.append("")
        for h in deep[:DOORS_NAMED]:
            path = [str(p).split(":", 1)[1] for p in h["path"]]  # type: ignore[union-attr]
            lines.append(f"- {h['hops']} hops: {' → '.join(path)}")
    if project.layers:
        lines += ["", f"Layers, bottom up: {' < '.join(project.layers)}."]
    lines += ["", "Check again with `vibesmell check`; it exits 1 while anything is left.", ""]
    return "\n".join(lines)
