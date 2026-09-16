# Archive

The measurement harnesses and probes behind the numbers in `CHANGELOG.md` and `docs/measurements/`.
They ran once, against a machine and a build that no longer exist.

Nothing here is on a path the product takes: not in the installer payload, not in CI, no checker
reads it, and ruff skips the directory (`pyproject.toml`). History is in git -- every file arrived
by `git mv`, so `git log --follow` on any of them reaches back past the move.

The rule, from `docs/plans/linux-implementation-plan.md` 5.3: `tools/` accepts only scripts referenced by the justfile, the docs, CI or the checkers; experiments go to `tools/archive/` at merge time.
