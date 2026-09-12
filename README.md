# Zoom → Google Drive archive pipeline

**Eliminated a recurring $1,000+/month Zoom storage bill by migrating 3+ TB of
cloud recordings into storage the business already owned — without losing a
single recording — then replaced the migration with an automated lifecycle so
the problem does not come back.**

A client's Zoom account had accumulated roughly **1,800 cloud meeting
recordings** over three years. Storage was **122% over its 1 TB cap**, the
overage was billed every month, and nobody could say with confidence which
recordings were safely archived and which existed only in Zoom.

Meanwhile the same business was paying for Google Workspace with **115+ TB of
unused capacity**. The recordings were sitting in the most expensive possible
place while a paid-for destination sat empty.

This repository is the tooling that closed that gap: it established file-by-file
proof of what was archived, moved the archive to Drive, reclaimed the Zoom
storage, and then replaced the entire manual process with an event-driven
pipeline on Cloud Run that handles every new recording automatically.

### What this project demonstrates

| | |
|---|---|
| **Identify repetitive work** | Manual, unverifiable backup-and-delete across ~1,800 recordings, repeating for every new meeting |
| **Design an automation** | Index → reconcile → verify → gated delete, with every destructive step provable and reversible |
| **Integrate APIs & services** | Zoom API + webhooks, Google Drive API v3, Cloud Run jobs & services, Cloud Scheduler, Secret Manager |
| **Make it reliable** | Byte-exact verification gates, pre-execution assertions, dry-run modes, resumable idempotent operations, append-only audit logs, unit tests |
| **Measure business impact** | **$1,000+/month eliminated**, 3+ TB migrated, 122% over cap → within free tier, **0 recordings lost**, 0 ongoing manual hours |

The engineering decision that mattered most was the last row of that table
being *sustainable*: this is an ongoing automated lifecycle, not a one-time
migration that silently rots the moment someone records a new meeting.

> **About this repository.** This is a sanitized portfolio copy of production
> tooling. Account names, meeting titles, folder ids and project ids have been
> replaced with placeholders, and the real Zoom/Drive indexes — which describe
> private meetings — are replaced by [generated fixtures](#fixtures) that match
> the original schemas exactly. The code, architecture and engineering decisions
> are unchanged.

---

## Contents

- [What this project demonstrates](#what-this-project-demonstrates)
- [Problem](#problem)
- [Process — establishing ground truth](#process--establishing-ground-truth)
- [Automation — from laptop to pipeline](#automation--from-laptop-to-pipeline)
- [API integrations](#api-integrations)
- [Testing](#testing)
- [Results](#results)
- [Repository layout](#repository-layout)
- [Running it](#running-it)

---

## Problem

The brief sounded simple — "back up the Zoom recordings and delete them to get
under the cap." The business case was equally simple: Zoom storage overage was
costing **$1,000+/month** on a recurring basis, while Google Workspace storage
the client already paid for sat **115+ TB empty**. Every month of delay was
money spent to keep files in the wrong place.

Five things made it hard:

**1. Deletion is irreversible, and the existing evidence was wrong.**
An earlier cleanup had verified backups against the *Google Drive Desktop local
cache*. That cache reports partially-synced files at their partial size, so it
declared good backups corrupt and — far worse — could declare an incomplete
upload complete. Any tool built on that premise could permanently destroy
recordings. Ground truth had to be re-established from scratch.

**2. Zoom's own storage numbers didn't add up.** The sum of recording file sizes
was ~969 GB. Zoom's billing meter said 335 GB. Both were correct, and the
difference had to be understood before anyone could predict what deleting a
recording would actually reclaim:

| Measure | Value |
|---|---:|
| Sum of all live recording files (API, verified against Zoom's file servers) | ~969 GB |
| Largest video layout per meeting + audio | ~314 GB |
| Zoom **billed** storage (`accounts/me/plans/usage`) | **335 GB** |

Zoom renders every meeting into five or six downloadable layouts — speaker view,
gallery view, shared screen, and combinations. All are real files the API will
hand you, but the billed figure tracks roughly **one layout per meeting**. So
deleting the remaining media would reclaim ~300 GB of *billed* storage, not
969 GB, while the archive still had to hold all of it. Getting this wrong in
either direction meant either a useless cleanup or a lost archive.

**3. The existing archive was a mess.** Years of ad-hoc backups had left whole
folders re-uploaded under variant names. Deduplicating it was its own project.

**4. Some recordings refused to delete.** A subset returned Zoom API error 3332
— *"being used for Zoom IQ for Sales"* — even though that AI feature's
subscription had been cancelled and its UI was gone.

**5. A one-time migration would not have solved it.** Even a flawless manual
migration only fixes the bill until the next recording is made. New meetings
were still being recorded weekly, so anything short of an automated lifecycle
would have put the account back over its cap and back on the overage bill within
months. The deliverable had to be a running process, not a completed task.

---

## Process — establishing ground truth

The guiding rule, which every tool in this repository enforces:

> **Never delete from Zoom without proving the backup exists in Drive.**

"Proving" needed a definition. Zoom does not expose a checksum for cloud
recording files, so the strongest pre-download signal is the **exact byte
length** Zoom reports, compared against the true byte length read back from the
**Drive API** — never from a local sync cache.

The pipeline is three read-only passes, then a gated write:

```
  ┌──────────────┐     ┌──────────────┐
  │  zoom_scan   │     │ drive_scan   │   two independent indexes,
  │  (Zoom API)  │     │ _md5 (Drive) │   neither trusted over the other
  └──────┬───────┘     └──────┬───────┘
         │                    │
         └────────┬───────────┘
                  ▼
          ┌───────────────┐
          │  reconcile    │  per-file byte match → per-meeting verdict
          └───────┬───────┘
                  │
     ┌────────────┼────────────┬──────────────┐
     ▼            ▼            ▼              ▼
SAFE_TO_DELETE  PARTIAL   NOT_BACKED_UP    NO_MEDIA
     │            │            │              │
     │        (backfill)   (backfill)     (never deleted)
     ▼
 ┌────────────┐
 │ run_delete │  trashes media only, transcripts kept, Drive-gated
 └────────────┘
```

Four design decisions carried most of the safety, and each came out of a real
failure:

**The Drive index is a multiset, not a set.** If a meeting has two distinct
media files that happen to be the same byte length, the archive must contain
*two* files of that length. Matching against a set would have cleared such a
meeting for deletion while one of its files had never been uploaded. Matches are
consumed as they are made — [`verification.py`](zoomarchive/verification.py).

**`PARTIAL` is a first-class verdict.** A meeting with some media archived and
some missing is the genuinely dangerous case, and it is never auto-deleted. It
goes to the backfill queue instead. Collapsing it into either "safe" or
"missing" would have lost data.

**Metadata files are excluded from the gate and never deleted.** Transcripts,
chat logs and timelines are tiny, collide on size constantly, and Zoom's
reported size for them is unreliable. Every delete operation is media-only, so
the searchable text record of every meeting stayed in Zoom even after the video
was removed.

**Dedup is safe by construction, and the assertion is checked before execution.**
Drive *does* expose MD5s, so dedup could be exact: keep one copy per MD5.
The plan asserts that no unique file loses every copy before anything is
trashed. The first draft of that plan would have orphaned **179 files** — the
assertion is what caught it. Keeper preference goes to the copy in the most
complete folder, so redundant partial folders empty out cleanly instead of
leaving strays behind — [`dedupe.py`](zoomarchive/dedupe.py).

Deletions are also **soft** on both sides: Zoom trash and Drive trash each
retain items for ~30 days, so the whole operation had a rollback window.

### The error 3332 lock, and how it was cleared

134 recordings (268 once recent recordings were attempted) could not be deleted
through the per-file API. Zoom returned error 3332, *"being used for Zoom IQ for
Sales"* — Zoom's AI sales-intelligence product, whose subscription had already
been cancelled and whose UI was gone from the account. Zoom support confirmed a
**bug in the per-file delete endpoint** and advised deleting all 268 by hand
through the web portal.

Reading the API surface more closely turned up a different endpoint —
`DELETE /meetings/{uuid}/recordings`, which deletes a meeting's recordings as a
unit and is a **separate code path that doesn't hit the bug**. All 267 were then
deleted automatically. Because that endpoint removes the entire meeting
including its transcript, it is used *only* where the media *and* transcripts
were already verified in Drive — [`whole_meeting_delete.py`](scripts/whole_meeting_delete.py).

This turned roughly a day of manual portal clicking into a gated, logged,
resumable run.

---

## Automation — from laptop to pipeline

### Why it had to leave the laptop

Measured from the workstation: ~5–6 MB/s from Zoom, ~4.5 MB/s to Drive.
Parallelism didn't help — the local uplink (~36–51 Mbps) was the ceiling, not
per-connection throttling. At that rate the remaining ~914 GB backfill was
several days of a machine that couldn't sleep, and long transfers kept dying on
connection drops.

Moving the transfer to **Cloud Run** put both ends on Google's network. Drive is
a free-egress destination, so the entire backfill cost a few hours of compute.

One constraint shaped the implementation: Cloud Run's `/tmp` is **RAM-backed**,
and the largest single recording was 6.66 GB. The job therefore streams
Zoom → Drive in 8 MB chunks with no disk buffering, so memory stays flat
regardless of recording size.

### The steady-state pipeline

Clearing the backlog fixed the bill once. Keeping it fixed meant the same
guarantees had to run on every future recording with nobody in the loop — so the
verification gate was wired into an event-driven pipeline with no local machine
involved. This is the part that turns a one-time saving into a permanent one:

```
 Zoom: recording.completed
          │ (webhook, HMAC-signed)
          ▼
 ┌──────────────────┐   Cloud Run service — verifies the signature,
 │  zoom-webhook    │   triggers the job, returns 200 fast so Zoom
 └────────┬─────────┘   doesn't retry
          │
          ▼
 ┌──────────────────┐   Cloud Run job, three modes:
 │  zoom-autobackup │     sweep  — daily: back up + verify + delete (7-day window)
 └────────┬─────────┘     single — one meeting, fired by the webhook
          │               gap    — size-matched backfill of old partials, no delete
          ▼
 ┌──────────────────┐   Cloud Run service — token-gated. Headline metric is a
 │  zoom-dashboard  │   live Zoom→Drive reconciliation: "Unbacked in Zoom".
 └──────────────────┘   0 = everything is archived, right now.
```

Plus **Cloud Scheduler** for cadence: the sweep every 3 hours and a dashboard
refresh hourly, both restricted to 9am–9pm Eastern so nothing runs against an
idle account overnight.

The dashboard's design choice worth calling out: rather than reporting what the
pipeline *believes* it did from its own logs, it re-derives the answer from both
APIs on every refresh and shows the reconciliation itself. A log can be
confidently wrong; a live reconciliation against both sources cannot be. A
standalone [`local_snapshot.py`](dashboard/local_snapshot.py) renders the same
view to static HTML with no GCP dependency — which is what made the outage below
survivable.

### Operating it: a billing outage

The GCP project's billing account was closed upstream, which suspended every
Cloud Run service and job for ~12 days. The pipeline went dark while recordings
piled up in Zoom.

Recovery is in the repository because it exposed a real operational gap:
**re-enabling billing does not redeploy suspended revisions**, so relinking the
project was necessary but not sufficient — the services had to be explicitly
redeployed. A catch-up sweep then ran with the window widened from 7 to 14 days
(13 meetings, 6.7 GB backed up, 12 media files trashed) before reverting to the
daily cadence. The live reconciliation confirmed **0 unbacked** across the entire
gap — which is precisely the question a log-based dashboard could not have
answered after a 12-day blackout.

### Edge case: per-participant audio

Meetings recorded with *separate audio file per participant* produce extra files
that **do not appear** in the normal `recording_files` listing. They surface only
under `participant_audio_files` on the by-UUID detail endpoint, which needs its
own scope. Until that was found, those meetings looked complete and were not.
[`participant_audio_backfill.py`](scripts/participant_audio_backfill.py) handles
them through the same byte-gated path as everything else.

---

## API integrations

**Zoom API** (Server-to-Server OAuth, least-privilege scopes):

| Capability | Scope |
|---|---|
| List recordings across all host accounts | `cloud_recording:read:list_user_recordings:admin` |
| Per-meeting file detail, incl. participant audio | `cloud_recording:read:list_recording_files:admin` |
| Trash individual media files | `cloud_recording:delete:recording_file:admin` |
| Whole-meeting delete (the 3332 workaround) | `cloud_recording:delete:meeting_recording:admin` |
| Read the **billed** storage figure | `billing:read:plan_usage:admin` |
| Registrant administration | `meeting:delete:registrant:admin` |

**Zoom webhooks** — `recording.completed`, with endpoint URL validation and
HMAC-SHA256 event signatures computed over `v0:{timestamp}:{raw body}`. The
signature covers the *raw* bytes, so it must be verified before the JSON is
re-serialised — a detail that is easy to get wrong and is
[covered by tests](tests/test_webhook_auth.py).
See [`webhook/auth.py`](webhook/auth.py).

**Google Drive API v3** — shared-drive support throughout
(`supportsAllDrives`, `corpora=allDrives`), chunked resumable uploads, and MD5
checksums for dedup. Backfill throughput is capped by Drive's **per-user** write
rate limit, since all writes go through a single OAuth user, so the job runs at
modest concurrency with exponential backoff on Drive 403s. Media retries hard;
tiny metadata files fail fast so they can never stall a run.

**Google Cloud** — Cloud Run jobs and services, Cloud Scheduler, Secret Manager
for the Drive refresh token, and the Cloud Run Admin API for job triggering with
per-invocation environment overrides.

**Zoom AI features as constraints, not consumers.** Two of Zoom's AI products
shaped this project from the outside. Zoom IQ for Sales held 267 recordings
hostage through a delete-API bug long after it was cancelled. And Zoom's
AI-generated meeting summaries and transcripts were treated as first-class
assets to preserve — which is the specific reason every deletion is media-only
and why the whole-meeting endpoint is restricted to meetings whose transcripts
are already verified in Drive.

**On how this was built.** The tooling was developed in an agentic AI coding
workflow (Claude Code) against live APIs — which is also why the safety
architecture looks the way it does. When code that can irreversibly delete a
client's data is being generated quickly, the interesting engineering moves into
the verification gates, the pre-execution assertions, the dry-run modes and the
append-only audit logs. Every destructive operation here has to get past a check
that was written to assume the caller might be wrong.

---

## Testing

Two layers, because they catch different things.

### Production safety mechanisms

These ran against live data and each one caught a real bug:

- **Strict byte verification.** An upload counts as backed up only when Drive's
  reported bytes equal Zoom's. On mismatch the partial upload is **deleted**, so
  it can never later be mistaken for a good backup.
- **Pre-execution assertions.** The dedupe plan is checked for orphaned files
  before anything is trashed, and aborts rather than proceeding. This caught 179
  files.
- **Dry-run mode** on every destructive job (`DRY_RUN=true`).
- **Resumable, idempotent deletes.** Every operation appends completed ids to a
  log and skips them on re-run, so an interruption never causes double work.
- **Uploads skip names already present** in the target folder, so retrying a
  partial run cannot create duplicates.
- **Append-only audit logs** of every file id trashed in Zoom, trashed in Drive,
  and uploaded.

### Unit tests

The pure decision logic — the rules that decide whether data may be deleted —
lives in [`zoomarchive/`](zoomarchive/) with no I/O, no network and no
credentials, so it can be tested directly. The scripts do the I/O and call in
for the verdict.

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

```
39 passed in 0.04s
```

| Suite | Covers |
|---|---|
| [`test_verification.py`](tests/test_verification.py) | The deletion gate: all four verdicts, the multiset rule, the media threshold boundary, index immutability across meetings, post-upload byte checks |
| [`test_dedupe.py`](tests/test_dedupe.py) | One copy per MD5 always survives, orphan detection, keeper preference, determinism, missing-MD5 handling |
| [`test_webhook_auth.py`](tests/test_webhook_auth.py) | Valid signatures pass; tampered bodies, wrong secrets, timestamp mismatches and re-serialised JSON all fail |
| [`test_naming.py`](tests/test_naming.py) | Path-hostile meeting titles, length caps, layout names never colliding |

The tests are written to fail on the specific mistakes that would cause data
loss, not to chase coverage. Two examples, both verified by mutating the source
and confirming the suite goes red:

- Swap the Drive size multiset for a plain set →
  `test_duplicate_sizes_need_duplicate_backups` fails.
- Let `PARTIAL` return `SAFE_TO_DELETE` →
  `test_some_media_missing_is_partial_never_safe` fails.

---

## Results

### Business impact

| | |
|---|---|
| **Recurring cost eliminated** | **$1,000+/month** Zoom storage expense |
| **Storage** | **122% over** the 1 TB cap → **34.7 GB of 1 TB**, inside the free tier |
| **Migrated** | **3+ TB** of recordings into Google Workspace capacity already paid for (115+ TB free) |
| **Recordings lost** | **0** |
| **Ongoing manual work** | **none** — event-driven pipeline, not a one-time migration |

The cost line is the headline, but the last line is the one that makes it hold.
A manual migration would have bought a few months before new recordings pushed
the account back over its cap and back onto the overage bill. Because every new
recording is now archived, verified and cleared from Zoom automatically, the
saving is permanent rather than deferred.

The second-order win: the destination was capacity the client was **already
paying for**. The project did not trade one bill for another — it moved data
from metered storage into 115+ TB of unused Workspace allocation.

### Engineering results

| | |
|---|---|
| Meetings verified backed up | 1,542 |
| Media reclaimed from Zoom (round 1) | ~961 GB across 5,912 files — transcripts kept |
| Redundant Drive copies removed | 4,581 files, ~1,169 GB |
| Later deletion rounds | ~878 files, ~328 GB |
| Error-3332 meetings cleared automatically | **267 meetings, 585 GB** |
| Manual portal work avoided | ~a day of clicking, replaced by a gated logged run |
| Unbacked recordings in Zoom | **0**, confirmed by live reconciliation |
| Unit tests over the deletion logic | 39, passing |

Deletions were soft on both sides — Zoom trash and Drive trash each retain items
~30 days — so the entire operation ran with a rollback window rather than on
faith.

---

## Repository layout

```
zoomarchive/     pure decision logic — the deletion gate, dedup planning, naming.
                 No I/O. Unit-tested.
scripts/         local analysis + operations tooling (see below)
cloud/           Cloud Run job: bulk Zoom → Drive backfill, chunked + byte-verified
autobackup/      Cloud Run job: ongoing sweep / single / gap modes
webhook/         Cloud Run service: signed recording.completed receiver
dashboard/       Cloud Run service: live reconciliation dashboard + static snapshot
files/           earlier cloud-function iteration, kept for provenance
fixtures/        generated sample data matching the real schemas
tests/           pytest suite over zoomarchive/ and webhook/auth.py
```

### Key scripts

| Script | Purpose |
|---|---|
| [`zoom_scan.py`](scripts/zoom_scan.py) | Walk all Zoom cloud recordings → `zoom_recordings.json` |
| [`drive_scan_md5.py`](scripts/drive_scan_md5.py) | Walk the Drive archive with MD5s → `drive_index_md5.json` |
| [`reconcile.py`](scripts/reconcile.py) | Classify every meeting: safe / partial / not backed up |
| [`verify_folder.py`](scripts/verify_folder.py) | Confirm a meeting's media sit together in one Drive folder |
| [`run_delete.py`](scripts/run_delete.py) | Trash Zoom media, Drive-gated, resumable, transcripts kept |
| [`whole_meeting_delete.py`](scripts/whole_meeting_delete.py) | The error-3332 workaround, gated on transcripts being archived too |
| [`build_dedupe_plan.py`](scripts/build_dedupe_plan.py) | Build the MD5 dedupe plan; aborts if any file would be orphaned |
| [`drive_trash.py`](scripts/drive_trash.py) | Execute the plan, with a keeper-exists guard |
| [`backup_par.py`](scripts/backup_par.py) | Parallel Zoom → Drive backup with strict verification |
| [`participant_audio_backfill.py`](scripts/participant_audio_backfill.py) | The hidden per-participant audio files |
| [`land.py`](scripts/land.py) | Full current-state reconciliation → spreadsheet |
| [`storage_breakdown.py`](scripts/storage_breakdown.py) | The layout-duplication analysis that explained the billing gap |
| [`compare_prior_audit.py`](scripts/compare_prior_audit.py) | Diff this reconciliation against the earlier (cache-based) audit |
| [`audit_ghost_deletions.py`](scripts/audit_ghost_deletions.py) | Find already-trashed recordings still inflating scan totals |
| [`remove_registrant.py`](scripts/remove_registrant.py) | Hard-delete registrants by email, no cancellation email, audit-logged |

<details>
<summary><strong>A registrant-management trap worth documenting</strong></summary>

Adding a passcode to a registration-enabled meeting **rotates every
registrant's join link**. If `registrants_email_notification` is on, saving that
edit emails every registrant their new link — with no confirmation prompt; the
toggle alone decides. That toggle can only be changed with a `meeting:update`
scope.

Hard-deleting a registrant (`DELETE .../registrants/{id}`) sends **no** email,
unlike cancel or deny. That difference is the entire reason
`remove_registrant.py` uses hard delete.
</details>

---

## Running it

Nothing here will run against a real account without credentials, and no
credentials are in this repository.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py     # then fill in, or export the env vars
```

Required configuration:

| Variable | Purpose |
|---|---|
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` | Zoom Server-to-Server OAuth app |
| `DRIVE_ROOT_FOLDER_ID` | Drive folder holding the archive tree |
| `DRIVE_TOKEN_JSON` | Path to the Google OAuth refresh token |
| `WEBHOOK_SECRET` | Zoom webhook Secret Token |
| `DASH_TOKEN` | Dashboard access token |

### Fixtures

The real indexes describe private meetings and are not in this repository.
`fixtures/` holds deterministically generated stand-ins with the exact same
schemas, so the tooling can be run and read end to end:

```bash
python3 fixtures/make_fixtures.py      # regenerate (deterministic)
python3 scripts/reconcile.py fixtures
python3 scripts/build_dedupe_plan.py fixtures
python3 scripts/storage_breakdown.py
```

`reconcile.py` on the fixtures produces the same shape of verdict the real run
did, and `storage_breakdown.py` reproduces the layout-duplication finding —
155 GB of files against 72 GB if only the largest layout per meeting were
billed, the same ~2x effect that explained the real account's numbers.

Scripts that talk to a live API are read-only by default or require
`DRY_RUN=false` to write.

---

## License

MIT — see [LICENSE](LICENSE).
