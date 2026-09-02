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

**Ingestion runs on startup** from the track list in `config/tracks.json`:
fetch → license gate → analysis (BPM/key/beat grid/segments/prefix sums) →
BPM-grid variant rendering. First start takes ~45 s for the 9-track demo
catalog (progress prints per track); results are cached in `data/`, so
subsequent starts are instant. Delete `data/` to force re-ingestion.

ND-licensed tracks are ingested for playback/metadata but get **no**
stretched variants and are excluded from all mixing features (this is the
CC-BY-ND compliance rule, not a bug).

### Using real Jamendo tracks

`config/tracks.json` ships in `"mode": "offline"` (deterministic synthesized
tracks — works with zero network, which is how this repo was developed and
tested). To ingest real Jamendo audio:

1. Get a (free) Jamendo API client id.
2. `export JAMENDO_CLIENT_ID=your_id`
3. In `config/tracks.json`: set `"mode": "jamendo"` and put real Jamendo
   track ids in the `id` fields (keep a `genre` matching a bucket in
   `backend/config.py`; `bpm`/`key` fields are ignored in this mode — they
   are detected).
4. Restart with an empty `data/` dir.

Only tracks with `audiodownload_allowed=true` are accepted; each track's CC
license is read from the API and every variant (BY / -SA / -NC / -ND /
-NC-SA / -NC-ND) is stored and enforced.

## Using the app

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
cd tests/backend && ../../.venv/bin/python -m unittest discover -s .  # 40, ~6 s
node --test tests/frontend/*.test.mjs                                 # 42, <1 s
npm run test:e2e                                                      # 12, ~20 s
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
testing-document id, the suite and test that proves it, the two known gaps
(including an open SQLite concurrency defect the browser suite found), and the
one check that still needs a human with ears.

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
  app.py            endpoints, startup ingestion, static serving
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
  db.py / timing.py / benchmark.py / config.py
frontend/           no-build vanilla ES modules
  js/{state,align,crossfade,navbar,deck,attribution}.js   pure logic (tested)
  js/{app,timeline,audio,api}.js                          DOM / canvas / WebAudio
tests/backend/      unittest — P1/P2/P3 + backend P4 (40)
tests/frontend/     node:test — UI interaction logic P4 (42)
tests/e2e/          Playwright — browser-only behaviour (12)
config/tracks.json  catalog + source mode
requirements.txt    backend dependencies
package.json        test tooling only (Playwright)
playwright.config.mjs
docs/               design-document.md, automation-test-manifest.md,
                    latency-report.md
data/               generated at runtime (db + audio) — safe to delete
data-e2e/           browser-suite catalog — safe to delete
```
