# Changelog

## Unreleased

- **The hook speaks only where it was asked to.** It stays silent unless the project declares
  vibesmell: a `[tool.vibesmell]` table in `pyproject.toml` (empty is enough) or a `vibesmell` key
  in `package.json`. Installed for every project, it used to talk in projects that never chose it.

## 0.1.0 — the smells a linter can't see

The first release. vibesmell reads a Python package or a TypeScript project whole, resolves who
calls what down to the method, and reports what code written fast leaves between files.

- **26 checks, each across files**: dead code and code only the tests use, forwards, tunnels,
  private names used abroad, boolean flags, import cycles, layers and forbidden imports, data
  clumps, constant parameters, hubs, shotgun surgery read from git, public names used at home only,
  unread fields, twin shapes, duplicate bodies, ignored returns, defaults nobody overrides, unused
  parameters, library calls scattered raw, unstable dependencies, feature envy and single
  implementations. Each one was tuned against real codebases until what it finds is what it says.
- **No baseline.** A finding is fixed, or the check is. A folder can be excused in
  `[tool.vibesmell]` only with a `why`, printed on every run.
- **A score** of six attributes, read from the source at any commit and stored nowhere.
- **The page** (`vibesmell serve`): the score, every finding with its check's note, every door's
  whole trace animated as the code reads, a who-uses/what-calls tree per function, and eighteen
  findings that can be *watched* in the code, station by station.
- **`play` and `reach`**: animate any flow on the page from the command line, and list every
  entrypoint that reaches a function.
- **TypeScript**, read with TypeScript's own compiler and checker (TypeScript 6, installed beside
  the reader on first use; it needs `node`).
- **Library mode** (`library = true`, or `--library`): every public name is a door.
- **For agents**: `vibesmell summary` prints the whole report as Markdown, and `vibesmell
  install-hook` tells Claude Code, after every save, what the file has new and what the save fixed.
