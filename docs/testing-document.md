# Testing document — DJ mixing app

*Synthesizes `dj-app-project-plan.md` (phases), `requirements.md` (compliance), and `ui-requirements.md` (UI/interaction) into testable requirements per phase. Each item is written as a checkable test; source doc noted in brackets where useful for traceability.*

## How to use this doc
- Phase sections (1-4) map 1:1 to the phases in `dj-app-project-plan.md` — a phase isn't "done" until its tests here pass, not just its build steps.
- The final section, **Pre-Launch / Commercial Readiness Gate**, is cross-cutting — it doesn't belong to any one phase and must pass before monetized launch regardless of which build phase is complete.
- IDs (P1-01, etc.) are for traceability if you wire this into a test tracker later.

---

## Phase 1 — Ingestion & analysis pipeline

### Functional
- [ ] **P1-01** Jamendo pull returns only tracks with `audiodownload_allowed=true`
- [ ] **P1-02** Every ingested track has cached BPM, key, and beat-grid data after the Essentia pass
- [ ] **P1-03** Segmentation produces labeled sections (intro/verse/build/drop/breakdown/outro) for every track
- [ ] **P1-04** Prefix-sum arrays are computed per track and return correct aggregate values for an arbitrary window in O(1) (spot-check against a brute-force sum)
- [ ] **P1-05** BPM-grid variants are rendered for every grid point within tolerance, per track, per genre bucket
- [ ] **P1-06** Rescaled beat-grid/segment data for each variant matches the expected stretch ratio (not re-run through Essentia — verify no duplicate analysis calls per variant)

### Compliance [`requirements.md` §1, §2]
- [ ] **P1-07** Every ingested track has its specific CC license variant stored (BY / BY-NC / BY-SA / BY-ND / BY-NC-SA / BY-NC-ND) — no track left blank or defaulted
- [ ] **P1-08** Tracks tagged **ND** are hard-excluded from the BPM-variant rendering step — verify zero stretched-variant files exist for any ND-tagged track
- [ ] **P1-09** Tracks tagged **SA** are flagged distinctly in the DB for downstream export-license handling
- [ ] **P1-10** Tracks tagged **NC** are flagged distinctly in the DB for downstream commercial gating

### Exit criteria
- [ ] Every non-ND track in the catalog has complete cached analysis + a full set of tempo-matched variants

---

## Phase 2 — Matching & recommendation logic

- [ ] **P2-01** Recommended candidates only include tracks sharing at least one BPM grid point with the current track
- [ ] **P2-02** Match score correctly combines BPM fit + Camelot key compatibility + energy continuity per the weighted formula
- [ ] **P2-03** API response includes the score breakdown (not just the total), matching each component's computed value
- [ ] **P2-04** Candidates scoring below the defined cutoff are excluded from the returned recommendation list
- [ ] **P2-05** Returned candidate order is strictly descending by match score

**Depends on:** Phase 1 tests passing for the full catalog.

---

## Phase 3 — Transition-point detection

- [ ] **P3-01** Candidate regions from segmentation align with actual structural boundaries (outro/breakdown zones) on a manually-verified sample track
- [ ] **P3-02** Sliding-window candidates are generated within/around those regions, aligned to the beat grid (no off-grid window start positions)
- [ ] **P3-03** Window scoring reads from precomputed prefix sums — verify no re-scan of raw frame data occurs per window (performance/architecture check, not just output correctness)
- [ ] **P3-04** The highest-scoring window pair for a known test-track pair matches a manually-verified "good transition" fixture
- [ ] **P3-05** The full scored curve for a track pair is retrievable (not just the top result) — required for marker rendering in Phase 4

**Depends on:** Phase 1 (segments + prefix sums).

---

## Phase 4 — Client-side mixing & tactile UI

### Playback & mixing engine
- [ ] **P4-01** Client loads pre-rendered, tempo-matched variants — verify no client-side time-stretch computation occurs at runtime
- [ ] **P4-02** Crossfade is driven by `GainNode` automation scheduled against the beat grid
- [ ] **P4-03** No audible pitch/tempo artifact ("chipmunk effect") — manual listening QA across a representative sample of track pairs
- [ ] **P4-04** Dragging a track updates playback live with zero server round-trips (verify via network inspection during drag)

### Title
- [ ] **P4-05** Title field is editable inline; the entered name persists correctly on reload

### Player cursor
- [ ] **P4-06** Clicking or dragging the cursor seeks playback to that time
- [ ] **P4-07** Cursor animates in sync with playback position while playing, and stops moving when paused

### Nav bar (timeline overview control)
- [ ] **P4-08** Resizing the filled segment zooms the Track Window view proportionally
- [ ] **P4-09** Dragging the filled segment pans the Track Window's visible time range without changing zoom level

### Track window
- [ ] **P4-10** Tracks can be dragged along the x-axis; repositioning updates playback timing accordingly
- [ ] **P4-11** Waveform display matches cached energy data — not recomputed client-side

### Suggested song deck
- [ ] **P4-12** List is ranked strictly descending by match score
- [ ] **P4-13** Each row shows both the numeric percentage AND a pie/circular fill visually matching that percentage
- [ ] **P4-14** Each row is draggable; dropping it on the Track Window triggers the overlay/mixing state

### Overlay / multi-track mixing state
- [ ] **P4-15** Dropped track ("Selected Track 2") renders as a distinctly colored waveform from Track 1
- [ ] **P4-16** On drop, Track 2 auto-snaps to the highest-scoring mixing marker (per resolved design decision)
- [ ] **P4-17** In the shared transition zone, both waveforms render simultaneously; outside it, only the relevant single track renders
- [ ] **P4-18** A mix accepts up to 100 tracks; the 101st is refused with a user-facing reason (raised from 2 — see `ui-requirements.md` §Overlay)
- [ ] **P4-18b** Edits ripple rigidly: moving, inserting or deleting a track shifts every downstream track by the same amount, leaving later transitions unchanged
- [ ] **P4-19** After the initial snap, overlap boundaries remain adjustable via drag

### Mixing markers
- [ ] **P4-20** Marker size scales with the transition-window score, **relative to the candidate set on screen**, so differences within one pair's narrow score band remain visible
- [ ] **P4-21** Multiple markers render simultaneously when multiple viable candidates exist for a given track pair

### Free-drag alignment
- [ ] **P4-22** A track can be dragged to an arbitrary (non-marked) point — placement is not hard-locked to markers only
- [ ] **P4-23** Every placement lands on the beat grid: a marker within reach wins, otherwise the nearest beat. No drag can leave beats misaligned
- [ ] **P4-24** Beat snapping does not drag a deliberate placement onto a distant marker — only the nearest beat

### Cross-fade visualization
- [ ] **P4-25** The waveform/volume visual in the overlap region reflects the actual live gain-automation curve driving playback — not a static decorative blend

### Attribution & licensing display [`requirements.md` §1]
- [ ] **P4-26** Artist name, track title, and a link to that track's specific CC license are displayed for the Selected Track
- [ ] **P4-27** Same attribution is displayed for Selected Track 2 once added to the overlay
- [ ] **P4-28** Attribution content correctly reflects each track's stored CC variant — spot-check across at least BY, BY-NC, and BY-SA examples
- [ ] **P4-29** An open-source credits page is reachable from the app and lists Essentia (AGPLv3), Rubber Band (GPL), wavesurfer.js (BSD-3-Clause), and Tone.js (MIT), each with license text or a link to it

**Depends on:** Phases 1-3. This is where upstream bugs typically surface first.

---

## Pre-launch / commercial readiness gate

*Cross-cutting — required before monetized launch regardless of which phase above is complete. [`requirements.md` §3, §4, §5]*

- [ ] **PL-01** Legal counsel has signed off on AGPL (Essentia) obligations for the actual deployed architecture
- [ ] **PL-02** Legal counsel has signed off on GPL (Rubber Band) obligations for the actual deployed architecture
- [ ] **PL-03** Privacy Policy is published and linked in-app
- [ ] **PL-04** Terms of Service is published, linked in-app, and presented for acceptance at signup/first use
- [ ] **PL-05** Cookie/tracking consent banner is implemented if analytics are added and EU users are served
- [ ] **PL-06** DMCA policy + registered agent are in place — required only if/when a user-upload feature ships
- [ ] **PL-07** Essentia commercial license is obtained, OR a documented AGPL compliance path is implemented
- [ ] **PL-08** Rubber Band commercial license is obtained, OR a confirmed-safe GPL compliance path is documented
- [ ] **PL-09** Jamendo commercial-use license/quote is obtained
- [ ] **PL-10** Full catalog audit confirms every active track is either NC-compatible with the current (non-monetized) model or covered by a commercial license
