# Repository conventions

## English only

Everything written into this repository is in English: source code, identifiers,
comments, documentation, workflow annotations (`::error::` / `::notice::` text),
test names, commit messages and PR titles.

A CI check (`.github/workflows/english-only.yml`) rejects any pull request that
adds CJK characters to file contents. Commit messages are not machine-checked, so
keeping them English is on you.

Em dash (U+2014) and ellipsis (U+2026) are allowed because both are valid English
punctuation. Fullwidth punctuation is not: the guard rejects U+3000-U+303F,
U+3040-U+30FF, U+3400-U+4DBF, U+4E00-U+9FFF, U+AC00-U+D7AF, U+F900-U+FAFF and
U+FF00-U+FFEF.

## Trust boundaries

This repository reviews untrusted pull requests, so a few rules are load-bearing
rather than stylistic:

- The agent may only write `review.md`. Every `git` / `gh` call and every PR
  comment stays in a deterministic workflow step.
- `run-review.sh` deletes `.cursor`, `.cursorrules` and `AGENTS.md` from the PR
  head before starting the agent. This file therefore has no effect on a review;
  binding review guidance belongs in `.github/review-context.md` on the base
  branch.
- Never add `--force` or `--yolo` to the agent invocation, and never relax the
  installer checksum, the scrubbing steps or the fork skip without an explicit,
  reviewed reason.

## Change flow

`feat/*` branch, then a pull request into `main`. There is no `develop` branch and
no release branch. Third-party actions are pinned to a commit SHA with a trailing
version comment.

Before opening a pull request, run the offline test:

```bash
bash .github/scripts/test-review-assembly.sh
```
