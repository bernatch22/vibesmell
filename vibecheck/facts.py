"""What the checks know of a package, in no language: each call, each function, each class.

A reader turns a language's syntax into these facts; the checks read only them. Python's reader is
python_facts.py; any other language needs a reader that fills the same shapes, and every check works.
"""

import collections
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Arg:
    """One argument a call gives, as far as the checks look at it."""

    # As written: `True`, `None`, `x`, `self.pool`.
    text: str
    # A constant: a number, a string, a boolean, None; `value` tells two constants apart.
    literal: bool = False
    value: str = ""
    boolean: bool = False
    # A bare name handed on as it came (`x`), or the name an attribute is read off (`x` of `x.y`).
    name: str | None = None
    attr_of: str | None = None
    # Every bare name read inside the argument.
    names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Call:
    """One call: the name it is made through, where, what it gives, and whether its value is kept."""

    name: str
    file: str
    line: int
    args: tuple[Arg, ...] = ()
    keywords: tuple[tuple[str, Arg], ...] = ()
    # It spreads `*args` or `**kwargs`: it may give anything.
    spreads: bool = False
    # Its value is let go: the call is a statement of its own.
    dropped: bool = False
    in_tests: bool = False

    def given(self, param: str, positional: tuple[str, ...]) -> Arg | None:
        """What it gives for a parameter, by keyword or by place; None when it leaves the default."""
        for name, arg in self.keywords:
            if name == param:
                return arg
        if param in positional and positional.index(param) < len(self.args):
            return self.args[positional.index(param)]
        return None


@dataclass(frozen=True)
class FunctionFacts:
    """A function or a method, as the checks see it."""

    name: str
    # Its parameters, `self` and `cls` left out; those a call can give by place; those with a default, as written.
    params: tuple[str, ...]
    positional: tuple[str, ...]
    defaults: tuple[tuple[str, str], ...]
    # Parameters that are booleans: annotated so, or defaulting to one.
    flags: frozenset[str]
    # Registered by a decorator somewhere the package cannot see.
    decorated: bool
    # A shape to fill: `...`, `pass`, or raising NotImplementedError.
    stub: bool
    returns_value: bool
    # How often each bare name is read, anywhere in it.
    loads: collections.Counter[str]
    # Parameters read exactly once, as an argument of a call: handed on, never looked at.
    relayed: frozenset[str]
    # When the whole body is one call, that call.
    sole_call: Call | None
    # When the body starts by testing a bare name: the name, and the lines of that test.
    first_if: tuple[str, int, int] | None
    # The body as a tree with its locals renamed: two copies share it; a small body has none.
    shape: str | None
    # How many attributes it reads off each name: `self`, a parameter, a local.
    receiver_reads: collections.Counter[str]
    # Each parameter's annotation, when it names one type.
    annotations: dict[str, str]
    # Every call made inside it.
    calls: tuple[Call, ...]


@dataclass(frozen=True)
class ClassFacts:
    """A class, as the checks see it."""

    # Its annotated fields, with the annotation as written.
    fields: tuple[tuple[str, str], ...]
    # The names of its bases, and whether any base, however far up, is not the package's.
    bases: tuple[str, ...]
    library_base: bool
    decorated: bool
    # It says it is abstract: an ABC base, or an abstract method.
    abstract: bool


@dataclass(frozen=True)
class LibraryCall:
    """A call made, inside a function body, through a name imported from a library."""

    api: str
    module: str
    line: int
    # A value built to be raised or returned, rather than work asked of the library.
    raised_or_returned: bool


@dataclass
class Facts:
    """Everything the checks read of a package, beyond its symbols and imports."""

    functions: dict[str, FunctionFacts] = field(default_factory=dict[str, FunctionFacts])
    classes: dict[str, ClassFacts] = field(default_factory=dict[str, ClassFacts])
    # Every call in the package and in its tests.
    calls: list[Call] = field(default_factory=list[Call])
    # (module, name) for every name a module reads without calling it; and the names the tests do.
    handed: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    handed_in_tests: set[str] = field(default_factory=set[str])
    # Every attribute read, every field matched by a pattern, every name listed in a collection.
    attribute_reads: set[str] = field(default_factory=set[str])
    matched_fields: set[str] = field(default_factory=set[str])
    listed_names: set[str] = field(default_factory=set[str])
    # The names each module calls through.
    called_names: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    library_calls: list[LibraryCall] = field(default_factory=list[LibraryCall])
