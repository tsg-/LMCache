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

When asked to review a PR, use the `/pr-review` skill which implements the full review
process from `docs/coding_standards.md` Section 9.

The review covers:

1. **Design doc compliance** -- check implementation against documented contracts (table format).
2. **Coding quality** -- typing, docstrings, naming, interface design per Sections 2-4.
3. **Correctness** -- logic bugs, error handling paths, resource management.
4. **Thread safety** -- shared state, lock protocols, concurrent access patterns.
5. **Test coverage** -- especially failure paths and concurrent access.
6. **PR structure** -- is the scope appropriate, or should it be broken down?

Issues are grouped by severity:
- **error**: Must fix before merge (missing types/docstrings, no tests, architectural problems).
- **warning**: Should fix (naming, modularity, test quality).
- **info**: Suggestion only, non-blocking.

See `docs/coding_standards.md` Section 9 for the full severity calibration and reviewer guidelines.


## Environment Facts (bmg0 / bmg1)

Avoid wasting turns re-discovering these:

- **SSH hosts:** `bmg0` (smc-test01, 192.168.100.3/200.3), `bmg1` (smc-test02, 192.168.100.4/200.4)
- **SSH user:** `dev` — not `root`. Always `ssh dev@bmg0` or just `ssh bmg0` (alias resolves).
- **Working dir on remote:** `~/tsg/LMCache` (`/home/dev/tsg/LMCache`)
- **Python venv:** `~/tsg/LMCache/.venv-ipu/bin/python3` (Python 3.12.3). Use `.venv-ipu/bin/pytest` and `.venv-ipu/bin/pip`. The system `python3` is at `/usr/bin/python3` but lacks test packages.
- **pytest command:** `cd ~/tsg/LMCache && .venv-ipu/bin/python -m pytest <args>` — always use the venv python, not bare `pytest`.
- **ulimit -l:** already `unlimited` on both hosts — no need to raise it for RDMA MR registration.
- **RDMA devices:** `mlx5_0` (192.168.100.x) and `mlx5_1` (192.168.200.x) on both hosts. CX7, RoCEv2.
- **Cross-wire:** bmg0:mlx5_0 ↔ bmg1:mlx5_1 (192.168.100 subnet); bmg0:mlx5_1 ↔ bmg1:mlx5_0 (192.168.200 subnet).
- **GID index for RoCEv2:** typically index 3 (`LMCACHE_RDMA_GID_INDEX=3`).
- **nixl installed:** `nixl-cu12` in `.venv-ipu`. Import as `nixl_cu12._api`.
- **Server port in tests:** 5605 for NIXL, 5601 for RDMA thin client. Kill stale servers with `fuser -k 5605/tcp` before re-running.
- **Repo sync:** `cd ~/tsg/LMCache && git fetch tsg && git reset --hard tsg/ipu-poc`

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
