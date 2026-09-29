"""The one place a project is read: by the reader of its language."""

import dataclasses

from vibecheck import graph, ts_reader
from vibecheck.graph import Package
from vibecheck.project import Project


def package_of(project: Project) -> Package:
    """The project's package, read whole by Python's reader or TypeScript's; a library's public names are doors."""
    if project.language == "typescript":
        package = ts_reader.read(project.package_dir, project.keep, project.sinks)
    else:
        package = graph.read(project.package_dir, project.entries, project.keep, project.sinks)
    return _as_library(package) if project.library else package


def _as_library(package: Package) -> Package:
    """Every public name, in a module no part of whose path is private, and every public method of a public class:
    the code that calls them is in the projects that use the library, which no reader of this one can see."""
    def public(sid: str) -> bool:
        symbol = package.symbols[sid]
        if symbol.private or any(part.startswith("_") for part in symbol.module.split(".")):
            return False
        return symbol.owner is None or public(symbol.owner)

    return dataclasses.replace(package, entrypoints=package.entrypoints | {sid for sid in package.symbols if public(sid)})
