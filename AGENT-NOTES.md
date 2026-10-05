# Agent notes — 2026-10-05

## Scope and safety

Implemented the requested **local-PC-on pipeline** for YouTube-only work:

`prompt/order → generate, clip, or parts → QA → stage → explicitly confirmed
local Studio-browser upload`.

The existing `desktop.py` / `orders.py` Studio lane is the uploader. No Buffer
or direct YouTube API publisher was substituted. The owner affirmed prior
written permission for this automation; that is an owner attestation, not
independently verified. Keep the lane within that permission's scope and do
not ask for sensitive documents or credentials in chat. Retain the
`desktop.uploads` gate, exact per-job channel/action/visibility confirmation,
source-rights attestation for source-derived work, disclosure/audience answers,
and audit/recovery records. Do not perform a live upload without a specific
item/job, exact destination, and requested action.

Phone-triggered/PC-off processing is deferred for this phase, not cancelled as
a longer-term goal. Hosted/free-runtime feasibility remains open. No live
upload, provider request, actual render, real browser control, channel action,
or account action was performed in this pass.

## Implementation changes

- Added `pipeline.py` as the durable local order executor. `main.py order`,
  the Publish panel flow, and the `orders.execute()` compatibility path route
  through it. Jobs, step checkpoints, staged packet references, upload
  attempts, and reports are journaled under local work directories; `--resume`
  handles saved work, and uncertain commits pause for a human Studio check plus
  explicit `pipeline-reconcile` confirmation rather than blind retry.
- Wired generation, YouTube-source clipping, and parts splitting into the
  pipeline. Requests accept explicit topics/local source paths, exact channel
  selections, visibility, and per-video altered-content/audience answers.
  Source-derived posts require the owner rights attestation. Invalid or
  credential-bearing non-YouTube URLs are rejected.
- Added media/metadata verification and artifact hashing before the existing
  desktop Studio commit lane. Incomplete batches, invalid media, missing
  answers/consent, disabled `desktop.uploads`, and unresolved duplicates fail
  closed. Final per-item/channel assignments and results are recorded in staged
  manifests/journals.
- Applied free-only routing to pipeline subprocesses and standalone Panel
  Generate/Clip/Parts actions. Free routes use Pollinations/template text,
  Pexels/Pixabay/Pollinations images, edge-tts, and public YouTube captions;
  Groq Whisper and metered/premium LLM, image/vision, and voice routes are
  disabled. Free-service quota and availability still apply.
- Updated Panel labels/notes to reflect free-only routes and the existing
  browser uploader. The Publish section previews the request and requires a
  one-use typed confirmation phrase bound to that exact request. Updated the
  CLI banner and module docs so they no longer describe the entire product as
  manual-upload-only.
- Rewrote `AUTOMATION-PIPELINE-PLAN.md` and updated `README.md` for the actual
  implementation, free-only behavior, staged workflow, recovery limits,
  scheduling gap, permission attestation, offline test scope, and deferred
  phone/hosted work.

## Verification

- Full offline smoke suite: **211 passed, 0 failed** (`.venv/bin/python
  test_smoke.py`).
- `py_compile` passed for `main.py`, `orders.py`, `pipeline.py`, `desktop.py`,
  `panel.py`, `images.py`, `clipper.py`, and `test_smoke.py`.
- `git diff --check` passed after trimming trailing whitespace.
- Studio success-state fixtures cover English `Video published` and Italian
  `Il tuo video è stato pubblicato`.
- Tests use stubs/fixtures. No live provider calls, real FFmpeg/ffprobe media
  validation, actual Studio browser session, upload, or channel/account action
  occurred. The environment has no FFmpeg/ffprobe, so this was not an
  end-to-end media test.

## Known limits / follow-up

- Per-job source rights are attested, not legally adjudicated or backed by a
  stored evidence record. Owners still need to review rights, factual content,
  finished media, and the appropriate YouTube disclosure answers.
- The current journals are local JSON/checkpoints rather than SQLite or an
  off-device backup. Restore drills, remote processing-state polling,
  resumable chunks, scheduled posts, stop/cancel integration across pipeline
  stages, and post-render item-level approval remain future hardening.
- The Panel stays local-only. Do not expose it to the public internet. Keep
  phone/PC-off execution and hosted/free-runtime feasibility in the later
  phase.

## Working-tree / history

Branch: `fix/manual-pipeline-hardening`. The pipeline/docs/test pass was
committed as `feat: add gated local YouTube pipeline` and transferred by bundle;
the owner confirmed fetching and pushing it from their local checkout. The
initial push attempt from this sandbox lacked Git authentication; no
credential was inspected or printed and no force-push was attempted.

## Windows test follow-up (2026-10-05)

- The owner ran the suite on Windows: 209 passed, with `order_cli` failing on
  cp1252 emoji output and `panel_launch` detecting an unrelated live Panel in
  the default port scan.
- `main.py` now configures CLI stdout/stderr to replace unencodable characters
  instead of crashing; the `order_cli` regression explicitly forces cp1252.
- The first `panel_launch` isolation attempt chose a port range around one
  fake server; Windows allocated the other fake server adjacent to it, so that
  test still failed. It now chooses a currently unused scan range separate
  from both fixtures and the default Panel port.
- `order_cli`, `panel_launch`, and the full offline suite now pass: **211
  passed, 0 failed**. The owner fetched and pushed follow-up `1e6eb83`; this
  additional panel-test isolation refinement is prepared as another follow-up.

Preserve the pre-existing mode-only changes to `hooks/pre-commit` and
`setup.sh`; neither is part of the pipeline commit.
