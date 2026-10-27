# Security policy

## Reporting a vulnerability

Issues are disabled on this repository. Report vulnerabilities through GitHub
private vulnerability reporting: open the **Security** tab and choose **Report a
vulnerability**. Please do not disclose the details in a public pull request.

Include the affected commit SHA, how the issue is reachable from a caller
workflow, and the impact you expect.

## How this Action handles secrets

- `CURSOR_API_KEY` is never read from `secrets.*` inside the Action. The caller
  resolves it and passes it through `with.cursor_api_key`, and the Action calls
  `::add-mask::` on it before any step can log it.
- The Action never reads the caller's `vars.*` either. Every variable is resolved
  in the caller workflow.
- The review agent runs with an empty `GITHUB_TOKEN` / `GH_TOKEN`. All `gh` calls
  live in deterministic workflow steps outside the agent.
- `scrub-review.sh` and `scrub-agent-stdout.sh` run before anything is uploaded or
  posted. A literal `CURSOR_API_KEY` / runner token or a private key PEM marker is
  fail-closed: `review.md` blocks the post, and `agent-stdout.log` is deleted
  instead of uploaded. Common token shapes are redacted by pattern.

## Trust model

- The PR title, body and diff are untrusted and injected inside a one-time
  nonce-delimited block. The prompt forbids following instructions from that
  block, and an injection attempt is reported as a P0 finding.
- The previous round's review comment is read back and treated as untrusted the
  same way, because anyone with write access can edit it.
- Rule files on the PR head (`.cursor`, `.cursorrules`, `AGENTS.md`) are deleted
  before the agent starts, so a pull request cannot supply its own "trusted"
  rules. Before deletion, regular tracked files allowed by the existing Read
  permissions are saved as inert `.txt` evidence. Symlinks are never followed;
  denied, binary or oversized head snapshots are explicitly marked unavailable.
  These copies and the complete diff parts remain untrusted review data.
- The Install step downloads the current Cursor install script from
  https://cursor.com/install and runs it. `installer_sha256` is ignored.
  `agent_sha256`, when set, still verifies the CLI binary after install.

## Known residual risks

Performance collection reads structured CLI events in memory and persists only
aggregate counters, timings and a validated CLI model label. Raw prompts, file
paths, arguments, source, results and stderr are not retained. Identifiers are
salted per run and discarded with the collector. The performance artifact does
not contain raw agent logs; its upload is best-effort and does not gate review.

These are deliberate, reviewed trade-offs and are documented in
[`.github/review-context.md`](.github/review-context.md): the install step runs the
current bootstrap script with no checksum, `agent_sha256` is optional, scrubbing
only catches literal secrets and common token shapes, and
`Shell(commandBase)` matches the first token so an allowlisted search command may
still take arguments.
