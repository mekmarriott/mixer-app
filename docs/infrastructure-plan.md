# Infrastructure plan — Vercel deployment, storage, and ingestion at 10k tracks

*Companion to `latency-report.md` (which covers compute latency) and
`dj-app-project-plan.md` (which sketched hosting). This document covers what
is actually missing to deploy, and sizes storage against a real 10,000-track
catalog. Numbers marked **measured** come from `backend/benchmark.py` output
recorded in `latency-report.md`; numbers marked **computed** come from the
sizing scripts described in §3. Vendor prices are as of writing and must be
re-checked before committing — see §8.*

## 0. Verdict up front

1. **The app cannot deploy to Vercel as written.** Not a config gap — five
   structural assumptions (local SQLite file, local audio files, startup
   ingestion, `ffmpeg`/`rubberband` subprocesses, full-catalog scans) are each
   individually fatal on a serverless platform. §1 lists them.
2. **There is no free storage tier that holds 10,000 tracks**, with or
   without variants, on Supabase Storage, Vercel Blob, or Cloudflare R2. The
   largest relevant free tier (R2, 10 GB) holds ~347 four-minute tracks with
   today's variant policy, or ~3,472 with no variants at all. §3–§4.
3. **The database side is free, easily — but only after a schema change.**
   Today's `analysis_json` column is **12.4 GB at 10k tracks** (computed),
   25× the Supabase free tier. Moving frame arrays out of Postgres and into
   object storage as float32 drops the DB to ~50 MB, which is 10% of the free
   tier and free forever. §3.2.
4. **The single highest-leverage change is narrowing the BPM grid.** Going
   from 9 grid points per bucket to 3 costs about 1% additional worst-case
   time-stretch — inaudible — and removes 60% of all stored bytes. §4.3.
5. **Ingest locally, serve from the cloud.** Local ingestion is free, has no
   timeout ceiling, already has `ffmpeg` and `rubberband` on this machine, and
   materially defuses the AGPL/GPL exposure flagged in `requirements.md` §3.
   Its one real cost is upload bandwidth. §5.

Recommended end state, 10k tracks: **Vercel** (static frontend + thin read
API) → **Supabase Postgres** (scalars only, free tier) → **Cloudflare R2**
(audio + analysis blobs, ~$1.58/mo) → **local batch ingestion**.

---

## 1. Vercel readiness — what is missing

### 1.1 Files that do not exist in the repo

| File | Why it is needed |
|---|---|
| `requirements.txt` | Vercel's Python builder installs from it. Present as an untracked file in the main checkout; not committed, so a deploy would install nothing. |
| `vercel.json` | Routes `/api/*` to the function, serves `frontend/` statically, sets `maxDuration` and the Python runtime. |
| `api/index.py` | Vercel's Python runtime discovers a WSGI app at `api/`. `backend/app.py` only builds the app under `if __name__ == "__main__"`. |
| `.gitignore` | Untracked in the main checkout. Must ignore `data/`, `.env*`, `__pycache__/`, `.vercel/`. Without it a `data/` directory of WAVs can be committed. |
| `.vercelignore` | Keep `tests/`, `docs/`, `data/`, and `config/` out of the function bundle — see the size limit in §1.3. |
| `.env.example` | `JAMENDO_CLIENT_ID`, `DATABASE_URL`, storage credentials, `DJMIXER_DATA`. Nothing currently documents the deploy-time environment. |
| `pyproject.toml` (or project setting) | Pin the Python version. README says 3.12; the system `python3` on this machine is 3.9.6, so the version is currently implicit and unpinned. |

### 1.2 Code assumptions that break under serverless

Each of these is a blocker, not a warning.

1. **SQLite on a writable local disk.** `db.connect()` opens
   `data/catalog.sqlite3` and runs `executescript(SCHEMA)` — a write — at
   `create_app()` time (`backend/app.py:24`). Vercel's filesystem is read-only
   except `/tmp`, and `/tmp` is per-instance and discarded. Every cold start
   would get an empty database. Must become Postgres.

2. **Startup ingestion.** `create_app()` runs `ingest.ingest_all()` when the
   catalog is empty (`backend/app.py:27`). On Vercel this fires on every cold
   start of every instance, against a read-only filesystem, inside the
   function timeout. Must be `run_ingestion=False` in the serverless entrypoint
   and the ingestion path must move out entirely (§5).

3. **Audio served from local paths.** `send_file(t["audio_path"])` and
   `send_file(v["path"])` (`backend/app.py:113-121`) read absolute paths
   recorded at ingest time. Those files do not exist in the function. This
   must become a **302 redirect to an object-storage URL** — and it must be a
   redirect, not a proxy: streaming a 2.9 MB (compressed) or 10.6 MB (WAV)
   body through a Vercel function bills the transfer and holds the function
   open for the whole download.

4. **`ffmpeg` and `rubberband` subprocesses.** `jamendo._fetch_jamendo()`
   shells out to `ffmpeg`; `stretch._stretch_rubberband()` shells out to
   `rubberband`. Neither binary exists in Vercel's Python runtime, and
   vendoring static builds would consume most of the bundle budget. Confirmed
   present locally (`/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/rubberband`)
   — which is an argument for local ingestion.

5. **Full-catalog scans in request handlers.** Two separate problems, and the
   second is worse than `latency-report.md` suggests:

   - `db.all_tracks()` is `SELECT *` (`backend/db.py:81`), so it pulls
     `analysis_json` and `segments_json` for every row. `/api/tracks` at 10k
     tracks would read ~12.4 GB from the database to return a metadata list.
   - `matching.recommend()` calls `db.analysis_of()` and `db.segments_of()`
     **per candidate** inside its loop (`backend/matching.py:113-127`). Each
     call is a fresh query returning a ~1.2 MB JSON document that is then
     parsed. The latency report projects "~28s" at 10k tracks by scaling the
     9-track mean; that projection assumes the per-track cost stays constant,
     but it does not — the dominant term is fetching and `json.loads`-ing
     gigabytes. The real figure is far worse and will exceed any function
     timeout.

   Both are fixed by the schema split in §3.2: `recommend()` becomes a single
   indexed SQL query over scalar columns, loading no blobs at all.

6. **Unpaginated catalog to the client.** `frontend/js/app.js:414` does
   `catalog = await api.tracks()` — the entire catalog, once, on load. Fine
   for 9 tracks; a multi-megabyte JSON payload at 10k. Needs pagination plus
   server-side search/filter.

### 1.3 Platform constraints to design against

- **Bundle size.** Vercel serverless functions cap at 250 MB uncompressed.
  `backend/app.py` imports `ingest` at module scope, which transitively
  imports `analysis` and `stretch`, both of which import `scipy` at module
  top. So a request-serving cold start currently loads the entire numpy+scipy
  stack, which commonly lands in the 100–200 MB range unpacked. **This is the
  most likely first deploy failure and must be measured before anything
  else.** The fix is cheap: the serving path needs no scipy at all — only
  `transitions.py` and `matching.py` touch numpy, and `rescale_analysis` is
  pure Python arithmetic. Restructure so `api/index.py` imports neither
  `ingest` nor `stretch`.
- **Function duration.** Hobby is roughly 60 s; Pro extends further with Fluid
  compute. Even the measured **4.73 s/track** ingestion of 60-second synthetic
  tracks does not fit a batch job in that envelope, and real 4-minute tracks
  through Rubber Band are an order of magnitude worse (§5.1).
- **Cron.** Hobby cron is limited to daily invocations, which is not a job
  queue. Scheduled re-ingestion needs a real worker.
- **Connection pooling.** Serverless creates many short-lived connections.
  Use Supabase's transaction pooler endpoint (port 6543), not the direct
  connection, or connection limits will be exhausted under modest concurrency.
- **CORS.** `frontend/js/audio.js` uses `fetch()` + `decodeAudioData()`. Once
  audio lives on a different origin (R2/Blob/CDN), that bucket needs CORS
  headers allowing the Vercel origin. Easy to miss until playback silently
  fails in the browser.
- **Supabase free-tier pausing.** Free projects suspend after about a week of
  inactivity and need a manual restore. For a demo shown intermittently this
  is a real operational papercut; Neon's autosuspend resumes on its own and is
  friendlier for this pattern.

### 1.4 Audio format — a correctness issue, not just a size one

Today everything is 22.05 kHz mono 16-bit WAV. Compressing is necessary (§3.1),
but the choice interacts with beat alignment:

- **MP3 and AAC both carry encoder delay** (AAC-LC priming is 2112 samples).
  Browsers apply the container's edit list inconsistently in
  `decodeAudioData`, so two decoded buffers can sit ~48 ms apart. At 124 BPM a
  beat is 484 ms, so that is ~10% of a beat — a phase error that is audible in
  exactly the crossfade this app exists to get right.
- **Mitigation:** store `encoder_delay_samples` per variant at encode time and
  trim client-side in `audio.js` before scheduling, or keep the two actively
  loaded variants in a delay-free format (WAV/FLAC) and use compressed audio
  only for preview.
- **Codec choice:** AAC-LC in `.m4a` decodes everywhere including Safari.
  Opus is smaller at equal quality but Safari's `decodeAudioData` support for
  it has historically been unreliable. Recommend AAC-LC 96 kbps unless Safari
  is out of scope.

---

## 2. What the existing docs get wrong

Two inconsistencies worth correcting in place:

- `dj-app-project-plan.md` sizes storage as "500 tracks × 20 BPM-grid variants
  × ~4MB". The implemented buckets in `backend/config.py` are 9 points (house
  120–128) and 11 points (downtempo 85–95), not 20, and the measured average
  is ~8.7 rendered variants per mixable track. The 20× figure overstates
  variant count but the ~4 MB/track figure understates real 4-minute
  compressed audio only slightly, so the two errors partly cancel.
- `latency-report.md`'s storage projections cover audio only. They omit the
  analysis JSON entirely, which is the item that actually breaks the free
  database tier (§3.2).

---

## 3. Sizing against 10,000 tracks

Assumptions: 10,000 tracks, 4-minute production length, ~9 grid variants per
mixable track (today's `BPM_BUCKETS` + `MAX_STRETCH_RATIO = 0.10`).

### 3.1 Audio — computed totals

Per 4-minute file:

| Encoding | Size |
|---|---|
| WAV 22.05 kHz mono 16-bit (today) | 10.58 MB |
| WAV 44.1 kHz stereo 16-bit | 42.34 MB |
| MP3 128 kbps stereo | 3.84 MB |
| **AAC-LC 96 kbps stereo (.m4a)** | **2.88 MB** |
| Opus 64 kbps mono | 1.92 MB |

Total for 10,000 tracks, by variant policy (GB / Cloudflare R2 $ per month
after its 10 GB free allowance, at $0.015/GB-mo):

| Variant policy | WAV (today) | MP3 128k | AAC 96k | Opus 64k mono |
|---|---|---|---|---|
| master + 9 variants (today) | 1058 GB / $15.73 | 384 GB / $5.61 | **288 GB / $4.17** | 192 GB / $2.73 |
| master + 3 variants | 423 GB / $6.20 | 154 GB / $2.15 | **115 GB / $1.58** | 77 GB / $1.00 |
| master + 1 canonical variant | 212 GB / $3.03 | 77 GB / $1.00 | **58 GB / $0.71** | 38 GB / $0.43 |
| master only (stretch on demand) | 106 GB / $1.44 | 38 GB / $0.43 | **29 GB / $0.28** | 19 GB / $0.14 |

Two things fall out. First, **compressing is worth more than any other single
change** — 3.7× on its own. Second, once compressed, even the worst policy is
about four dollars a month, so the real question is not cost but which free
tier it does or does not fit into (§4).

### 3.2 The database — the finding that is not in the latency report

`analysis.analyze()` stores three per-frame arrays plus three prefix-sum
arrays, serialized as JSON with full float precision (`frame_features()`
calls `.tolist()` with no rounding). At hop 512 / 22.05 kHz that is 43 frames
per second.

Computed, per track:

| | 60 s track (bench) | 240 s track (production) |
|---|---|---|
| Frames | 2,583 | 10,335 |
| **`analysis_json` as stored today** | **0.309 MB** | **1.239 MB** |
| Frames only, float32 binary | 0.031 MB | 0.124 MB |
| Frames + prefix, float32 binary | 0.062 MB | 0.248 MB |
| Frames float32, decimated to 10 Hz | 0.007 MB | 0.029 MB |

**At 10,000 four-minute tracks that is 12.4 GB of JSON in a column** — 25×
the Supabase free tier's 500 MB, and it would be read in full by both
`/api/tracks` and every `recommend()` call (§1.2 item 5).

Storing the same data as float32 binary is a 10× win, and dropping the prefix
sums (they are a `cumsum` away from the frames, recomputable in microseconds
on load) is another 2×. But the better move is to notice what request-time
code actually needs:

| Endpoint | What it truly needs |
|---|---|
| `/api/tracks` | Scalars only. |
| `/api/tracks/<id>/waveform` | ~480 downsampled envelope points + beat grid. Today it loads all 10,335 frames to emit 300. |
| `/api/tracks/<id>/recommendations` | Camelot, grid points, and two energy scalars (mean RMS of A's outro, of B's intro). **No arrays at all.** |
| `/api/transitions?a&b` | Full prefix sums — but for exactly two tracks. |

So split by access pattern:

**Postgres (Supabase free tier) — scalars and small fixed-size blobs:**
`id, name, artist, genre, license, license_nd/sa/nc, mixable, native_bpm,
camelot, camelot_num, camelot_letter, duration_s, intro_rms, outro_rms,
energy_mean, master_url`, plus `waveform_480` and `beat_grid` as `bytea`
float32 (~2 kB each) and `segments` as small `jsonb`.
→ ~5 kB/track × 10,000 = **~50 MB, or 10% of the free tier.** Free forever.

**Object storage — `analysis/{id}.npz`**, float32 frames (~124 kB/track,
1.2 GB total), fetched only by `/api/transitions`, two objects per request.

Precomputing `intro_rms`/`outro_rms` as columns is what converts
`recommend()` from "load and parse the catalog" into one indexed SQL query:
join `variants` on shared `grid_bpm`, filter by Camelot neighbourhood, order
by a scored expression. That is the mitigation `latency-report.md` gestures at
under "recommend needs indexing", made concrete.

---

## 4. Storage options compared

### 4.1 The free-tier question, answered directly

You asked whether anything is free for both tracks and variants. It is not,
and it is not close. Free allowances relevant here (verify before relying on
them — see §8):

| Provider | Free storage | Free egress | Egress rate after |
|---|---|---|---|
| Supabase Storage | ~1 GB | ~5 GB/mo | ~$0.09/GB |
| Vercel Blob (Hobby) | ~1 GB | ~10 GB/mo | ~$0.05/GB |
| Cloudflare R2 | **10 GB** | **unlimited, $0** | **$0** |
| Backblaze B2 | 10 GB | 3× stored/mo | $0.01/GB |

What 10 GB — the most generous of them — actually holds, in 4-minute tracks:

| Encoding | + 9 variants | + 3 variants | + 1 variant | master only |
|---|---|---|---|---|
| WAV (today) | 94 | 236 | 472 | 944 |
| AAC 96k | 347 | 868 | 1,736 | 3,472 |
| Opus 64k mono | 520 | 1,302 | 2,604 | 5,208 |

Even the most aggressive combination — Opus 64 kbps mono, zero variants, which
would gut both audio quality and the app's core pre-rendering design — tops out
around 5,200 tracks. **10,000 tracks does not fit in any single free tier.**
Sharding across R2 + B2 + Supabase + Blob would reach ~22 GB, which is still
short and is not an architecture worth operating.

The honest reframing: audio storage is not where you should be trying to save
money, because compressed it costs **$1.58/month** at the recommended policy.
The database is where "free" is genuinely achievable and durable, and §3.2
gets you there.

### 4.2 Which of your two available options to use where

You have Supabase and Vercel Blob available. They should be used for different
things, and neither should hold bulk audio at 10k scale.

- **Supabase Postgres — yes, for metadata.** After the §3.2 schema split this
  is ~50 MB and stays free indefinitely. Watch the free-tier pausing behaviour
  (§1.3) and use the transaction pooler.
- **Supabase Storage — no.** 1 GB free and ~$0.09/GB egress. For an audio app
  the egress is the disqualifier, not the storage.
- **Vercel Blob — yes, but only at prototype scale.** It is the lowest-friction
  option: one vendor, one token, no extra account. At a few hundred tracks it
  is genuinely the right call. At 10k it fails on egress, not capacity:
  1,000 mix sessions/day × 2 tracks × 2.88 MB ≈ **173 GB/month**, which is
  **$8.64/mo on Blob, $15.55 on Supabase Storage or S3, and $0.00 on R2** —
  and that number scales linearly with users while storage does not.
- **Cloudflare R2 — the recommendation at 10k.** Zero egress is decisive for a
  workload whose bytes are almost entirely audio delivery. This is the same
  conclusion `dj-app-project-plan.md` already reached; the numbers above
  confirm it rather than revisiting it.

A reasonable path is to start on Vercel Blob for the first few hundred tracks
and move to R2 when the catalog or the traffic justifies it. Keep object keys
provider-agnostic (`masters/{id}.m4a`, `variants/{id}_{grid_bpm}.m4a`,
`analysis/{id}.npz`) and store only the key in Postgres, resolving to a full
URL through a single configurable base. Then the migration is a bucket copy
plus one environment variable, not a schema change.

### 4.3 The lever that matters most: narrow the BPM grid

Storage is 90% variants. `BPM_BUCKETS` currently renders 9 points for house
and 11 for downtempo. That width buys less than it appears to.

Two tracks are mix-compatible when they share a grid point, so a **narrower
grid does not reduce compatibility at all** — with 3 points per bucket, every
house track still shares all three with every other house track. What the grid
width buys is only a smaller *stretch amount* for pairs whose native tempos
happen to be close.

- 9 points spaced 1 BPM across 120–128: worst-case stretch to the nearest
  point is 0.5 BPM ≈ **0.4%**.
- 3 points at {121, 124, 127}: worst case is 1.5 BPM ≈ **1.2%**.
- 1 point at {124}: worst case is 4 BPM ≈ **3.2%** — still well inside the
  10% cap, and inside the ±6% pitch range DJs use routinely.

The difference between 0.4% and 1.2% stretch through Rubber Band is not
audible. **Dropping 9 points to 3 removes 60% of stored bytes** (288 GB →
115 GB at AAC 96k) for that. Dropping to 1 removes 80% (→ 58 GB) but changes
product behaviour: `bpm_score` stops being a shared-grid property and becomes
a function of distance from the bucket centre, which arguably reads better but
is a design decision rather than an optimisation, so it is offered rather than
recommended.

The one-line version: `BPM_BUCKETS = {"house": [121, 124, 127],
"downtempo": [87, 90, 93]}`.

---

## 5. Ingestion: cloud-pull vs. local-pull-and-push

### 5.1 Compute cost, from measured numbers

`latency-report.md` measures **3.44 s** to render ~8.7 variants of a 60-second
track with the phase vocoder — about 0.0074 s per variant-second of audio.
Scaling to 4-minute tracks at 9 variants gives ~16 s/track; Rubber Band's
high-quality offline mode is typically 2–5× slower, so **~50–80 s/track
single-core**, plus analysis and transcode. Call it ~60 s/track.

**10,000 tracks ≈ 167 core-hours.** That is the number both options are
paying, one way or another. It is a one-time batch cost, not a running cost.

### 5.2 Option A — the deployed app pulls tracks

This cannot run on Vercel functions at all: no `ffmpeg`, no `rubberband`, no
writable disk, and a 60 s timeout against a 60 s/track job. It requires a
separate worker:

- **Vercel Sandbox** — ephemeral Firecracker microVMs that can install
  arbitrary binaries. Genuinely viable, fans out well, stays inside the Vercel
  account. Billed per CPU/memory-hour, and per-sandbox duration is bounded, so
  10k tracks means chunking into batches with a queue and resume logic.
- **Render / Railway / Fly worker + job queue** (Postgres-backed or Upstash).
  Conventional and robust. Free tiers are CPU-throttled and sleep, so this is
  a paid tier in practice.
- **GitHub Actions** — free and unmetered for public repositories, 2,000
  min/mo for private. 167 core-hours does not fit the private allowance
  (33 hours), but for a public repo a matrix of parallel jobs is a real free
  option, if an unusual one.

**For:** catalog grows without a laptop; scheduled re-ingestion; multiple
contributors; no upload bottleneck (the worker writes straight to the bucket
in-datacentre).
**Against:** a second piece of infrastructure to build and operate — queue,
retries, idempotency, secrets, monitoring; real CPU-hour cost; and it puts
Essentia and Rubber Band on a network-accessible server (§5.4).

### 5.3 Option B — ingest locally, push to the remote

Run the existing pipeline on this machine; `ffmpeg` and `rubberband` are
already installed. Then upload audio to the bucket and rows to Postgres.

**Compute:** 167 core-hours across 8–10 performance cores ≈ **17–21 wall-clock
hours**. Two overnight runs, or one weekend.

**Upload is the real constraint**, and it is what should drive the variant
policy:

| Policy (AAC 96k) | Upload | At 50 Mbps up | At 20 Mbps up |
|---|---|---|---|
| master + 9 variants | 288 GB | ~12.8 h | ~32 h |
| master + 3 variants | 115 GB | ~5.1 h | ~12.8 h |
| master + 1 variant | 58 GB | ~2.6 h | ~6.4 h |
| master only | 29 GB | ~1.3 h | ~3.2 h |

**For:** zero compute cost on hardware you already own; no timeout or bundle
ceiling; Essentia, Rubber Band, and `ffmpeg` all trivially installable;
failures are debuggable locally; and the licensing benefit in §5.4.
**Against:** the catalog only grows when your laptop is on; needs a genuinely
resumable uploader; home upstream bandwidth is the bottleneck.

**What it needs built:** a `backend/publish.py` CLI that is idempotent and
resumable — content-hash each object, skip what already exists in the bucket,
upsert Postgres rows in batches, and record per-track state so an interrupted
run continues rather than restarts. This is the main new code either option
requires, and it is smaller here than the queue infrastructure Option A needs.

### 5.4 The licensing argument for Option B

`requirements.md` §3 flags two unresolved copyleft questions: whether
Essentia's AGPL is triggered by running it server-side behind a network API,
and whether Rubber Band's GPL is triggered by the server-side rendering
architecture.

**Local ingestion largely removes both questions rather than answering them.**
AGPL §13's network clause is triggered by users interacting with the software
remotely over a network. If Essentia only ever runs on your machine and the
deployed service receives its *output* — BPM numbers, key strings, float
arrays — then no user ever interacts with Essentia over a network, and you are
a mere user of the software rather than an operator of a service built on it.
The same reasoning applies more straightforwardly to Rubber Band, whose plain
GPL triggers on distribution, which batch-rendering on a laptop does not do.

Option A puts both libraries on a network-accessible server and keeps both
questions live. This is a substantive architectural consequence of the choice,
not a footnote — though, per the standing note in those docs, it is reasoning
about license terms and not legal advice, and counsel should still confirm it
before a real launch. Attribution and credits-page obligations are unaffected
either way.

### 5.5 Recommendation

**Option B, with a narrowed grid.** Local ingest at 3 grid points per bucket:
~17–21 hours of compute you do not pay for, ~5 hours of upload, 115 GB at rest,
$1.58/month, and the copyleft exposure largely sidestepped. Option A only
becomes worth its complexity when the catalog needs to grow without you — which
is a product change, not an infrastructure one, and can be added later behind
the same `publish.py` interface.

A defensible middle path if upload bandwidth is the binding constraint: upload
masters and analysis only (29 GB, ~1.3 h), and render variants on demand in a
worker, caching results in the bucket. The working set is far smaller than the
catalog, so steady-state storage lands near the master-only row. This costs
first-play latency on a cold pair and adds the worker back, so it is worth it
only if the upload genuinely does not fit.

---

## 6. Recommended architecture

```
  Local machine                     Vercel                    Cloudflare R2
  ─────────────                     ──────                    ─────────────
  jamendo fetch                     frontend/ (static)        masters/{id}.m4a
  essentia / DSP analyze            api/index.py              variants/{id}_{bpm}.m4a
  rubberband render (3 pts)           GET /api/tracks         analysis/{id}.npz
  ffmpeg -> AAC 96k                   GET /api/tracks/:id            ▲
  publish.py ──────────────┐          GET .../waveform               │
        │                  │          GET .../recommendations   302 redirect
        │                  └────────► GET /api/transitions      (no proxying)
        │                              │
        ▼                              ▼
  Supabase Postgres  ◄──────────────  pooled connection (port 6543)
  scalars, waveform_480, beat_grid, segments   ~50 MB @ 10k tracks
```

Frontend and API stay in one Vercel project; audio never passes through a
function.

## 7. Sequenced plan

**Phase 0 — deployability (small catalog, prove the shape)**
1. Commit `requirements.txt`, `.gitignore`, `.vercelignore`, `.env.example`.
2. Add `api/index.py` → `create_app(run_ingestion=False)`; add `vercel.json`.
3. Remove the module-level `ingest` import from `app.py` so scipy leaves the
   serving path; **measure the bundle against the 250 MB limit before
   anything else** (§1.3).
4. ~~Swap SQLite → Supabase Postgres behind the existing `db.py` functions.~~
   **Being built on the `data-retrieval` branch** — see §9. This branch
   consumes it rather than duplicating it.
5. Move audio to Vercel Blob, `send_file` → 302 redirect, configure CORS.
   Deploy the 9-track demo catalog end to end.

**Phase 1 — make it survive scale**
6. Schema split: frames out of Postgres into `.npz`; add `intro_rms`,
   `outro_rms`, `waveform_480`, `camelot_num`/`camelot_letter` columns.
7. Rewrite `recommend()` as indexed SQL; index `variants(grid_bpm)` and
   `tracks(genre, camelot_num, mixable)`.
8. Replace `SELECT *` in `all_tracks()`; paginate `/api/tracks` and update
   `app.js:414`.
9. Transcode to AAC-LC 96k; store `encoder_delay_samples` and trim in
   `audio.js` (§1.4).
10. Narrow `BPM_BUCKETS` to 3 points per bucket.

**Phase 2 — the 10k catalog**
11. Build `publish.py`: resumable, content-hash-keyed, batch upserts.
12. Move buckets to R2, flip the URL base, verify zero-egress delivery.
13. Batch-ingest locally in tranches; validate recommendation quality at each
    tranche rather than only at the end.

## 8. To verify before committing

- **Bundle size** of the serving function with the dependency set actually
  needed. This gates Phase 0 and is cheap to measure.
- **All vendor prices and free allowances quoted here.** They change; treat
  §4.1 as the shape of the answer, not as current quotes.
- **Vercel Hobby vs Pro function duration and cron limits** at the time of
  deploy.
- **`decodeAudioData` behaviour** for AAC priming in the browsers you care
  about — measure the actual offset rather than assuming 2112 samples survive
  the round trip.
- **Jamendo bulk-fetch terms.** Pulling 10,000 tracks is a different
  relationship with the API than pulling 9, and rate limits and acceptable-use
  terms should be checked before a batch run of that size.

---

## 9. Coordination with the `data-retrieval` branch

That branch is concurrently building `backend/db/` as a package: a canonical
`sql/schema.sql` in engine-neutral type tokens, a `dialect.py` that renders DDL
and parameter styles for both SQLite and PostgreSQL, sqlc-style annotated query
files, and a `codegen.py` step that derives typed bindings from them.

**This is the Postgres migration this document asked for in §7 step 4, and it
is a better version of it than "a driver swap".** The dialect layer keeps
SQLite working locally while making Supabase the deployment target, which means
local ingestion (§5.3) and cloud serving can share one schema definition. This
branch should consume it, not duplicate it. Two pieces of §7 are now theirs:
the Postgres swap (step 4) and the recommendation index (step 7 — their
`idx_tracks_match ON tracks (mixable, genre, camelot)` is the right index).

Four points were raised with that session, ordered by how much cheaper they are
to change before `codegen.py` emits models and tests bind to the shape:

1. **`analysis_json JSONDOC` → JSONB is the 12.4 GB problem from §3.2**, now
   promoted into a canonical schema. The fix is the split in §3.2: frames and
   prefix sums out to object storage as float32, `intro_rms`/`outro_rms`/
   `waveform_480`/`beat_grid` in as fixed-size columns, `segments_json` stays.

2. **`SELECT *` in `GetTrack` / `ListTracks` / `ListMixableTracks`** carries the
   blob columns on every row (§1.2 item 5). Their `ListAllVariants` already
   eliminates the variants N+1, which is the same instinct applied to the other
   half of the problem. Needs an explicit column list or a summary query.

3. **`tracks.audio_path` and `variants.path` hold absolute local paths.** These
   must become provider-agnostic object keys (§1.2 item 3, §4.2) resolved
   through one configurable base URL, so Vercel Blob → R2 is an environment
   variable rather than a data migration.

4. **`dialect.TYPES` needs a `BLOB`/`BYTEA` token** for the float32 arrays in
   (1). Nothing else in the schema requires it today.

One requirement flows the other way: whatever `connect()` that package grows
must use a **pooled** PostgreSQL endpoint (Supabase transaction pooler, port
6543) when running under Vercel, per §1.3. That is a connection-layer concern
on their side, not a schema one.

Their `camelot TEXT` column plus `idx_tracks_match` does serve the
recommendation pre-filter adequately — the Camelot neighbourhood is a small
explicit `IN` list computed in Python, which uses the index fine. The
`camelot_num`/`camelot_letter` split floated in §7 step 6 is therefore optional
and should not be pushed if they prefer the single column.

---

## 10. Jamendo API limits (investigated)

Findings below separate what the vendor documents from what was observed
against the live service, because the two disagree in an important way.

### 10.1 What is documented

- **No published rate limit.** Neither the API reference nor the terms of use
  states a requests-per-second or per-minute figure. The terms reserve the
  right to "impose restrictions and limitations on the number and frequency of
  requests or calls made to the API" without predefining any.
- **35,000 requests/month** on the free tier is the commonly cited figure. It
  could not be confirmed at the source — Jamendo's own help page returns 403 —
  so it is treated as true for budgeting and never relied on as fact.
- **`/tracks` accepts an array of ids, `limit` max 200.** This is the single
  most important documented fact for this project: see §10.3.
- Commercial use requires a separate quote, per `dj-app-project-plan.md`.

### 10.2 What was observed on the live API

Measured against the real service (not projections):

- **No rate-limit headers of any kind.** No `X-RateLimit-*`, no `Retry-After`,
  no quota field in the body. The response set is Cloudflare-standard plus
  `x-powered-by: PHP/7.0.33`. There is therefore **no way to read remaining
  quota from the service** — a client must instrument its own counter, which
  is what `ratelimit.RequestBudget` exists for.
- **No 429s at 2 req/s.** 30 serial metadata calls at that rate returned 200
  throughout, and a few hundred requests across a session drew no throttling.
  That establishes a floor on the real ceiling, not the ceiling.
- **Audio downloads come from a different host** —
  `prod-N.storage.jamendo.com` (nginx), not the Cloudflare-fronted
  `api.jamendo.com`. Consistent with file fetches not counting against the API
  quota, though nothing in the response proves it, so `publish.py --dry-run`
  reports the assumption rather than burying it.
- That host returns **`Content-Type: text/html` for MP3 bytes**. Never sniff
  the content type; hand the payload to ffmpeg and let it detect the format.

### 10.3 The two findings that changed the implementation

**Batching is worth ~200x, and only one spelling of it works.** Repeating the
`id=` parameter does *not* batch — the API keeps the last value and returns a
single result. The ids must reach the wire as `id=a+b+c`. Joining on a space
produces exactly that once urlencoded; joining on a literal `+` is escaped to
`%2B` and silently breaks batching. With batching, metadata for 10,000 tracks
costs **~50 requests instead of 10,000** — 0.14% of the assumed monthly quota
instead of 29%.

**Empty results arrive as HTTP 200, and the loss is all-or-nothing.** A failed
lookup is not a 4xx or 5xx: it is a success response with `results: []` and
error code 0, invisible to any check that inspects only status or headers.
Observed failure rate for a single-id lookup was 27–50% depending on the
minute. Critically, a repeated 12-id batch returned 12/12 or 0/12 and **never a
partial** — so a short result set never means "those tracks were delisted", it
always means "retry".

The naive implementation is therefore quietly wrong in the worst way: accept
the first response and a 10,000-track import silently drops a fraction of the
catalog while reporting success. `jamendo._fetch_batch()` retries on short
results and raises `IncompleteBatch` rather than accepting one, and the
completeness assertion is covered by tests.

### 10.4 Compliance flag — the caching clause

Jamendo's API terms state that applications "**must not be specifically
designed to cache the content nor offering an offline access to the content**",
permitting caching "only to the extent reasonably necessary for the operation
of the Application."

This app permanently stores masters *and* rendered derivative variants. That
is at minimum in tension with the clause, and it is **a separate question from
the CC-license analysis already tracked in `requirements.md` §2** — a CC-BY
licence may permit the copy and the derivative while the API terms of the
service you obtained the file through separately restrict caching.

It is flagged, not resolved: it is a product and legal decision, not a
cleanup, and the variant pre-rendering is the application's core mechanic.
It belongs on the `requirements.md` checklist before any 10,000-track batch
run, alongside the existing commercial-use items.

### 10.5 Resulting client policy

| Setting | Value | Rationale |
|---|---|---|
| Metadata batch size | 200 ids/request | documented max; ~50 requests per 10k tracks |
| API rate | 2.0 req/s default | highest rate observed clean; a floor, not a ceiling |
| Download concurrency | 2 default | **only 1 is verified safe** — raise deliberately |
| Retries on empty result | 6, exponential + full jitter | the 27–50% silent-failure mode |
| Backoff jitter | full (uniform in [0, d]) | stops parallel workers retrying in lockstep |
| Request budget | opt-in hard ceiling | a retry loop, not a burst, is what eats a monthly quota |

Rate limiting is shared process-wide and deliberately decoupled from CPU
parallelism: raising `--workers` from 1 to 8 speeds up rendering without
increasing the outbound request rate at all.

---

## 11. Phase 0 status

Implemented on the `worktree-infra` branch. 76 backend + 42 frontend tests pass.

**Done**

- Grid narrowed to 5 BPM spacing (`config.GRID_SPACING`). House 9 → 2 variants
  per track, downtempo 10.71 → 2.76 mean. Pairwise compatibility is unchanged
  at 100% within every bucket, verified by an exhaustive test over the full
  native range; only worst-case joint stretch moves, 3.33% → 4.17% for house
  and not at all for downtempo. Backend suite runtime fell from ~30 s to ~6 s,
  which is the same reduction showing up as compute.
- `backend/storage.py` — provider-agnostic key/URL seam with a filesystem
  backend (dev + tests, no credentials) and a Vercel Blob backend via the CLI.
- Audio endpoint returns **302 to the store** instead of streaming bytes.
- **scipy removed from the serving import graph** — `analysis`, `segmentation`
  and `stretch` now import it lazily, and `stretch` had a dead import. Verified
  by assertion, not inspection: `scipy in sys.modules` is `False` after
  importing the app. **Measured: 105 MB of scipy and 31 MB of numpy against a
  250 MB limit; the serving dependency set drops from ~158 MB to ~53 MB.**
  Note that lazy imports fix cold-start time but *not* bundle size — bundle
  size is set by what is installed, which is why `requirements.txt` (serving)
  and `requirements-ingest.txt` (local) are split.
- `api/index.py`, `vercel.json`, `.gitignore`, `.vercelignore`, `.env.example`.
- `backend/publish.py` — resumable parallel batch publisher, two pools
  (rate-limited threads for network, processes for CPU), only keys crossing
  the process boundary. Verified end to end: 9 tracks on 8 workers in 4.6 s,
  and a second run correctly reports "nothing to do".
- `backend/ratelimit.py` — token bucket, hard request budget, full-jitter
  backoff. Tested on a fake clock rather than by sleeping.

**Deliberately not done here**

- The Postgres swap and query layer are on the `data-retrieval` branch (§9).
  That branch also fixed the shared-connection concurrency defect, added FK
  cascade from `tracks` to `variants` (relevant to any bulk catalog rewrite),
  and adapts to the transaction pooler.
- The §3.2 analysis-blob split is still open and is the remaining large win.
  It is not a schema change alone: `analysis.py` must stop emitting the arrays,
  `matching.py` must read `intro_rms`/`outro_rms` instead of prefix sums, the
  waveform endpoint needs a stored envelope, and `transitions.py` must keep
  its O(1) window property. `audio_path` → `audio_key` and the `BLOB`/`BYTEA`
  dialect token belong in that same change.

**Added to the verify list in §8**

- ~~The Postgres path has never run against a live server.~~ **Done.**
  `make test-pg` brings up a project-local PostgreSQL cluster and runs the
  full backend suite against it: 114 tests pass, and the catalog lands with
  `boolean` / `double precision` / `jsonb` column types, confirming the
  dialect layer's type mapping executes rather than merely rendering. It also
  confirms `verify_schema()`'s `SELECT * LIMIT 0` returns positional order on
  Postgres. Two things it does **not** cover: Supabase's transaction pooler
  (a Supabase component — `prepare_threshold` and connection-limit behaviour
  still need a real instance), and any Supabase-specific auth or networking.
- Finding from that first run: `psycopg-pool` was missing from the serving
  manifest even though `engine.py` uses `psycopg_pool.ConnectionPool`. Nothing
  caught it because nothing had ever constructed a `PostgresEngine`. A deploy
  would have failed on first request.
- The 35,000 requests/month figure is still unconfirmed at the source (§10.1),
  and the API exposes no telemetry to confirm it against.

### 11.1 Known gap: there is no migration runner

`migrate()` on the `data-retrieval` branch only *creates*. Every statement in
`schema.sql` is `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`,
so against a database that already has the old shape it is a silent no-op.
There is no schema-version table and no `ALTER` path.

This is invisible in development and tests — the fixture and the e2e suite
build a fresh database every run, and locally you delete `data/`. It stops
being invisible the first time a Supabase instance holds rows worth keeping.

**It bit immediately, and the measured behaviour was worse than the paragraph
above predicts.** Renaming `audio_path` → `audio_key` against a database built
before the rename produced *no error anywhere*: `/api/health`, `/api/tracks`
and `/api/tracks/<id>` all returned 200, and the audio endpoint returned a 302
to `/blobs//old/abs/path.wav`. The cause is that generated row mapping is
**positional** — `_from_row` zips row tuples against `_FIELDS` by index — so a
renamed column silently hands the old column's value to the new field. The
`data-e2e/` directory is explicitly cached ("delete data-e2e/ to rebuild"), so
any developer with one from before the rename would have hit exactly this.

`Database.verify_schema()` now runs after `migrate()` and compares each live
table's columns against the model's declared fields, raising with the actual
and expected column lists and what to do about it. It migrates nothing; it
converts a silent wrong answer into an actionable startup error. A regression
test builds a pre-rename database and asserts the refusal.
**The failure splits by statement kind, not by engine.** An earlier draft of
this section claimed Postgres fails loudly and SQLite silently; that is wrong,
and measurement on one engine shows why:

| Statement kind | Behaviour against a stale table |
|---|---|
| Names the column (`UpsertTrack`) | Raises. `table tracks has no column named audio_key` |
| `SELECT *` (`GetTrack`) | **Returns silently wrong data.** `audio_key` → `/old/abs/path.wav` |

Both rows were observed on the *same* SQLite database in the same process, and
both hold on Postgres too: psycopg returns tuples positionally, so `_from_row`
mismaps identically there. The real signature is therefore **writes fail,
reads lie** — which is nastier than either half of the original claim, because
the loud write failure trains you to look at ingestion while the reads quietly
serve wrong values to clients.

Only one part of the original framing was genuinely SQLite-specific and it
still stands: if `DB_PATH` moves, `migrate()` cheerfully creates a *second*
database file with the new shape, so a stale catalog and a fresh one coexist
and both look healthy. When diagnosing "tracks I definitely ingested are
missing" on SQLite, check for more than one `.sqlite3` under `data/` before
anything else. Postgres has no analogue.

`verify_schema()` catches all of the above at startup on either engine, so
this is now a description of what was fixed rather than of live exposure.

Consequences for the sequenced plan:

- The `audio_path` → `audio_key` / `variants.path` → `object_key` rename (§9
  item 3) is safe to ship now only against a database one is willing to
  recreate. Against a populated Supabase it needs a hand-written
  `ALTER TABLE ... RENAME COLUMN`.
- The same applies to every column the §3.2 analysis-blob split adds.
- Before Phase 2 puts a real catalog in Supabase, either a migration runner
  (a `schema_migrations` table plus ordered files) must exist, or schema
  changes must be applied by hand and recorded somewhere durable.

Recreating the database is a legitimate answer at Phase 0/1 scale; it stops
being one the moment a 10,000-track import has been paid for.
