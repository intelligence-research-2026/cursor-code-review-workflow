# cursor-code-review-workflow

**Cursor CLI Code Review** GitHub Action (restricted autonomy: the agent only writes `review.md`, and the Action posts the comment with `gh`).

- [`action.yml`](action.yml) — the product itself (composite Action; this is what cross-repository calls use)
- [`.github/scripts/`](.github/scripts/) — the executable implementation (`resolve-pr` / `install-cursor-cli` / `run-review` / `scrub-review` / `post-review-comment` plus `assets/`)
- [`.github/workflows/code-review.yml`](.github/workflows/code-review.yml) — self-integration (`uses: ./` dogfooding)

Callers always use **`uses: owner/repo@<COMMIT_SHA>`** (Action form). GitHub fetches the entire action repository into `github.action_path`, so the scripts are immediately available. The older reusable-workflow form (`uses: owner/repo/.github/workflows/….yml@SHA`) only pulled the YAML and then checked this repo out with the caller's `GITHUB_TOKEN`; it is deprecated and has been removed.

**This repository is public**, so a private repo in any organization can pin its SHA. No Settings → Actions → General → Access configuration is needed (that only applied while this repo was private and callers lived in the same organization). A private action repository cannot be consumed across organizations, which is exactly why this one is public.

## Prerequisites

Configure these in the **caller repository**:

| Name | Type | Required | Notes |
|------|------|----------|-------|
| `CURSOR_API_KEY` | Secret | yes | API Key issued by the [Cursor Dashboard](https://cursor.com/dashboard); passed through `with.cursor_api_key` |
| `CURSOR_INSTALLER_SHA256` | Variable | yes* | Bare SHA256 (see the command below); the caller **must** set `with.installer_sha256: ${{ vars.CURSOR_INSTALLER_SHA256 }}` because the Action never reads Variables itself |
| `CURSOR_MODEL` | Variable | no | Model slug; defaults to `auto`; callers should set `with.model: ${{ vars.CURSOR_MODEL || 'auto' }}` |
| `CURSOR_AGENT_SHA256` | Variable | no | Verifies the CLI binary after install; callers may set `with.agent_sha256: ${{ vars.CURSOR_AGENT_SHA256 }}` |

Getting the installer checksum (**hash only, no filename column**):

```bash
curl -fsSL https://cursor.com/install | sha256sum | cut -d' ' -f1
```

\* The Action **does not** read the caller's `vars.*`. Setting the Variable without writing `with.installer_sha256` makes Install fail closed. The caller must pass the Variable (or a literal SHA) through `with:`.

Optional: place `.github/review-context.md` in the caller repository (it is read from the **PR base**), or override it through the `review_context` input.

Billing goes to the Cursor account that owns the API Key. This check is **advisory**; do not add it to branch protection required checks.

The agent's `Read` allowlist covers common source, frontend and build entry points (ts/py/go/java/kt, vue/svelte, Makefile, Dockerfile, package.json and so on) plus configuration and documentation; `.env*` and credential-like paths stay denied. Search is available through `rg`/`grep`/`find`/`ls`. Extensions that are not listed can only be inspected through search snippets, so the review may be incomplete for them.

## Caller example

```yaml
# .github/workflows/cursor-code-review.yml
name: Cursor Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_dispatch:
    inputs:
      pr_number:
        required: true
        type: string

jobs:
  code-review:
    if: >
      github.event_name == 'workflow_dispatch' ||
      (github.event.pull_request.draft == false &&
       github.event.pull_request.head.repo.full_name == github.repository &&
       github.event.pull_request.user.login != 'dependabot[bot]' &&
       github.event.pull_request.user.login != 'renovate[bot]' &&
       github.actor != 'dependabot[bot]' &&
       github.actor != 'renovate[bot]')
    runs-on: ubuntu-latest
    timeout-minutes: 20
    concurrency:
      group: cursor-code-review-${{ github.event.pull_request.number || inputs.pr_number }}
      cancel-in-progress: true
    permissions:
      contents: read
      issues: write
      pull-requests: write
      actions: write
    steps:
      - name: Cursor Code Review
        uses: intelligence-research-2026/cursor-code-review-workflow@<COMMIT_SHA>
        with:
          pr_number: ${{ github.event.pull_request.number || inputs.pr_number }}
          caller_event: ${{ github.event_name }}
          installer_sha256: ${{ vars.CURSOR_INSTALLER_SHA256 }}
          model: ${{ vars.CURSOR_MODEL || 'auto' }}
          cursor_api_key: ${{ secrets.CURSOR_API_KEY }}
```

Key points: use `uses: owner/repo@<COMMIT_SHA>` (**not** `.github/workflows/….yml@SHA`); pass the Secret through `with.cursor_api_key`; **do not** pass `environment`. Draft and bot skipping, `runs-on`, `timeout-minutes`, `concurrency` and `permissions` all belong to the **caller job**.

## Self-integration

This repository uses **`uses: ./`** (which requires `actions/checkout` first) so dogfooding always runs against the PR head. See [`code-review.yml`](.github/workflows/code-review.yml).

## Action inputs

| Input | Notes |
|-------|-------|
| `pr_number` | **Must be passed** (only `inputs.pr_number` is used; there is no event fallback) |
| `caller_event` | The caller's `github.event_name`; on forks only `pull_request` may skip, everything else is fail-closed |
| `review_context` | Overrides the review priorities |
| `review_context_file` | Defaults to `.github/review-context.md` |
| `installer_sha256` | **Must be passed by the caller through `with:`** (typically `installer_sha256: ${{ vars.CURSOR_INSTALLER_SHA256 }}`); empty makes Install fail closed |
| `model` | Passed by the caller through `with:`; empty defaults to `auto` |
| `agent_sha256` | Passed by the caller through `with:`; empty only emits a notice |
| `honor_pr_body_decisions` | Defaults to `false`; see "Multi-round convergence" below |
| `cursor_api_key` | **Must be passed** as `secrets.CURSOR_API_KEY` (through `with.cursor_api_key`) |

The Action **never reads** `vars.*` or `secrets.*`. Variables are resolved in the caller workflow and then passed in.

## Multi-round convergence

A reviewer is stateless by default: every round takes the full `base..head` diff and reviews it from scratch. Each fix triggers another review, so the same batch of issues gets reported over and over, a later round may contradict an earlier one, and patches pile up.

Before posting a new comment, this Action reads back the previous comment carrying the same marker:

- The previous body is injected into the prompt delimited by `--- BEGIN PREVIOUS REVIEW <nonce> ---` and treated as **untrusted data**, exactly like the PR diff (anyone with write access can edit that comment)
- The round number is written into the comment footer as `round=`, and the next round increments from it
- The prompt requires a per-item `Fixed` / `Still open` / `Withdrawn` / `Superseded` verdict and **forbids** re-listing a previously reported issue as a new finding
- It also **forbids** asking to revert a change an earlier round explicitly requested; overturning one requires a `CONTRADICTION` label explaining what the earlier round got wrong, with no "satisfy both sides" compromise tacked on
- Findings are tightened: they must land on lines this PR's diff touched, must carry a `Confidence`, P2 is capped at 3, P3 never enters Findings, and the overall cap is 8
- A closing `## Convergence` section states plainly that the PR can be merged once no P0/P1 remains open

Reading the old comment is **fail-open** (it continues as round 1 with a warning) — losing continuity is far better than failing the whole review.

With `honor_pr_body_decisions: true`, items dispositioned as `skip` in the PR body's `## Review decisions` section are not reported again either. The PR body is author-controlled, so enabling this hands the author a mute switch; it is therefore **off by default** and should only be enabled where there is a single author, the check is advisory, and forks are already skipped by the caller `if`.

This repository's own dogfooding enables the flag, which holds only because of "single author + advisory + forks skipped". The repository is public, but external fork PRs get no write access and are already skipped by the caller `if`, so the premise still stands. **The moment an outside contributor gets write access or non-owner branches are accepted, dogfooding must set `honor_pr_body_decisions` back to `false`.**

## Behavior summary

1. GitHub places this action repository at `github.action_path` (it is public, so every caller can read it); the scripts are staged to `$RUNNER_TEMP/ccr/scripts`
2. Resolve the PR (forks: dispatch fails, everything else skips) → validate the API Key → read back the previous review and round → check out the caller's PR head
3. Verify and install the Cursor CLI → the agent writes nothing but `review.md`, under the allowlist
4. Scrub secret shapes → upload the artifact → upsert the PR comment (footer carries `round=`)

Local debugging: with `PR_NUMBER` / `GH_TOKEN` / `GH_REPO` and friends set, `resolve-pr.sh` can be run on its own; the full agent flow still needs `CURSOR_API_KEY` and an installed CLI.

Offline test (no network and no API Key required; a `gh` stub drives the real scripts):

```bash
bash .github/scripts/test-review-assembly.sh
```

It covers the placeholder substitution order (a review context must not forge the nonce or the round), `default-review-context.md` staying within `REVIEW_CONTEXT_MAX`, previous-comment selection and round increment, plus edge cases such as fail-open, a body left with nothing but the marker, and oversized truncation.

Official reference: [Cursor CLI · GitHub Actions](https://cursor.com/docs/cli/github-actions).
