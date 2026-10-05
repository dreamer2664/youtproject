# Agent notes — 2026-10-05

## Scope

Reliability hardening for local generation and **manual YouTube upload kits**.
No browser publishing, YouTube API upload, Buffer publish, paid service, or
external provider/API call was used in this review. Generated clips/parts kits
are now YouTube-only; other platform destinations remain out of scope.

## Changes made

- `generate --keep-going` now returns nonzero if any job fails. Backlog-picked
topics from failed renders are restored, failed topics are retryable, and the
backlog file is written by atomic replace.
- Packaging failures retain the job's `generated` state, record a redacted
retry hint, and return nonzero. A subsequent successful package clears that
error. Upload-kit replacement is staged and only committed after all files
are written; a failed replacement preserves the previous kit.
- Multi-source clip/parts/longform commands return nonzero for partial batches,
while still processing every source and recording per-source results.
- Script review is truthfully described as a model plausibility pass, never
source verification. `[CUT]` material is removed before speech; an all-cut
script blocks rendering. The no-key template uses a research-method scaffold
rather than invented witness/archive/verification claims.
- Added credential redaction for recognizable formats, query strings, bearer
headers, and configured secrets in relevant errors/logs.
- Upload kits, Telegram delivery copy, configuration, and docs follow the
YouTube-only manual workflow; Studio's AI-use answer is conditional on the
finished video's content. Rights/attribution guidance was corrected.
- Docs clarify the YouTube Data API audit applies per API project, not per
channel, and that manual YouTube app/site upload is the legitimate alternative
to an unverified API upload that is locked private.
- Docs no longer promise unattended work while the PC is shut down. The local
bot cannot poll or render while the PC is off; no hosted runner was added.

## Verification

- Full offline smoke suite: **206 passed, 0 failed**.
- Targeted regressions for retryable topic generation/package failures,
transactional kit replacement, partial URL batches, fact-review `[CUT]`
handling, safe error redaction, and YouTube-only kits passed.
- Python byte-compilation and `git diff --check` passed.
- No live FFmpeg end-to-end video render, provider API call, actual Studio
upload, or channel/account action was performed.

## Working-tree note

Branch: `fix/manual-pipeline-hardening`. Existing mode-only changes to
`hooks/pre-commit` and `setup.sh` predated this pass and are unrelated; preserve
them and do not include them in a task-specific commit unless deliberately
reviewed.
