# DJ Mixer — prototype

Generates DJ mixes from Creative-Commons tracks: pre-rendered tempo-matched
variants, harmonic/energy-based next-track recommendations with score
breakdowns, and a tactile timeline where you drag track 2 onto scored
transition markers. Implements the companion docs (in `docs/`)
(`dj-app-project-plan.md`, `requirements.md`, `ui-requirements.md`,
`testing-document.md`).

## Requirements

- Python 3.9–3.13 (developed on 3.13) — dependencies pinned in `requirements.txt`
- Node 22+ — **tests only**; the app itself has no build step and ships no
  npm runtime dependencies
- Two binaries on PATH: **ffmpeg** (decodes Jamendo MP3s) and **rubberband**
  (production time-stretch)

## Setup

```bash
brew install ffmpeg rubberband     # macOS; apt: ffmpeg rubberband-cli
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

npm install                        # test tooling only
npx playwright install chromium    # browser suite only
```

There is still no bundler and no migrations. `npm` exists solely to run the
browser suite — nothing in `frontend/` imports a package.

### Dependency seams

Three real dependencies do the heavy lifting, each behind a seam with a
numpy/scipy stand-in so the pipeline still runs on a bare install:

| Job | Production engine | Fallback if absent |
|---|---|---|
| BPM, beat grid, key, frame features | **Essentia** | numpy/scipy DSP |
| BPM-grid variant rendering | **Rubber Band** (R3 engine) | scipy phase vocoder |
| Track source + licensing | **Jamendo API** | synthesized `offline` mode |

A fallback is a *different engine*, not a slower one, so it must never engage
unnoticed in production. `GET /api/credits` and every stored analysis record
which engine actually ran (`analysis.engine`), and two env vars turn a silent
downgrade into a startup failure:

```bash
export DJMIXER_REQUIRE_ESSENTIA=1
export DJMIXER_REQUIRE_RUBBERBAND=1
```

### Jamendo credentials

Put a (free) Jamendo client id in a **`.env`** at the repo root — it is
gitignored and read automatically at startup, so no shell setup is needed:

```
JAMENDO_CLIENT_ID=your_client_id
```

`JAMENDO_API_CLIENT` is accepted too, since that is what Jamendo's own
dashboard calls the field. Real environment variables always win over `.env`.

## Run locally

```bash
.venv/bin/python -m backend.app
# → http://127.0.0.1:5050
```

**Ingestion runs on startup, in the background.** The server binds its port
immediately and the page shows a progress overlay until the catalog is ready
(`/api/status` reports phase and progress). Waveform envelopes are precomputed
during warmup and inlined into the deck payload, so the opening page load makes
no per-track requests at all.

Ingestion pulls from the track list in `config/tracks.json`:
fetch → license gate → analysis (BPM/key/beat grid/segments/prefix sums) →
BPM-grid variant rendering. First start takes **~4–5 min** for the 12-track
demo catalog of real Jamendo audio (progress prints per track; roughly 7 s
fetch + 2 s analysis + 17 s variant rendering each). Results are cached in
`data/`, so subsequent starts are instant. Delete `data/` to force
re-ingestion.

ND-licensed tracks are ingested for playback/metadata but get **no**
stretched variants and are excluded from all mixing features (this is the
CC-BY-ND compliance rule, not a bug).

### Ingestion is resumable — nothing is downloaded twice

Each track carries a `status` high-water mark:

```
pending → fetched → analyzed → ready
```

Every stage is skipped when its output is already durably on disk, so a
restart only does the work that is actually missing:

- a track at `ready` is skipped entirely — **no request is made for it**
- a crash after download re-analyzes from the persisted master, without
  re-fetching the audio
- a deleted variant file is re-rendered on its own; intact ones are not
- a missing master *is* re-fetched — the disk wins over the recorded state

Failures never rewind that mark. A track that dies during analysis stays at
`fetched` with the reason in `status_error`, so retrying resumes instead of
restarting, and one bad track does not abort the rest of the catalog.

`GET /api/ingest` reports the whole picture — per-track status, stage
timestamps, variant counts, and any error. `/api/tracks` only lists tracks at
`ready`, so a half-ingested track is never advertised to the UI.

To rebuild from scratch, delete `data/`; to redo one stage, clear that
track's status (or call `ingest_track(..., force=True)`).

### The catalog

`config/tracks.json` ships in `"mode": "jamendo"` with **72 tracks from the
Jamendo ["Fresh & New" playlist](https://www.jamendo.com/playlist/500608490/fresh-and-new)**.
Each track's CC licence — **including its version**, mostly 3.0 rather than
4.0 — is read from the API and enforced.

Of the playlist's 84 entries, 83 resolve through the API and **11 are excluded
because Jamendo reports `audiodownload_allowed=false`**: the P1-01 gate
refuses them before any download, so they cannot be ingested at all.

> ### ⚠️ Most of this catalog cannot be mixed
>
> **64 of the 72 tracks are ND-licensed** (58 BY-NC-ND, 3 BY-ND, plus others).
> ND forbids derivative works, and a time-stretched variant *is* a derivative,
> so those tracks get no variants and are excluded from every mixing feature.
> That is the compliance rule in `docs/requirements.md` §2 working correctly —
> not a bug — but it means the catalog supports:
>
> - **8 mixable tracks**, forming **5 mixable pairs**
> - 64 tracks that are browsable and playable at native tempo only
>
> The playlist is curated for freshness, not for permissive licensing. If you
> want a catalog that exercises the mixer properly, filter a Jamendo search by
> licence instead (`ccnd=false`), which is how the previous 12-track catalog
> was built — see git history for that version.

Tempo bands (`BPM_BUCKETS` in `backend/config.py`) define which tracks can
meet at a shared BPM. Because a "fresh & new" pop playlist spans tempos no
genre label predicts, the bands are named for tempo rather than genre, and
each track's band is assigned from its *detected* BPM:

| Band | BPM | Tracks |
|---|---|---|
| `slow` | 70–84 | 1 |
| `downtempo` | 85–95 | 7 |
| `midtempo` | 96–119 | 24 |
| `house` | 120–128 | 14 |
| `uptempo` | 129–152 | 21 |
| `fast` | 153–182 | 5 |

The `bpm`/`key` fields are the values Essentia measured at curation time. In
`jamendo` mode they are informational (everything is re-detected); switching
`"mode"` to `"offline"` uses them to synthesize a stand-in catalog of the same
shape that needs no network or credentials.

To swap in your own tracks, replace the `id`/`genre` fields and restart with
an empty `data/`. A track whose detected BPM reaches no grid point in its band
can never be mixed; `tests/backend/test_p5_dependencies.py` asserts the
shipped catalog still contains at least one genuinely mixable pair.

> **Note on the Jamendo API terms.** The API terms restrict applications
> "specifically designed to cache the content nor offering an offline access
> to the content", allowing caching "only to the extent reasonably necessary
> for the operation of the Application". This prototype permanently stores
> downloaded masters *and* rendered variants under `data/`, which the
> pre-rendered-variant design depends on. That is a separate question from the
> per-track CC licence (see `docs/requirements.md` §2) — the CC licence may
> permit a copy that the API terms restrict. Flagged, not resolved.

## Using the app

0. The opening deck browses by genre (top 5 each) — nothing is ranked yet,
   because with no track selected there is nothing to match against.
1. Drag any track from the deck onto the timeline — it becomes Track 1
   (magenta) and the deck switches to ranked next-track suggestions
   (score % + pie, with BPM/key/energy breakdown on hover).
2. Drag a suggestion onto the timeline — it becomes Track 2 (blue), snaps to
   the highest-scoring gold transition marker, and both tracks switch to the
   shared BPM-grid variants.
3. Drag Track 2 left/right to adjust: magnetic pull engages near markers and
   beats; free placement everywhere else. The overlap's waveform taper *is*
   the crossfade gain that plays.
4. Nav bar: drag the rectangle to pan, drag its edges to zoom, click outside
   it to jump. Click the timeline to seek; ▶ plays the mix with an
   equal-power crossfade.
5. Footer: per-track CC attribution links + open-source credits.

## Tests

```bash
./run_tests.sh          # all three suites
./run_tests.sh --fast   # skip the browser suite (no server boot)
```

or individually:

```bash
cd tests/backend && ../../.venv/bin/python -m unittest discover -s .  # 62, ~7 s
node --test tests/frontend/*.test.mjs                                 # 50, <1 s
npm run test:e2e                                                      # 18, ~23 s
```

Test names mirror the testing-document ids (P1-01…P4-29). Backend tests build
a real 5-track fixture catalog (one full ingestion, shared across modules) —
synthesized, not fetched, so the suite stays hermetic and has known BPM/key
ground truth to assert Essentia against. Frontend tests run the pure
interaction-logic modules under `node:test` — see `docs/design-document.md`
§8 for the seam. The Playwright suite drives a real Chromium against its own
Flask server on port **5199** with its own catalog in `data-e2e/`, covering
what the other suites structurally cannot: native drag-and-drop, canvas
rendering, the WebAudio clock, and network silence during drag.

`test_p5_dependencies.py` covers the three provider seams: that Essentia is
the engine *actually* running rather than merely the one being reported, that
Rubber Band renders to the requested duration on the R3 engine, and that CC
licence versions survive the round trip. `test_p6_resumable.py` covers the
ingestion state machine and crash recovery — including that a failed attempt
never causes already-downloaded audio to be fetched again. Engine-specific
cases skip when the dependency is absent.

Three further tests hit the live Jamendo API and are opt-in, since the network
is involved:

```bash
DJMIXER_LIVE_TESTS=1 ./run_tests.sh --fast
# verifies every catalog track is still fetchable and still carries the
# licence recorded in config/tracks.json
```

**`docs/automation-test-manifest.md` is the coverage map** — every
testing-document id, the suite and test that proves it, the remaining gap, and
the one check that still needs a human with ears. It also records the SQLite
concurrency defect the browser suite found (API-01) and how it was fixed.

## Database

Locally the backend uses a SQLite file under `data/`; no setup needed. All
persistence goes through `backend/db/`, where the schema and every query live
in `.sql` files and the typed Python bindings are generated from them. After
editing anything under `backend/db/sql/`, regenerate and commit the result:

```bash
.venv/bin/python -m backend.db.codegen
```

`migrate()` is additive: a column added to `schema.sql` is applied to an
existing catalog on the next start, so upgrading does not mean re-ingesting.

Deploying against Supabase is a connection string, not a code change:

```bash
.venv/bin/pip install -r requirements-postgres.txt
export DJMIXER_DATABASE_URL='postgresql://…@…pooler.supabase.com:6543/postgres'
```

See **`docs/database.md`** for the query annotation format, the transaction and
concurrency model, and what the Postgres path does and does not have test
coverage for.

## Latency & scaling

`docs/latency-report.md` contains real measured per-stage latencies from
this machine plus projections to 500 / 10k tracks and the infrastructure
table. Regenerate after pipeline changes:

```bash
python3 -m backend.benchmark
```

## Repo layout

```
backend/            Flask API + pipeline
  app.py            endpoints, readiness gating, static serving
  dbpool.py         bounded connection pool + admission semaphore
  warmup.py         background ingestion, waveform precompute, readiness
  waveforms.py      envelope computation + startup cache
  catalog.py        zero-state deck (genres x N, popularity-aware)
  ingest.py         fetch → gate → analyze → segment → variants
  jamendo.py        track source (jamendo | offline provider seam)
  synth.py          deterministic fixture-track synthesizer
  analysis.py       BPM / beat grid / key / frames / prefix sums
  segmentation.py   novelty segmentation + section roles
  stretch.py        time-stretch (rubberband | phase-vocoder seam)
  bpm_grid.py       genre buckets, grid planning, stretch cap
  matching.py       Camelot table, match score, recommendations
  transitions.py    windowed transition scoring (prefix-sum backed)
  licensing.py      CC parsing, ND/SA/NC flags, attribution
  db/               the only code that touches persistence — see docs/database.md
    sql/            canonical schema + annotated queries (source of truth)
    codegen.py      sqlc-style generator -> models.py + queries.py
    dialect.py      canonical types + placeholders -> SQLite / PostgreSQL
    engine.py       connections, transactions, concurrency control
    catalog.py      Database / Catalog interface
  timing.py / benchmark.py / config.py
frontend/           no-build vanilla ES modules
  js/{state,align,crossfade,navbar,deck,attribution,boot}.js  pure logic (tested)
  js/{app,timeline,audio,api}.js                          DOM / canvas / WebAudio
tests/backend/      unittest — P1/P2/P3 + backend P4 + DB layer P5 (76)
tests/frontend/     node:test — UI interaction logic P4 (42)
tests/e2e/          Playwright — browser-only behaviour (18)
config/tracks.json  catalog + source mode
requirements.txt    backend dependencies
requirements-postgres.txt   deployment extras (Supabase/PostgreSQL)
package.json        test tooling only (Playwright)
playwright.config.mjs
docs/               design-document.md, automation-test-manifest.md,
                    database.md, latency-report.md
data/               generated at runtime (db + audio) — safe to delete
data-e2e/           browser-suite catalog — safe to delete
```
