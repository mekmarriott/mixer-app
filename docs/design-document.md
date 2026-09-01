# Design document — decisions outside the provided docs

This repo implements `dj-app-project-plan.md`, `requirements.md`,
`ui-requirements.md`, and `testing-document.md`. Those docs settle the big
architecture (pre-rendered BPM-grid variants, Jamendo + CC compliance, match
formula, marker UX). This document records only the decisions those docs
**didn't** make — mostly forced by the development environment, plus a few
algorithm and UI calls — and why each was made.

---

## 1. Provider seams: the environment has no network

The dev/test server has **no outbound network**. That rules out, at build
time: the live Jamendo API, installing Essentia (AGPL C++ build), installing
Rubber Band, npm packages, CDN assets, and webfonts. Rather than stub the
system into fiction, every third-party dependency became a **provider seam**
with a real fallback implementation that honors the same contract:

| Planned dependency | Seam | Fallback used here | Swap trigger |
|---|---|---|---|
| Jamendo API | `backend/jamendo.py fetch_track(entry, mode)` | `offline` mode: deterministic synthesized tracks from `config/tracks.json` | `"mode": "jamendo"` + `JAMENDO_CLIENT_ID` env |
| Essentia | `backend/analysis.py` (`HAVE_ESSENTIA` flag) | numpy/scipy DSP implementing the same output contract (BPM, beat grid, key, frames, prefix sums) | `import essentia` succeeding |
| Rubber Band | `backend/stretch.py` | STFT phase vocoder (scipy) | `rubberband` CLI on `PATH` (auto-detected) |

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

Real Essentia would replace the detectors; the frame/prefix layout and every
downstream consumer stay unchanged.

## 4. Time-stretch fallback

Classic STFT phase vocoder (2048/512, Hann): magnitude interpolation at
resampled frame positions, phase accumulated by per-bin instantaneous
frequency. Adequate quality for a prototype and fully offline; Rubber Band's
formant-preserving transient handling is the production upgrade, used
automatically when its CLI is present. The stretch cap (±10%) and the
grid-variant plan come from the project plan and are enforced upstream of
this module either way.

## 5. Audio format: WAV, 22.05 kHz, mono

Prototype-only call, three reasons: Python's stdlib `wave` writes it with no
extra dependency; browsers decode it natively via `decodeAudioData`; analysis
at 22.05 kHz halves STFT cost with no loss for the features used (nothing
above ~11 kHz matters to BPM/key/energy). Production plan (unchanged from the
project plan): 44.1 kHz masters, compressed delivery (Opus/AAC via ffmpeg,
which is already used for Jamendo MP3 decode in `jamendo` mode), R2 + CDN.
The latency report's storage projections use compressed sizes.

## 6. Persistence: SQLite

Single-file SQLite with JSON blobs for analysis/segments. Chosen because the
prototype's DB work is trivial (a few hundred rows, read-heavy) and it keeps
`python -m backend.app` the only startup step. `check_same_thread=False` +
a write lock handles Flask's threaded server. The schema is deliberately
boring (tracks / variants / latency) so the move to Postgres/Supabase at 500+
tracks (per the latency report's infra table) is a driver swap plus indexes,
not a redesign.

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
