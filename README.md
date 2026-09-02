# DJ Mixer — prototype

Generates DJ mixes from Creative-Commons tracks: pre-rendered tempo-matched
variants, harmonic/energy-based next-track recommendations with score
breakdowns, and a tactile timeline where you drag track 2 onto scored
transition markers. Implements the companion docs (in `docs/`)
(`dj-app-project-plan.md`, `requirements.md`, `ui-requirements.md`,
`testing-document.md`).

## Requirements

- Python 3.9+ (developed on 3.13) — dependencies pinned in `requirements.txt`
- `ffmpeg` on PATH (Jamendo MP3 decode, `jamendo` mode only)
- Node 22+ — **tests only**; the app itself has no build step and ships no
  npm runtime dependencies
- Optional, auto-detected: `rubberband` CLI (higher-quality stretch),
  `essentia` Python package (production analysis)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

npm install                        # test tooling only
npx playwright install chromium    # browser suite only
```

There is still no bundler and no migrations. `npm` exists solely to run the
browser suite — nothing in `frontend/` imports a package.

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
BPM-grid variant rendering. First start takes ~5 s for the 9-track demo
catalog (progress prints per track); results are cached in `data/`, so
subsequent starts are instant. Delete `data/` to force re-ingestion.

For real catalogs use the parallel batch publisher instead of startup
ingestion — it is resumable, rate-limits the track source, and is the only
supported path at more than a few dozen tracks:

```bash
python3 -m backend.publish --dry-run     # plan + API cost, spends nothing
python3 -m backend.publish --workers 8
```

ND-licensed tracks are ingested for playback/metadata but get **no**
stretched variants and are excluded from all mixing features (this is the
CC-BY-ND compliance rule, not a bug).

### Using real Jamendo tracks

`config/tracks.json` ships in `"mode": "offline"` (deterministic synthesized
tracks — works with zero network, which is how this repo was developed and
tested). To ingest real Jamendo audio:

1. Get a (free) Jamendo API client id.
2. `export JAMENDO_API_CLIENT=your_id`
3. In `config/tracks.json`: set `"mode": "jamendo"` and put real Jamendo
   track ids in the `id` fields (keep a `genre` matching a bucket in
   `backend/config.py`; `bpm`/`key` fields are ignored in this mode — they
   are detected).
4. Restart with an empty `data/` dir.

Only tracks with `audiodownload_allowed=true` are accepted; each track's CC
license is read from the API and every variant (BY / -SA / -NC / -ND /
-NC-SA / -NC-ND) is stored and enforced.

#### Building a catalog from a community listing

Hand-curating ids does not scale past a playlist, and the Jamendo community
pages are client-rendered infinite scroll, so there is no list in the HTML to
copy. `make discover` asks the API for the same listing instead:

```bash
make discover COUNT=200 TAG=electronic OUT=config/tracks.electronic.json
make discover COUNT=1000 OUT=config/tracks.all.json          # every genre
DJMIXER_TRACKS=config/tracks.electronic.json make ingest
```

`COUNT` is how many *ingestible* tracks to collect, not how many rows to read.
ND-licensed and non-downloadable tracks are filtered out during discovery, so
200 means 200 tracks that will actually render variants — worth knowing,
because roughly half of a typical Jamendo listing is ND and would be refused
at the licence gate after being counted.

Discovered entries carry `"genre": "auto"`. The field is a *tempo band* that
selects the BPM grid, the API exposes no usable tempo, and
`bpm_grid.resolve_bucket` derives it from the analysed BPM at ingest time.
Do not substitute a fixed band: `grid_points` answers an out-of-band request
with an empty list rather than an error, so every track outside that one band
would be downloaded, analysed, stored, and left with no variants at all.

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
6. Mixes save themselves. The picker in the top bar lists saved mixes
   (most recently edited first); `+ New mix` returns to the zero state.
   Selecting a mix with tracks resumes it — the deck shows what to play next,
   ranked against the last track, rather than the browse view.

## Tests

```bash
./run_tests.sh          # all three suites
./run_tests.sh --fast   # skip the browser suite (no server boot)
```

or individually:

```bash
cd tests/backend && ../../.venv/bin/python -m unittest discover -s .  # 98, ~7 s
node --test tests/frontend/*.test.mjs                                 # 50, <1 s
npm run test:e2e                                                      # 18, ~23 s
```

Test names mirror the testing-document ids (P1-01…P4-29). Backend tests build
a real 5-track fixture catalog (one full ingestion, shared across modules).
Frontend tests run the pure interaction-logic modules under `node:test` — see
`docs/design-document.md` §8 for the seam. The Playwright suite drives a real
Chromium against its own Flask server on port **5199** with its own catalog in
`data-e2e/`, covering what the other suites structurally cannot: native
drag-and-drop, canvas rendering, the WebAudio clock, and network silence
during drag.

**`docs/automation-test-manifest.md` is the coverage map** — every
testing-document id, the suite and test that proves it, the remaining gap, and
the one check that still needs a human with ears. It also records the SQLite
concurrency defect the browser suite found (API-01) and how it was fixed.

### Continuous integration

`.github/workflows/ci.yml` runs every suite on push and pull request, in three
jobs:

| Job | What it runs |
|---|---|
| **Backend** | full suite on SQLite, the DB layer against a real PostgreSQL service, then the service smoke test |
| **Frontend** | the `node:test` logic modules |
| **Browser** | the Playwright suite against a server it starts itself |

**No secrets, no network.** `config/tracks.json` ships in `jamendo` mode and
would need a metered API key to boot, so every job points `DJMIXER_TRACKS` at a
synthetic catalog (`mode: offline`) instead. That drives the same
ingest → analyse → render → serve path deterministically and for free.

CI also sets `DJMIXER_REQUIRE_ESSENTIA=1` and `DJMIXER_REQUIRE_RUBBERBAND=1`,
and asserts both engines are live before running anything. A fallback is a
*different* engine, not a slower one — without that gate a runner missing
`rubberband` would quietly test the phase vocoder and still go green.

The **service smoke test** (`tests/backend/test_p7_service.py`) is the one that
boots the real application the way a deployment does — schema creation,
ingestion, warmup, then the endpoints a browser calls — against PostgreSQL
rather than SQLite. Nothing else covers that combination. Run it locally with:

```bash
make test-smoke          # brings up local Postgres, uses its own database
```

The browser job is currently **non-blocking** (`continue-on-error`). It runs in
full and uploads its report, but four `delete-track` specs fail on main for a
reason that predates the workflow — `buildChain()` adds a third track where it
overlaps the first, and `check_overlaps` correctly returns 409. That line
should come out as soon as those specs pass.

## Database

Locally the backend uses a SQLite file under `data/`; no setup needed. All
persistence goes through `backend/db/`, where the schema and every query live
in `.sql` files and the typed Python bindings are generated from them. After
editing anything under `backend/db/sql/`, regenerate and commit the result:

```bash
.venv/bin/python -m backend.db.codegen
```

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
  deck.py           zero-state deck (genres x N, popularity-aware)
  mixes.py          saved mixes: chain walk, ripple edits, overlap invariant
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
