"""The few words every check uses of a symbol's id and a module's name."""


def short(sid: str) -> str:
    """`Store.append` for `log.store:Store.append`."""
    return sid.split(":", 1)[1]


def is_dunder(sid: str) -> bool:
    return short(sid).split(".")[-1].startswith("__")


def folder(module: str) -> str:
    """The top folder a module lives in; the empty string for a module at the package's root."""
    return module.split(".")[0] if "." in module else ""
