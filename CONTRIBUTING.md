# Contributing

## English only

Source code, comments, documentation, workflow annotations, test names and commit
messages are all written in English. A CI check rejects pull requests that add CJK
characters to file contents; see [AGENTS.md](AGENTS.md) for the exact character
ranges.

## Change flow

1. Branch from `main` as `feat/<short-topic>`. There is no `develop` branch and no
   release branch.
2. Run the offline test locally:

   ```bash
   bash .github/scripts/test-review-assembly.sh
   ```

3. Open a pull request into `main`. Direct pushes to `main` are blocked.
4. Two checks run on the pull request: the English-only guard and this
   repository's own Cursor Code Review (dogfooding through `uses: ./`).
5. A maintainer reviews and merges. Merges are always manual; auto-merge is not
   used.

The Cursor Code Review check is **advisory**. It is never added to branch
protection required checks, because a same-repo `pull_request` runs the head
workflow and can read Secrets.

## Pinning

Third-party actions are pinned to an immutable commit SHA with a trailing version
comment, for example:

```yaml
uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

Callers of this Action pin it the same way: `uses: owner/repo@<COMMIT_SHA>`, never
a mutable `@v*` tag.

## Things not to change casually

The items listed under "Architecture invariants" in
[`.github/review-context.md`](.github/review-context.md) are trust boundaries, not
style preferences. Weakening the allowlist permissions, the scrubbing steps, the
fork skip or the installer checksum fail-closed behavior needs an explicit reason
in the pull request description.
