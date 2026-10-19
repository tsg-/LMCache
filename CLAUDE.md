# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Code Quality Standards

The authoritative reference for coding quality, review process, and PR expectations is
**[docs/coding_standards.md](docs/coding_standards.md)**. Read it before writing or reviewing code.

Key principles (see the full doc for details and rationale):

- **Strong typing**: All functions have type hints. No `Any`, no bare generics. Avoid `Optional` -- initialize objects even if empty.
- **Docstrings**: Every public function has a complete docstring (summary, args, returns, raises). Docstrings must match actual current behavior.
- **Encapsulation**: Never access private members (`_`-prefixed) of other classes. Minimize public interfaces.
- **Interface design**: No ambiguous return values. No boolean parameters (use enums or split functions). Document schemas for dict/container params.
- **Testing**: New features and bug fixes require tests. Tests verify the public interface, not implementation details.
- **No `assert` for validation**: Use `if/raise ValueError` for runtime checks.
- **PR scope**: Keep PRs small and focused. Break large changes into multiple PRs.

For the quick-reference checklist and build/test/lint commands, see **[AGENTS.md](AGENTS.md)**.

## Design Docs

Design docs live under **`docs/design/`**, which **mirrors the `lmcache/` package tree**.
A design doc for code at `lmcache/<path>/` is located at `docs/design/<path>/`:

- `lmcache/cli/commands/ping.py` → `docs/design/cli/commands/ping.md`
- `lmcache/v1/distributed/l2_adapters/` → `docs/design/v1/distributed/l2_adapters/`
- `lmcache/v1/mp_observability/` → `docs/design/v1/mp_observability/`

When investigating a module, always check the mirrored `docs/design/<path>/` first for
design rationale, contracts, and extension guides. When adding or updating a design
doc, place it at the path matching the module it describes. See
[docs/design/README.md](docs/design/README.md) for the full convention.

Module `README.md` files stay co-located with code (symlinked from `docs/design/`); do
not relocate them.

## PR Review Instructions

When asked to review a PR, use the `/pr-review` skill. It carries the review dimensions
and the severity calibration from `docs/coding_standards.md` Section 9.
