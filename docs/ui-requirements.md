# UI requirements — mixing interface

*Derived from `DJ_Mixer_Prototype_UI.pdf` (4-slide mockup). Complements `dj-app-project-plan.md` — component data sources are cross-referenced to the phases defined there.*

## Component inventory (initial state — one track selected)

| Component | Description |
|---|---|
| **Title** | Mix name, e.g. "My First Mix" |
| **Track Window** | Main timeline area; displays the Selected Track as a waveform (line shows volume/intensity over time) |
| **Player Cursor** | Vertical line inside the Track Window marking current playback position |
| **Total Mix Time** | Duration display, top-right (e.g. "12:23") |
| **Nav Bar** | Thin horizontal bar directly below the Track Window; the filled (blue) segment represents the currently visible portion of the timeline relative to the full track |
| **Suggested Song Deck** | Ranked list of candidate next-tracks below the Track Window — each row shows a waveform preview, song name + artist, a percentage match score, and a circular/pie compatibility indicator |

## Interaction requirements

### Title
- [ ] Editable inline text field; persists as the mix name

### Player Cursor
- [ ] Controls audio playback: moving/clicking it seeks playback to that point in time
- [ ] **Draggable whether playing or paused**, and grabbable even where it sits over a track — the strip along the top of the Track Window is a scrub ruler. Scrubbing while playing pauses for the duration of the gesture and resumes from where it is released
- [ ] **Separate from track dragging.** Grabbing the playhead never moves a track; dragging a track never moves the playhead
- [ ] Animates automatically across the Track Window in sync with elapsed time during playback

### Nav Bar (timeline overview control)
- [ ] The filled segment is both **resizable** (drag an edge to expand/contract it → zooms the Track Window in/out) and **draggable** (drag the whole segment → pans the Track Window's visible time range)
- [ ] Acts as a minimap/scrollbar for the Track Window, not a standalone navigation menu

### Track Window
- [ ] Tracks placed here can be **dragged along the x-axis** (time) to reposition them
- [ ] Waveform line represents volume/intensity, sourced from cached energy data (Phase 1 pipeline output), not computed live in the browser

### Suggested Song Deck
- [ ] List is ranked by mixing score (highest first) for the currently selected track — sourced from the Phase 2 recommendation/matching logic
- [ ] Each row displays: waveform preview, song name + artist, numeric match percentage, **and** a circular/pie visual filled proportionally to that percentage (both representations shown together, not one or the other)
- [ ] Each row is **draggable** from the deck; dropping it onto the Track Window initiates the overlay/mixing state described below

## Overlay / multi-track mixing state

Triggered when a Suggested Song Deck candidate is dropped onto the Track Window.

- [ ] The dropped track ("Selected Track 2") renders as its own waveform, visually distinguished by color from the original ("Selected Track") — mockup uses pink/magenta for track 1, blue for track 2
- [ ] Track 2's waveform **overlaps** Track 1's in a shared transition region — both waveforms render simultaneously in that zone rather than one replacing the other; before the overlap, only Track 1 is visible; after it, only Track 2 is visible
- [ ] **On drop, Track 2 auto-snaps to the highest-scoring mixing marker** (the best-scoring candidate from the Phase 3 transition-window curve) — the user isn't required to manually align it on first drop
- [ ] **Up to 100 tracks per mix.** A mix is an ordered *chain*: each track overlaps only its immediate neighbours, so a transition is always pairwise and at most two tracks sound at once. N-way layering is still out of scope — what is in scope is a set of arbitrary length, up to several hours.
- [ ] **Tracks can be deleted** — first, middle or last. The track immediately to the right drops onto the best transition point with its new predecessor (the two were never neighbours, so its old gap is meaningless); everything further right keeps its transition relative to its own predecessor. Select a track by clicking it, then press Delete or click the x on its label
- [ ] **Edits ripple rigidly.** Moving, inserting or deleting a track shifts everything downstream by the same amount, preserving every transition after the edit. Each track stores its gap from the previous track, not an absolute position, so a ripple is one edit rather than a rewrite of the tail.
- [ ] After the initial snap, the overlap region's boundaries remain user-adjustable (consistent with "tracks can be dragged on the x-axis" above) — dragging a track changes where its overlaps start/end, subject to the beat-grid constraint below

### Mixing markers
- [ ] Rendered as arrow/marker indicators at the top of the Track Window, positioned at candidate transition start points for Track 2
- [ ] **Marker size is proportional to the compatibility/overlap score at that point** — a direct visual encoding of the Phase 3 scoring curve, not a fixed-size icon. Scaled **relative to the candidates on screen** (weakest at the smallest size, strongest at the largest): scores for one pair cluster in a narrow band, and an absolute scale renders them indistinguishable
- [ ] Multiple markers can appear simultaneously (mockup shows 3), representing multiple candidate start points, not just the single best one
- [ ] **Every junction in the chain keeps its own markers.** A mix of N tracks has N-1 junctions; adding a track must not erase the transition points of the ones before it

### Free-drag alignment behavior
- [ ] The user can drag a track to a non-marked point, not just the pre-scored markers — but every placement lands on the beat grid; positions between beats are not reachable
- [ ] While dragging, a marker within reach wins outright; otherwise the track lands on the **nearest beat**. Beats can never be misaligned — an off-grid position is not representable, not merely discouraged
- [ ] The marker catch radius is a tunable interaction parameter (currently 1.25 s) — wide enough that a good transition is easy to hit, narrow enough that it never overrides a deliberate placement elsewhere. Outside it, the nearest beat always wins

### Cross-fade visualization
- [ ] Within the overlap region, the waveform/volume visuals for both tracks show the actual crossfade shape (e.g. Track 1's line tapering down as Track 2's tapers up) rather than a hard cut or an unmodified overlay of two full-volume waveforms
- [ ] This should reflect the real gain-automation curve driving playback (Phase 4: client-side `GainNode` crossfade), so the visual stays truthful to what's actually audible — not a decorative approximation

## Data source mapping (for engineering reference)

| UI element | Backed by |
|---|---|
| Waveform / volume visual | Cached energy features from Essentia (Phase 1) |
| Match % + pie indicator | `match_score` output (Phase 2 formula) |
| Mixing markers + sizing | Transition-window scoring curve (Phase 3 sliding-window candidates) |
| Cross-fade shape | Live gain automation during client-side playback (Phase 4) |
| Draggable tracks / live preview | Pre-rendered, tempo-matched variants — no server round-trip (Phase 4 architecture decision) |

## Design decisions (resolved)

- **Auto-snap on drop:** yes — Track 2 snaps to the highest-scoring mixing marker when first dropped
- **Max tracks per mix:** 100 (chained; overlaps are pairwise between neighbours)
- **Edit model:** rigid ripple — downstream tracks keep their spacing
- **Free placement:** allowed anywhere on the beat grid; a nearby marker wins, otherwise the nearest beat. Off-grid placement is not reachable
