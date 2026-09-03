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
  rules.
- The Cursor CLI install script is verified against a caller-supplied SHA256 and
  fails closed when that input is empty.

## Known residual risks

These are deliberate, reviewed trade-offs and are documented in
[`.github/review-context.md`](.github/review-context.md): the installer checksum
covers the bootstrap script rather than the whole supply chain, `agent_sha256` is
optional, scrubbing only catches literal secrets and common token shapes, and
`Shell(commandBase)` matches the first token so an allowlisted search command may
still take arguments.
