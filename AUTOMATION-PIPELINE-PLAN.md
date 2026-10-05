# Automated YouTube Pipeline — Reliability & Rollout Plan

**Local implementation complete; offline-verified (2026-10-05).** The owner
specified the existing local browser-controlled YouTube Studio lane
(`desktop.py` / `orders.py`), not Buffer or a direct API uploader, and affirmed
prior written permission covering that automation. This is an owner
attestation, not independently verified here; keep use within that scope and
do not ask for sensitive permission documents in chat. The owner accepted
local PC-on execution for this phase; phone-triggered/PC-off processing is
deferred. No live upload, real browser action, or channel/account action has
been performed. The task target is a deliberate request to generate, clip, or
split, stage, validate, and—only with exact per-job consent—upload to a
selected YouTube channel.

## The target experience

The owner chooses one of three explicit actions:

1. **Generate** — make a new video from a topic/prompt.
2. **Clip** — make vertical clips from a source the owner is authorized to use.
3. **Parts** — split a source the owner is authorized to use into a numbered
   series.

A request can say **publish** or **do not publish**. If it requests posting,
the CLI/Panel first show the interpreted operation, requested/max item count,
source/topic, exact selected channel identities, round-robin assignment rule,
and visibility. The owner confirms that per-job action before rendering. Once
confirmed, rendering, validation, staging, and the existing local Studio upload
action run. Each final item/channel assignment is written to its staged packet
and upload journal. If rendering is short/incomplete or QA fails, upload is
blocked. This version does not add a second post-render approval of item titles
or assignment. Scheduling and scheduled times are not implemented; ambiguous
channel selections are rejected rather than guessed.

The goal is **fail-safe and recoverable**, not literally fail-proof. Networks,
providers, storage, YouTube, and free-tier quotas can fail. A failure must not
lose the source, corrupt a complete kit, silently claim success, or create a
duplicate post on retry.

## What exists today

- **Durable local orchestrator:** `pipeline.py` is called by the CLI `order`
  command and the compatibility `orders.execute()` wrapper. It journals each
  job and step under `work/pipeline/jobs/<job-id>/`, uses an active-job lock,
  writes rolling checkpoints, stages per-item/channel manifests, and writes a
  final report under `work/post/<date>/<job-id>/report.json`. `--resume` resumes
  a saved local job; an ambiguous upload is not blindly retried.
- **Inputs and render lanes:** the order parser and Panel accept explicit
  generation topics/prompts, source-file paths, exact channel names/IDs,
  visibility, and per-video disclosure/audience answers. The durable order
  runs generated videos, clips, and parts. URL sources are restricted to
  credential-free YouTube/youtu.be hosts; local source files remain allowed.
  Other destinations and third-party publisher adapters are out of scope.
- **Free-only provider routing:** order subprocesses always receive
  `--free-only`; standalone Panel Generate/Clip/Parts actions do too. Text is
  Pollinations/template, images are limited to Pexels/Pixabay/Pollinations,
  voice uses edge-tts, and metered LLM/Groq Whisper/Gemini image or vision/
  premium voice routes are disabled. Transcription tries public YouTube
  captions; if unavailable, the free-only clip/parts job stops rather than
  calling Groq. Free-service quotas/availability can still limit work.
- **QA and staging:** completed media is checked with ffprobe for video stream,
  codec, dimensions, and positive duration; ffmpeg performs a full decode when
  installed. Upload metadata bounds and artifact checksums are verified before
  the browser lane. These checks do not replace a human review of rights, facts,
  title quality, or YouTube's disclosure decision.
- **Uploader and consent:** the only uploader wired into this pipeline is the
  existing `desktop.py` local Studio-browser lane. `desktop.uploads` must be
  on; the request must have an exact selected channel/visibility and explicit
  per-job confirmation; clip/parts also need the source-rights attestation,
  plus the altered-content and made-for-kids answers. The Panel previews the
  exact request and binds a one-use token to a typed phrase. Studio success
  markers are checked; uncertain outcomes are journaled for manual inspection
  and `pipeline-reconcile`. No Data API upload, Buffer publish, or alternative
  publisher is used by this pipeline.
- **Recovery limits:** the JSON journals/checkpoints, checksums, lock, and
  duplicate ledger provide local recovery, not a full backup strategy. There
  is no SQLite migration, off-device backup, automatic restore drill, remote
  processing-state poll, scheduled-post flow, or upload chunk resume yet.
- **Phone / PC power:** no new phone/PC-off orchestration was added. The local
  worker and browser require the PC and signed-in browser to be running.
  Phone-triggered/PC-off processing is deferred; hosted/free-runtime
  feasibility remains open for a later phase.
- **Verification:** on 2026-10-05 the offline smoke suite passed **211 tests,
  0 failed**. Syntax compilation passed. Tests use mocks/fixtures: no provider
  calls, live render, real Studio browser session, channel action, or upload
  was performed. The owner's permission statement remains owner-attested, not
  independently verified.

## Hard gates before automatic public posting

### 1. The requested local browser uploader needs YouTube authorization

The user's requested `desktop.py` / `orders.py` uploader controls YouTube
Studio in a local browser. YouTube's Terms prohibit accessing the service using
automated means except for public search engines operating under `robots.txt`
or with YouTube's **prior written permission**. That restriction does not
disappear because the browser is on the owner's PC, the owner clicks the job
button, or the code avoids the Data API. The existing `desktop.uploads` switch
is an app safety gate, not permission from YouTube. Do not extend or enable
browser-driven uploads as an API-audit workaround. See
[YouTube Terms of Service](https://www.youtube.com/t/terms).

The owner has affirmed that prior written permission covers this local
Studio-browser automation. We rely on that owner attestation; it is not
independently verified in this workspace. Stay within its stated scope. If
coverage is uncertain or changes, leave `desktop.uploads` off and use manual
Studio upload instead. The software confirmation/gate remains required even
with permission.

### 2. Other legitimate publisher routes (background only; not selected)

The owner selected the existing local Studio browser uploader for this task.
The alternatives below are context only: this pipeline does not use or offer
them as a fallback.

YouTube's `videos.insert` documentation says uploads from unverified API
projects created after 28 July 2020 are restricted to private viewing and that
each API project must undergo an audit to lift that restriction. That audit is
**per API project, not per channel**. YouTube Help also says a video locked by
an unverified API service can be re-uploaded through the YouTube app/site.
That makes manual upload a legitimate route; it is not a way to disguise an
API upload. See [Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert)
and [Videos locked as private](https://support.google.com/youtube/answer/7300965?hl=en).

If the browser route is not authorized, use one audited/approved YouTube API
project for direct uploads, with OAuth consent for each channel the owner
explicitly connects, or consider Buffer/another integration only after
verifying current authorization, intended YouTube features, exact channel
mapping, and free-plan support. Do not represent a third-party integration,
browser automation, or a different API key as an audit bypass. If no compliant
route is available, automatic public posting is blocked; manual Studio upload
remains available.

YouTube's Developer Policies require API clients to clearly identify actions
performed for a user and obtain the user's express consent before those
actions. A UI must identify the channel and the upload/privacy action before
execution. See [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies).
The implemented confirmation binds the exact job prompt/topic, configured
channel selection, visibility, and publish intent. Scheduled time is not
supported. A generic Generate/Clip/Parts button never publishes.

### 3. Resolve where the work runs

The current local pipeline is usable only while the PC and worker are running.
A fully shut-down PC cannot service a phone request. Local PC-on processing is
accepted for this phase. Phone-triggered/PC-off processing is deferred, not
cancelled; hosted/free-runtime feasibility remains a later decision. A free
host is not a guaranteed always-on contract, and video rendering, storage, or
quota can exceed its limits. If no genuinely free service meets measured
resource/reliability needs, continuous PC-off processing and “100% free”
cannot both be promised.

Do not bind the current unauthenticated local panel to the public internet.
Phone/LAN access requires authentication, CSRF protection, rate limits, and a
private-network design; a random port or obscured URL is not security.

## Remaining architecture and hardening targets

### A. Current durable orchestrator; future hardening

`pipeline.py` is the local `order` executor behind the CLI, Panel, and legacy
`orders.execute()` compatibility entry point. Each front end should continue
to use the validated job path rather than assembling its own generation/upload
sequence. The present job journal is atomic JSON with rolling checkpoints and
an active lock; it is not SQLite and is not an off-device backup.

A job record should include:

- stable job ID and request fingerprint;
- operation (`generate`, `clip`, `parts`, or `longform`), normalized prompt,
  source URL/path, and relevant render settings;
- owner/channel profile ID(s), visibility, schedule, and an explicit publish
  intent/consent record when posting is requested;
- source-rights state and evidence reference for supplied footage;
- stage, attempt counters, timestamps, progress, failure category, and
  redacted diagnostics;
- artifact manifest: filenames, SHA-256 checksums, sizes, and metadata version;
- publisher adapter, remote upload/post ID, upload status, and reconciliation
  result.

Use a tested state machine rather than loosely changing strings:

`received → validated → queued → rendering → rendered → QA → ready →
awaiting publish consent (if needed) → uploading → processing →
scheduled/published → reconciled`

Failures go to `retry_wait` or `needs_owner`; cancellation is a terminal state
that is checked before every irreversible operation. A job may advance only
when its required artifacts exist and pass validation. `published` means the
remote channel was queried and verified—not merely that an HTTP request
returned success.

Use SQLite with WAL/transactions for concurrent job and publication records,
or first harden the JSON store with unique temp files, locking, fsync, schema
versioning, and recovery tests. **Do not migrate and delete the current
`state.json` in one step:** import it, compare counts/IDs, keep a read-only
snapshot, then switch only after a restore test.

### B. Adapters, not one giant command

- **Input adapters:** CLI and Panel currently route order requests through the
  durable job path. Do not add phone/Telegram confirmation until a separate
  phase; any legacy Telegram `/order` path must remain unable to commit an
  upload without the exact local per-job consent. Natural-language parsing
  should show operation/source/count/channel before work starts.
- **Render adapters:** reuse the existing `scriptgen`, `clipper`, `parts`,
  `longform`, `assembler`, and `package` modules behind one `run(job)`
  contract. Keep a fallback chain, but permit only zero-cost providers unless
  a future request explicitly changes the cost policy.
- **QA adapter:** validate file size/nonzero bytes, ffprobe streams/duration,
  dimensions/codec, playback, audio/video sync, subtitle bounds, metadata
  limits, title/description consistency, missing assets, duplicate hashes,
  and disk space. Route uncertain outputs to `needs_owner`.
- **Publisher interface:** the current adapter is the existing local Studio
  browser path (`desktop.post_one()`), under the owner's affirmed permission
  and the app's explicit gate. No direct API or Buffer publisher is part of
  this pipeline. Future processing/status APIs or alternate adapters require
  a separate user decision and authorization review; do not bypass exact
  channel/action consent or the desktop safeguards.

### C. Fail-safe consent and content checks

A request that intends to publish must identify the exact channel and desired
visibility. Scheduling is not implemented. CLI/Panel show the interpreted
job and selected destination; the Panel displays the exact confirmation phrase,
and the CLI asks for the exact channel/visibility phrase. The job request,
consent, stage reports, and per-attempt results are journaled locally. Do not
assume the separate desktop kill switch cancels a pipeline already between
stages; stop/cancel integration is a hardening item.

Before publishing:

- **Rights:** auto-publish only material the owner owns or has permission to
  use. A URL in `sources/sheet.csv`, source credit, trimming, captions, or a
  “fair use” label is not permission. Add explicit `owned/licensed/unknown`,
  evidence note/link, and approval fields; `unknown` fails closed.
- **Facts:** the current model review is not source verification. Add a
  source-backed research/claim record or require owner review for factual
  claims. Never auto-publish the offline template scaffold as a finished
  factual video without replacing it with supported content.
- **AI use:** derive the Studio AI-use answer from the actual finished media
  and YouTube's current criteria, not merely from “AI was used.” Realistic
  invented scenes, altered real events/places, and real-person impersonation
  can require disclosure; stylized/minor assistance can differ. Preserve the
  decision and basis in the job manifest. Official guide:
  [Disclosing use of GenAI content](https://support.google.com/youtube/answer/14328491?hl=en).
- **Duplicate prevention:** future hardening can compare the requested
  source/topic, artifact hash, channel, metadata, and a remote upload ID. The
  current ambiguous-commit path pauses, asks the owner to inspect that exact
  channel in Studio, and accepts only a human-verified result through
  `pipeline-reconcile`; it does not auto-query YouTube or auto-retry.
- **Channel choice:** show the actual channel name/ID and account before
  consent. Do not default to the first organization or a service-level match.
  Store OAuth credentials separately per owner/channel, least-privilege, and
  never in `config.yaml`, logs, prompts, job JSON, or backup archives.

## Backup and recovery design

Use several independent layers. Backups improve recoverability; they do not
make outages, copyright claims, account actions, or platform decisions
impossible.

1. **Before each job:** snapshot the job request, current state, source list,
   settings, provider route, consent, and prompt to a versioned local journal.
   Redact secrets and avoid storing OAuth refresh tokens in the job record.
2. **During rendering:** write to a unique per-job staging directory and
   `.partial` media filename. Never overwrite the last known-good output. On
   success, validate with ffprobe, compute a SHA-256 manifest, then atomically
   rename into the immutable artifact directory.
3. **Before publication:** verify at least one valid local master exists, its
   checksum matches the manifest, and a complete upload kit has been built.
   Retain both the master and kit until remote publication is reconciled and a
   configured recovery period expires.
4. **Queue/database:** use SQLite's online backup API or atomic, unique-name
   snapshots before schema changes and publication transitions. Keep a
   configurable rolling set (for example, last 30 daily snapshots) and a
   pre-migration snapshot. Test restoring to a temporary directory before
   pruning older backups.
5. **Media copies:** the render master plus packaged copy give two local
   copies, not a true off-device backup. If the owner already has an external
   drive or a genuinely free private storage account with enough quota, add a
   third copy and verify checksums. Do not silently upload private video or
   captions to a public host. Do not promise 3-2-1 backups without confirming
   the second medium and off-site capacity exist.
6. **Publisher journal:** persist request ID, idempotency key, remote ID,
   chunk/resume position, last response category, and reconciliation result
   before moving to the next stage. If the process crashes after remote
   success, query by known ID/job marker before retrying. Do not retry a
   potentially successful `create` blindly.
7. **Startup repair:** scan incomplete states, `.partial` files, stage/backup
   directories, orphaned remote IDs, missing checksums, and low disk space.
   Resume only idempotent steps; quarantine ambiguous work for owner review.
8. **Restore drills:** automate fault injection for disk full, process kill,
   restart mid-render, network timeout, HTTP 429/5xx, expired OAuth, missing
   source/thumbnail/SRT, duplicate button clicks, and partial multi-channel
   completion. A backup that has not been restored in a test is not a proven
   backup.

Keep secrets out of backups. On Windows, prefer DPAPI or the OS credential
store for OAuth refresh tokens. Keep the existing rule: never echo the PAT;
rotate the chat-exposed PAT when the current work arc is complete.

## Implementation status and remaining gates

### Implemented (offline-tested, no live service actions)

- Local `pipeline.run()` connects order parsing to generation, clip/parts,
  validation, staged packets, and the existing desktop Studio uploader. CLI and
  Panel consent gates are per job; the panel token is one-use and bound to the
  exact request, topic, channel selection, visibility, and per-video answers.
- The pipeline fails closed on missing exact consent, disabled upload gate,
  missing source-rights attestation for clip/parts, missing altered-content or
  audience answer, incomplete output, duplicate/uncertain commit, failed media
  QA, or invalid source URL. It journals stages and upload attempts and offers
  `--resume` plus owner-confirmed `pipeline-reconcile` after manual Studio
  inspection.
- Free-only subprocess routing is wired for generated videos, clips, and
  parts. Standalone Panel Generate/Clip/Parts are free-only too. Groq Whisper
  is disabled in that mode; public YouTube captions are used, or the job stops
  clearly when captions are unavailable. No paid/metered provider is enabled
  by this pipeline; free-provider quotas can still be exhausted.
- Media QA includes ffprobe stream/codec/dimension/duration checks, optional
  full ffmpeg decode, metadata bounds, and checksums. Source URLs are restricted
  to ordinary YouTube hosts; local files are allowed as source material.
- Offline verification on 2026-10-05: `python test_smoke.py` reported
  **211 passed, 0 failed**; `py_compile` passed. Fixtures exercise English and
  Italian Studio success markers. No live provider call, actual FFmpeg/ffprobe
  run, browser session, channel action, or upload was performed.

### Deliberately deferred / not yet verified

- Phone-triggered or PC-off processing is deferred. The current worker needs
  the local PC and signed-in browser on. Hosted/free-runtime feasibility remains
  open for later; do not bind the local unauthenticated panel publicly.
- No live render/provider/browser/upload test has been run. An offline pass is
  not evidence that a provider quota, FFmpeg build, YouTube Studio layout, or
  browser session will work live.
- Source rights are a per-job owner attestation, not a legal determination or
  stored evidence record. Fact verification/citations are not added to the
  pipeline. The altered-content answer is selected by the owner after review;
  the code does not infer YouTube's policy answer from generated media.
- JSON journals/checkpoints are local recovery aids, not a full backup plan.
  SQLite, off-device copies, restore drills, remote processing-state polling,
  resumable upload chunks, scheduled posts, and a configurable posting-rate
  policy remain future hardening. Scheduling is not available in this pipeline.

### Conditions before any live pilot

Permission is owner-attested and must remain within its actual scope; the
`desktop.uploads` flag is only the application's commit gate. Do not run a live
post without the owner specifying a particular item/job, exact destination,
visibility, and requested publish action. Confirm the exact channel and
per-video disclosure/audience settings in the job plan, review rights and
media, and keep the local browser safeguards and audit log intact. A specific
user request is still required for any real upload; implementation consent is
not permission to publish an arbitrary video.

## Current recommendation

Use the local pipeline only for explicit PC-on requests and verify the staged
artifacts before considering a publish action. Keep the desktop uploader as
implemented, do not substitute Buffer or a direct API path, and retain the
existing confirmation, `desktop.uploads` gate, source-rights check, media QA,
uncertain-state pause, and audit logging. Continue the phone/PC-off and
free-host feasibility work later as a separate phase.

## References

- [YouTube Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [YouTube videos locked as private](https://support.google.com/youtube/answer/7300965?hl=en)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- [YouTube GenAI disclosure guidance](https://support.google.com/youtube/answer/14328491?hl=en)
