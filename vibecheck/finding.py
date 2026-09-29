"""A finding: one symbol, what is wrong with it, and the fix."""

from dataclasses import dataclass

from vibecheck.graph import Package


@dataclass(frozen=True)
class Finding:
    """One thing to fix, with the words to fix it."""

    check: str
    symbol: str
    message: str
    fix: str
    related: tuple[str, ...] = ()
    key: str = ""
    # Set when the finding is a module's, not a symbol's: `symbol` is then the module's name.
    line: int = 0
    # Places the finding is about beyond its symbols, as (file, line, from, to): a constant's call sites, the tests.
    sites: tuple[tuple[str, int, int, int], ...] = ()
    # The commits a finding read from git, as (hash, subject), newest first: a shotgun's.
    commits: tuple[tuple[str, str], ...] = ()

    def module(self) -> str:
        return self.symbol.split(":")[0]

    def row(self, package: Package) -> dict[str, object]:
        """The finding as the CLI prints it."""
        symbol = package.symbols.get(self.symbol)
        return {
            "id": f"{self.check}:{self.symbol}" + (f":{self.key}" if self.key else ""),
            "check": self.check,
            "symbol": self.symbol,
            "file": package.file_of(self.module()),
            "line": symbol.line if symbol else self.line,
            "message": self.message,
            "fix": self.fix,
            "related": list(self.related),
            "key": self.key,
            "sites": [{"file": file, "line": line, "from": start, "to": end} for file, line, start, end in self.sites],
            "commits": [{"hash": h, "subject": subject} for h, subject in self.commits],
        }
