# Design document — decisions outside the provided docs

This repo implements `dj-app-project-plan.md`, `requirements.md`,
`ui-requirements.md`, and `testing-document.md`. Those docs settle the big
architecture (pre-rendered BPM-grid variants, Jamendo + CC compliance, match
formula, marker UX). This document records only the decisions those docs
**didn't** make — mostly forced by the development environment, plus a few
algorithm and UI calls — and why each was made.

---

## 1. Provider seams (originally: the environment had no network)

This design was written when the dev/test server had **no outbound network**,
which ruled out at build time: the live Jamendo API, installing Essentia
(AGPL C++ build), installing Rubber Band, npm packages, CDN assets, and
webfonts. Rather than stub the system into fiction, every third-party
dependency became a **provider seam** with a real fallback implementation
honoring the same contract.

**All three seams now run on the real dependency.** The fallbacks remain, so
the pipeline still works on a bare install, but they are no longer the
default path:

| Dependency | Seam | Production engine | Fallback if absent |
|---|---|---|---|
| Jamendo API | `backend/jamendo.py fetch_track(entry, mode)` | live API v3.0, credentials from `.env` | `offline` mode: deterministic synthesized tracks |
| Essentia | `backend/analysis.py` | `RhythmExtractor2013` (BPM/beats), `KeyExtractor` (edma), `Spectrum`/`RMS`/`Flux` (frames) | numpy/scipy DSP, same output contract |
| Rubber Band | `backend/stretch.py` | `rubberband --fine` (R3 engine) CLI | STFT phase vocoder (scipy) |

Because a fallback is a *different engine* rather than a slower one, a silent
downgrade in production would quietly change every detected BPM and key.
Three things guard that: `analysis.engine` is recorded on every stored
analysis (so the engine that actually ran is auditable after the fact),
`engine_name()` reports it live, and `DJMIXER_REQUIRE_ESSENTIA` /
`DJMIXER_REQUIRE_RUBBERBAND` turn a missing dependency into a startup
failure. The previous code reported `engine: "essentia"` whenever the package
merely *imported*, while numpy/scipy did all the work — the failure mode this
now prevents.

One integration detail worth recording, because it is invisible and
consequential: Essentia's rhythm extractors assume **44.1 kHz** and several
(including `RhythmExtractor2013`) expose no `sampleRate` parameter at all.
Feeding them the pipeline's 22.05 kHz masters directly makes them read the
audio as half-length and double-tempo, which put every detected BPM an octave
out and truncated the beat grid to the first half of each track.
`analysis.py` therefore resamples to `ANALYSIS_SR` before analysis, while
storage and variants stay at `config.SAMPLE_RATE`.

Two properties were non-negotiable for the fallbacks:

1. **The compliance and pipeline logic is identical in both modes.** The
   `audiodownload_allowed` gate (`validate_source_meta`, test P1-01), license
   parsing, ND exclusion, variant planning, prefix sums, matching, and
   transition scoring don't branch on provider. Only *where audio and
   metadata come from* changes.
2. **The fallback is real signal processing, not canned answers.** The
   analysis pass genuinely detects BPM/key/beats from audio; tests assert
   detection against the synthesizer's ground truth (P1-02), which caught
   several real DSP bugs during development.

## 2. Synthetic fixture tracks (offline provider)

`backend/synth.py` generates deterministic (seeded per track id) audio with
DJ-typical structure: intro / build / drop / breakdown / drop2 / outro energy
envelope, four-on-the-floor kick, offbeat hats, root/fifth bassline, i–i–iv–v
chord pads, and a tonic+third drone. Design intent:

- **Structure exists so segmentation and transition scoring have something
  true to find.** The outro really is low-energy and late; tests can assert
  "best exit is late in A" (P3-04) against reality, not mocks.
- **The kick is pitched to the tonic** and the drone includes the scale's
  third. Early versions used a fixed 55 Hz kick — every track leaked pitch
  class A into its chroma and key detection only worked for A-rooted keys.
  A fixture that fights the detector tests nothing.
- Fixture key/BPM combos in `config/tracks.json` were verified clean
  end-to-end (9/9 detect correctly). Two original combos sat on genuine
  ambiguity edges of the fallback detector (relative-major confusion) and
  were swapped for verified adjacent-key combos — fixture data is arbitrary;
  what matters is that the catalog exercises the license mix (BY, BY-SA,
  BY-NC, BY-ND, BY-NC-SA, BY-NC-ND) and harmonic adjacency structure.

## 3. Fallback analysis DSP (what "the Essentia contract" means here)

- **BPM**: spectral-flux onset strength → autocorrelation → argmax over
  70–180 BPM, then **octave disambiguation**: repeatedly double the lag while
  the doubled lag keeps ≥50% of the AC peak (kick+hat patterns otherwise lock
  onto the half-beat), then compare comb scores across candidate multiples
  {½, ⅔, 1, 1.5, 2, 3} weighted by a mild log-Gaussian prior centered at
  115 BPM (catches ⅔-period polyrhythm locks). Parabolic interpolation for
  sub-lag precision. Verified within ±0.5 BPM on all fixtures.
- **Beat grid**: comb-phase search — the offset maximizing onset energy under
  a beat-period comb; grid = arithmetic sequence from that phase.
- **Key**: STFT magnitudes folded to a 12-bin chroma (80–2500 Hz),
  **log-compressed** (`log1p`) so pattern beats loudness, correlated against
  Krumhansl–Schmuckler major/minor profiles over all 24 rotations → Camelot.
- **Frames + prefix sums**: per-hop RMS, spectral flux, bass-band ratio
  (20–150 Hz / total) with cumulative-sum arrays; `window_mean` is two array
  reads regardless of window size (asserted structurally in P1-04).

Essentia now replaces these detectors by default (`RhythmExtractor2013`
multifeature for BPM and the beat grid, `KeyExtractor` with the `edma`
profile for key, `Spectrum`/`RMS`/`Flux` for frames). The frame/prefix layout
and every downstream consumer are unchanged — that was the point of pinning
the contract first. Two differences are worth knowing:

- The beat grid is now **real tracked beat times**, not an arithmetic
  sequence from a single phase estimate, so spacing varies slightly across a
  track. `transitions._window_starts` snaps candidate starts to the nearest
  frame rather than truncating, which the uniform grid had masked.
- `edma` is chosen over `temperley`/`krumhansl` because it was trained on
  electronic dance music, which is what this catalog is. On the fixture
  ground truth `edma`, `krumhansl` and `bgate` all score 9/9 while
  `temperley` scores 7/9.

## 4. Time-stretch

Rubber Band's R3 engine (`--fine`) is the production path, invoked per
variant at ingestion. The CLI still defaults to the older R2 engine for
backward compatibility, so `--fine` is passed explicitly: variants are
pre-rendered once and never touched in the playback path, which is exactly
the case where buying quality with CPU is free.

The fallback is a classic STFT phase vocoder (2048/512, Hann): magnitude
interpolation at resampled frame positions, phase accumulated by per-bin
instantaneous frequency. Adequate for a prototype and fully offline. The
stretch cap (±10%) and the grid-variant plan come from the project plan and
are enforced upstream of this module either way.

## 5. Audio format: WAV, 22.05 kHz, mono

Prototype-only call, three reasons: Python's stdlib `wave` writes it with no
extra dependency; browsers decode it natively via `decodeAudioData`; analysis
at 22.05 kHz halves STFT cost with no loss for the features used (nothing
above ~11 kHz matters to BPM/key/energy). Production plan (unchanged from the
project plan): 44.1 kHz masters, compressed delivery (Opus/AAC via ffmpeg,
which is already used for Jamendo MP3 decode in `jamendo` mode), R2 + CDN.
The latency report's storage projections use compressed sizes.

## 6. Persistence: a generated DB layer over SQLite (Postgres-ready)

Single-file SQLite with JSON blobs for analysis/segments. Chosen because the
prototype's DB work is trivial (a few hundred rows, read-heavy) and it keeps
`python -m backend.app` the only startup step. The schema is deliberately
boring (tracks / variants / latency).

Everything that touches persistence goes through `backend/db/`; nothing outside
it sees a connection, a cursor or a SQL string.

**SQL lives in `.sql` files.** `db/sql/schema.sql` is the single source of
truth and `db/sql/queries/*.sql` carry sqlc-style annotations
(`-- name: GetTrack :one`). `db/codegen.py` parses both and generates
`models.py` (row dataclasses) and `queries.py` (one typed method per
statement); the generated files are committed and a test fails if they drift.
sqlc itself targets Go and its Python plugin needs a Go toolchain plus a
protobuf plugin, so this reimplements the useful part rather than adopting the
tool. The payoff is that a renamed column breaks code generation instead of
silently returning `None`, and a misspelled parameter is caught against the
schema instead of binding NULL.

**Concurrency.** The original arrangement — one connection with
`check_same_thread=False` shared across Flask's worker threads, with a lock
around only the writes — was not sufficient, and the browser suite caught it
(API-01 in `docs/automation-test-manifest.md`): concurrent reads on the shared
connection interleaved and returned 500s and phantom 404s. Connections are now
**per thread**, held only for the life of a request, in WAL mode so readers
never block the writer, with `foreign_keys=ON`, a `busy_timeout` for
out-of-process writers, and a process-wide lock serialising writes. Read and
write scopes are explicit and nest, so ingestion commits a track and its
rendered variants as one transaction instead of a commit per statement.

**Admission control.** Per-thread connections fix the correctness half of
API-01, but they do not bound *how much* concurrent work reaches the database:
`SQLiteEngine` mints a connection per thread on demand, and nothing caps the
threads. `backend/dbguard.py` wraps the `Database` with a semaphore admitting at
most `DB_MAX_CONCURRENCY` callers, kept strictly below the engine's connection
ceiling (`psycopg_pool`'s `max_size` on Postgres; on SQLite there is no ceiling,
so admission *is* the bound). Blocking at admission rather than inside a
connection checkout makes the wait bounded, observable via `/api/status`, and
answerable with a clean 503. The gate is re-entrant per thread, because read and
write scopes nest and an inner scope must not re-take a permit its own thread
already holds.

**Postgres/Supabase is a URL, not a rewrite.** `db/dialect.py` maps the
canonical column types to each engine's DDL and rewrites `:named` placeholders
into psycopg's `%(name)s`; query bodies stay neutral (`ON CONFLICT` works on
both), so there is one set of `.sql` files rather than one per engine. Setting
`DJMIXER_DATABASE_URL` switches engines. See `docs/database.md`.


**Correction.** This section previously claimed that "`check_same_thread=False`
+ a write lock handles Flask's threaded server". That was wrong, and the
browser suite proved it: sharing one connection across worker threads while
locking only writes let concurrent *reads* interleave, so 15-20% of them either
raised `sqlite3.InterfaceError` (HTTP 500) or returned a phantom-empty row that
the API reported as a 404 for a track that exists. A write lock is not
sufficient — reads need the same discipline.

`backend/dbpool.py` replaces the shared connection with a bounded pool:

- **No connection is used by two threads at once.** Each caller checks one out
  for the duration of its work. This is what removes the race.
- **In-flight DB work is capped strictly below the pool size.** A semaphore
  admits at most `DB_MAX_CONCURRENCY` callers against `DB_POOL_SIZE`
  connections. The headroom makes admission the queueing point — bounded,
  observable via `/api/status`, and answerable with a clean 503 — instead of
  letting a worker stall inside a checkout. Set the limit below whatever the
  storage engine allows concurrently; on Postgres that is `max_connections`
  minus whatever the ingest workers hold.
- WAL + `busy_timeout` so readers do not block each other.

This is a storage-layer contract, not a SQLite detail: the same bound is what a
Postgres pool needs, which is why it lives in its own module rather than inside
`db.py`.

## 7. API server: Flask

Flask over FastAPI because it's what the environment has installed, and the
API is small, synchronous, and serves files — async buys nothing here. The
app factory (`create_app(run_ingestion=...)`) exists so tests can mount the
API on a fixture DB without triggering startup ingestion.

Two API semantics worth recording (not specified in the docs):

- `GET /api/transitions?a&b` returns **403** when either track is
  ND-licensed (mixing is a derivative use — refusing at the API makes the
  compliance rule non-bypassable by any client) and **409** when the pair
  shares no BPM grid point (a valid request whose answer is "these two can't
  meet"; distinct from 4xx client error so the UI can message it precisely).
- The transitions response carries `match` (the Phase-2 score breakdown) so
  the UI never recomputes matching client-side.

## 8. Frontend: no-build vanilla ES modules

No npm is possible offline, and nothing in the UI needs a framework: the
page is two canvases, a list, and a dialog. Native ES modules give the test
seam instead — every piece of interaction *logic* lives in pure modules
(`state.js`, `align.js`, `crossfade.js`, `navbar.js`, `deck.js`,
`attribution.js`) with no DOM/WebAudio imports, so Node 22's built-in
`node:test` runs them directly (42 tests, no test framework installed).
DOM/canvas/audio code (`app.js`, `timeline.js`, `audio.js`) stays thin and
delegates every decision to the pure layer.

**The single-source-of-truth crossfade rule (P4-25):** `crossfade.js` is the
only place gain math exists. `audio.js` schedules
`gainCurve(...)` onto `GainNode`s (`setValueCurveAtTime`);
`timeline.js` multiplies waveform bar heights by `trackGainAt(...)` from the
same module. The rendered fade can't drift from the audible fade because
there is exactly one fade.

**Magnetic pull tuning** (the docs specify the behavior, not constants):
marker radius 1.25 s with quadratic ease (full snap at center, zero at the
edge), beat radius 0.18 s, markers win over beats. Chosen so a marker is
easy to hit from ~a bar away but a deliberate placement 1.5 s off stays put
(P4-24 asserts both properties).

## 8a. Startup precompute, readiness, and the zero state

Three related decisions, all made after the prototype's first live run showed
the opening page doing avoidable work.

**Waveforms are computed once at startup, never per page load.** A track's
envelope derives entirely from its cached analysis, which never changes after
ingestion — so recomputing it (and re-reading the multi-megabyte
`analysis_json` blob) on every page load was pure waste. `backend/warmup.py`
builds every envelope the UI can ask for before the server reports ready, and
`/api/deck` and `/api/tracks/<id>/recommendations` inline them into their
payloads. The opening load went from nine parallel per-row requests to zero,
which also removed the burst that triggered the §6 read race.

**The server binds its port before the catalog exists.** Ingestion now runs on
a background thread and `/api/status` reports phase, progress and elapsed time.
Catalog endpoints answer 503 + `Retry-After` with that same payload while
warming, and the client renders it as a progress overlay rather than a
half-built page. A failed warmup answers 500, not 503: it is not "try again
shortly", it needs attention. `/api/health` and `/api/status` are never gated,
or nothing could poll them.

**The zero state browses; it does not rank.** With no track selected there is
nothing to match against, so scoring the opening deck would be theatre. It
shows a few tracks per genre (`DECK_TRACKS_PER_GENRE`) and runs no pair
analysis at all — that begins when track 1 is chosen, and goes through the same
bounded pool as everything else. Ordering prefers a stored `popularity` value
and falls back to a per-genre deterministic shuffle, so the view is stable
across restarts without being alphabetical. Nothing stores popularity yet (see
the manifest's "Not yet covered"); the code path is live and dormant.

## 8b. Saved mixes

**What is stored is ordering plus one gap per track — nothing else.** The audio
is already rendered and the analysis already cached, so a mix is fully described
by which tracks, in what order, with what spacing. That is why persisting on a
drag is affordable at all.

**`mix_tracks` is a linked list.** `next_id` means an insert or delete in the
middle of a 100-track mix repoints one row instead of renumbering every row
after it. The cost, stated plainly: ordering cannot be expressed in SQL, so a
mix is read whole and walked in Python, and the walk has to guard against cycles
and orphans (it does — `MIX-01`). At 100 nodes a `position` column with
renumbering would also have been viable; the linked list wins on write
amplification, not on read simplicity.

**`delta_beats` is an integer, not seconds.** It is the gap from the *previous*
track's start, in whole beats at that node's `grid_bpm`. Relative, so a ripple
edit rewrites one row and the rest of the chain moves with it. Integral, so an
off-grid placement is not representable — beat alignment becomes a property of
the schema rather than a rule the UI is trusted to apply. Seconds are derived:
`delta_beats * 60 / grid_bpm`.

**At most two tracks may overlap**, checked on every write path
(`check_overlaps`). A track reaching its second-nearest neighbour would put
three on the grid at once, which both the crossfade model and the playback
engine assume cannot happen. The browser clamps the drag to the same bound so
the gesture stops rather than producing a state the API would reject with a 409.

**A drag persists, but coalesces.** The write is one row and one column, so the
cost is real but tiny. What is not tiny is issuing it on every `pointermove` —
those fire around 60 times a second for a gesture whose entire outcome is a
single integer. The client debounces during the drag and flushes on release, so
a gesture costs one or two writes rather than a hundred.

## 9. Visual design

The mockup PDF is a colorless sketch except for the mandated track colors
(track 1 magenta `#ff4fa3`, track 2 blue `#4fa8ff`). Around that mandate:
dark slate-indigo "hardware chassis" palette, monospace system-stack labels
(screen-printed-hardware register; also the only way to get a characterful
face with zero webfonts), and **gold reserved exclusively for transition
markers** — the marker lane is the product's one insight, so it gets the one
accent color. Marker arrows scale 10–26 px linearly with score (P4-20).

## 10. Transition scoring refinements (within Phase 3's design)

Two calls the plan left open:

- **Window starts snap to downbeats**, not just beats. In 4/4 dance music
  transitions start on the 1; it also removes a scoring artifact where a
  region whose first beat wasn't a downbeat had *every* candidate penalized
  by the phase term.
- **Windows may overrun the track end** (aggregates clamp to available
  frames): the crossfade finishing exactly as track A ends is the classic DJ
  exit, and requiring full in-bounds windows had crowded out precisely those
  late-outro candidates.
- **Position is blended into the role term** (0.6·role + 0.4·position, where
  position favors late-in-A exits / early-in-B entries): segment labels are
  coarse on 60 s fixtures, and the blend expresses the same musical prior
  continuously. Weights (0.35 energy / 0.30 phase / 0.20 spectral / 0.15
  role) are per the testing doc.

## 11. What is deliberately not implemented

- **Title persistence across reload (P4-05, second half).** The title is
  inline-editable and held in mix state; there is no save/load of mixes at
  all in v1 (no backend mix entity in the plan). The reload-persistence
  clause of P4-05 therefore fails by design until a mix-save feature exists;
  flagged here rather than half-shipped as localStorage.
- **Waveform library.** wavesurfer.js (and Tone.js) remain in the credits
  page per P4-29 as the production plan, but the prototype renders waveforms
  with ~60 lines of canvas — a library was not worth an offline vendoring
  exercise.
- **Auth, mix export, user uploads** — out of scope per the project plan
  (export licensing implications are why SA/NC flags are stored now).

## 12. Manual QA items (not automatable in this environment)

The following testing-document items need a human with a browser/ears; code
paths they exercise are unit-covered, but final sign-off is manual:

| ID | Manual check | Automated support already in place |
|---|---|---|
| P4-03 | Listen for pitch/tempo artifacts across pairs | stretch ratio cap enforced; variants byte-served (P4-01) |
| P4-04 | DevTools network tab silent during drag | drag path calls no `fetch` by construction (all data preloaded) |
| P4-05 | Inline title editing feel | input handler unit-testable; persistence: see §11 |
| P4-06/07 | Seek + cursor animation smoothness | seek math and position bookkeeping covered in `audio.js`/state tests |
| P4-10 | Drag feel of track 2 | offset/magnetic math fully covered (align tests) |
| P4-14/15 | Drag-and-drop + color distinctness by eye | draggable gating and color constants covered/fixed |
| P4-22..24 | Pull "feel" | radii/easing asserted numerically |

---

*Everything else — weights, cutoffs, grid buckets, compliance rules, marker
UX — is implemented exactly as specified in the four source docs.*
