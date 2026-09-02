# Automation test manifest

Which automated test proves which item in `testing-document.md`, and — just as
importantly — which items nothing proves yet.

Every row is traceable: the ID column matches the testing document exactly, and
each test name below exists verbatim in the referenced file. Regenerate the
cross-check any time with:

```bash
grep -rhoE "P[1-4]-[0-9]{2}" tests/ | sort -u
```

---

## The suites

| Suite | Runner | Count | Runs in | What it is for |
|---|---|---|---|---|
| **Backend** | `unittest` (stdlib) | 40 | ~6 s | Pipeline, compliance gates, API contract. Builds a real 5-track fixture catalog through one full ingestion. |
| **Frontend logic** | `node:test` (stdlib) | 42 | <1 s | The pure interaction modules (`state`, `align`, `crossfade`, `navbar`, `deck`, `attribution`) — no DOM, no WebAudio. |
| **Browser** | Playwright + Chromium | 12 | ~20 s | What the other two structurally cannot reach: native drag-and-drop, canvas pixels, the WebAudio clock, and real network behaviour. |
| **Manual** | a human with ears | 1 | — | Perceptual judgement only. |

```bash
./run_tests.sh              # all three automated suites
./run_tests.sh --fast       # skip the browser suite (no server boot)
npm run test:e2e            # browser suite alone
npm run test:e2e:headed     # watch it drive the app
```

### How the browser suite runs

`playwright.config.mjs` starts its **own** Flask server on **port 5199** against
its **own** catalog in `data-e2e/`. Neither is incidental:

- **Port 5199, not 5050.** `python -m backend.app` binds 5050, so a dev server
  is usually already there. With `reuseExistingServer` the suite would silently
  test against whatever catalog that process happens to hold.
- **`DJMIXER_DATA` must be absolute.** Ingestion stores file paths in SQLite
  exactly as given, and Flask's `send_file` resolves relative paths against the
  app root (`backend/`), not the cwd. A relative value produces 404s and 500s on
  every audio and waveform request.

First run ingests the 9-track offline catalog (~1 min); after that `data-e2e/`
is cached and startup is immediate. Delete the directory to rebuild.

The tests use no test hooks and reach into no module internals — they drive the
app through real events and read state back only from what the UI renders, so a
green run means the visible product works.

---

## Phase 1 — Ingestion & analysis

| ID | Requirement | Suite | Test |
|---|---|---|---|
| P1-01 | Only `audiodownload_allowed=true` tracks pass | Backend | `test_p1_01_download_gate_accepts_allowed`, `..._rejects_disallowed` |
| P1-02 | BPM, key, beat grid cached per track | Backend | `test_p1_02_cached_bpm_key_beatgrid`, `test_p1_02_beat_grid_spacing_matches_bpm` |
| P1-03 | Labeled sections for every track | Backend | `test_p1_03_segmentation_labels` |
| P1-04 | Prefix sums correct and O(1) | Backend | `test_p1_04_prefix_sums_match_bruteforce`, `test_p1_04_window_mean_is_o1` |
| P1-05 | Variants at every in-tolerance grid point | Backend | `test_p1_05_variants_for_every_grid_point_in_tolerance` |
| P1-06 | Rescaled grid/segments, no re-analysis per variant | Backend | `test_p1_06_rescale_matches_ratio_no_reanalysis`, `test_p1_06_analyze_called_once_per_track` |
| P1-07 | Specific CC variant stored, never defaulted | Backend | `test_p1_07_specific_license_stored` |
| P1-08 | ND tracks have zero stretched variants | Backend | `test_p1_08_nd_excluded_from_variants` |
| P1-09 | SA flagged distinctly | Backend | `test_p1_09_sa_flagged` |
| P1-10 | NC flagged distinctly | Backend | `test_p1_10_nc_flagged` |
| — | Phase exit criteria | Backend | `test_p1_exit_criteria` |

## Phase 2 — Matching & recommendation

| ID | Requirement | Suite | Test |
|---|---|---|---|
| P2-01 | Candidates share a BPM grid point | Backend | `test_p2_01_candidates_share_grid_point` |
| P2-02 | Weighted BPM + Camelot + energy formula | Backend | `test_p2_02_weighted_formula`, `..._camelot_component`, `..._bpm_component_prefers_less_stretch` |
| P2-03 | Response carries the score breakdown | Backend | `test_p2_03_breakdown_in_response` |
| P2-04 | Below-cutoff candidates excluded | Backend | `test_p2_04_cutoff_excludes_low_scores` |
| P2-05 | Order strictly descending by score | Backend | `test_p2_05_sorted_descending` |
| — | ND tracks never recommended (compliance) | Backend | `test_nd_tracks_never_recommended` |

## Phase 3 — Transition detection

| ID | Requirement | Suite | Test |
|---|---|---|---|
| P3-01 | Regions align with structural boundaries | Backend | `test_p3_01_regions_align_with_structure` |
| P3-02 | Windows beat-grid aligned, none off-grid | Backend | `test_p3_02_windows_beat_aligned` |
| P3-03 | Scoring reads prefix sums, never rescans frames | Backend | `test_p3_03_scoring_reads_prefix_sums_only` |
| P3-04 | Best window matches the verified fixture | Backend | `test_p3_04_best_matches_known_good_fixture`, `..._score_components_present_and_bounded` |
| P3-05 | Full scored curve retrievable | Backend | `test_p3_05_full_curve_retrievable` |

## Phase 4 — Client-side mixing & tactile UI

Bold rows are the ones the browser suite added; every one of them was on the
manual sign-off list in `design-document.md` §12 before Playwright existed.

| ID | Requirement | Suite | Test |
|---|---|---|---|
| P4-01 | Pre-rendered variants served; no client stretch | Backend | `test_p4_01_audio_serves_prerendered_variant`, `..._missing_variant_404` |
| P4-02 | `GainNode` automation drives the crossfade | Logic | `P4-02: equal-power crossfade — a^2 + b^2 == 1 …`, `P4-02: fade-out is monotonic …` |
| P4-03 | No audible pitch/tempo artifact | **Manual** | requires listening — see below |
| **P4-04** | **Zero server round-trips during drag** | **Browser** | `P4-04 dragging issues zero server round-trips` |
| **P4-05a** | **Title editable inline** | **Browser** | `P4-05a title is editable inline` |
| P4-05b | Title persists on reload | **Known gap** | `P4-05b title does NOT persist across reload` — see below |
| **P4-06** | **Click/drag the cursor seeks playback** | **Browser** | `P4-06 clicking the track window seeks playback to that time` |
| **P4-07** | **Cursor tracks playback, stops when paused** | **Browser** | `P4-07 cursor advances while playing and holds when paused` |
| P4-08 | Resizing the nav segment zooms | Logic | `P4-08: dragging the right edge resizes -> zooms proportionally`, `…left edge…` |
| P4-09 | Dragging the nav segment pans without zooming | Logic | `P4-09: dragging the segment pans without changing zoom`, `…clamps at the right edge…` |
| **P4-10** | **Track 2 draggable on x; timing follows** | **Browser** | `P4-10 track 2 can be dragged along the x-axis and mix timing follows` |
| P4-11 | Waveform from cached energy, not recomputed | Backend | `test_p4_11_waveform_from_cached_analysis`, `..._waveform_bpm_rescale` |
| P4-12 | Deck ranked strictly descending | Logic | `P4-12: deck order is strictly descending by match score` |
| P4-13 | Percentage **and** matching pie per row | Logic | `P4-13: numeric percentage matches the score`, `…pie fill angle…`, `…path geometry…`, `…edge cases…` |
| **P4-14** | **Rows draggable; drop enters mixing state** | **Browser** | `P4-14 deck rows are draggable and dropping one enters the mixing state` |
| **P4-15** | **Track 2 renders in a distinct colour** | **Browser** | `P4-15 track 2 renders in a colour distinct from track 1` |
| P4-16 | Drop auto-snaps to the best marker | Logic | `P4-16: on drop, offset snaps to the highest-scoring marker`, `…empty marker list…` |
| P4-17 | Both waveforms render only in the shared zone | Logic | `P4-17: overlap zone exists only where both tracks are present`, `…no overlap zone when…` |
| P4-18 | No third track | Logic + **Browser** | `P4-18: a third track cannot be added (max 2…)` · `P4-18 a third track cannot be added` |
| P4-19 | Overlap adjustable after the snap | Logic | `P4-19: overlap boundaries adjust when track 2 is dragged` |
| P4-20 | Marker size scales with score | Logic | `P4-20: marker size is strictly increasing with score and bounded` |
| **P4-21** | **Multiple markers render simultaneously** | **Browser** | `P4-21 multiple transition markers render simultaneously` |
| P4-22 | Free placement away from markers | Logic | `P4-22: drag far from every attractor is unchanged (free placement)` |
| P4-23 | Magnetic pull near markers and beats | Logic | `P4-23: magnetic pull engages inside the marker radius`, `…strongest at the center…`, `…beat-grid attractors…` |
| P4-24 | Pull never overrides deliberate placement | Logic | `P4-24: pull never overrides deliberate placement outside the radius`, `…eases off toward the radius edge…` |
| P4-25 | Drawn fade **is** the audible gain curve | Logic | `P4-25: outside the overlap, each track plays/draws at full gain`, `P4-25: inside the overlap, drawn gain follows the same curve` |
| P4-26 | Attribution for the selected track | Backend + Logic | `test_p4_26_27_attribution_for_all_tracks` · `P4-26: attribution line contains title, artist, and license name` |
| **P4-27** | **Attribution for track 2 once added** | Backend + **Browser** | `test_p4_26_27_attribution_for_all_tracks` · `P4-27 attribution is displayed for track 2 once added` |
| P4-28 | Attribution matches the stored CC variant | Backend + Logic | `test_p4_28_attribution_matches_stored_variant` · `P4-28: attribution reflects the stored CC variant across BY / BY-NC / BY-SA` |
| P4-29 | Reachable credits page with all four licenses | Backend | `test_p4_29_credits_endpoint` |

### API behaviour not assigned a testing-document ID

| Requirement | Suite | Test |
|---|---|---|
| ND pair rejected at `/api/transitions` (403) | Backend | `test_transitions_nd_pair_forbidden` |
| No shared grid point returns 409, not a 4xx client error | Backend | `test_transitions_no_shared_grid_conflict` |
| Transitions payload shape | Backend | `test_transitions_happy_path_payload` |
| License flags exposed for commercial audit (supports PL-10) | Backend | `test_license_flags_exposed_for_commercial_audit` |

---

## Known defects and gaps

Tracked here rather than left to be rediscovered. Both have a test attached, so
each will announce itself when fixed.

### API-01 — concurrent catalog reads fail (open defect)

**`tests/e2e/api-concurrency.spec.mjs`, marked `test.fail()`** — it is expected
to fail today, and Playwright raises an error the moment it starts passing.

`backend/app.py` opens one `sqlite3.Connection` with `check_same_thread=False`
and shares it across every Flask worker thread. `backend/db.py` guards writes
with `WRITE_LOCK` but takes no lock on reads (`get_track`, `analysis_of`,
`variants_for`). Concurrent `execute()` calls on a single connection interleave,
so a read either raises `sqlite3.InterfaceError` (served as **HTTP 500**) or
returns a phantom-empty row that the endpoint reports as **HTTP 404 for a track
that exists**.

The browser found this on the suite's first run: opening the page fires one
waveform request per deck row in parallel, and roughly **15–20% of them fail**.
The unittest suite cannot see it — Flask's `test_client` is single-threaded.

`design-document.md` §6 states that "`check_same_thread=False` + a write lock
handles Flask's threaded server". That is not sufficient; reads need the same
serialization, or each thread needs its own connection.

Repro without a browser, against a server on port 5199:

```bash
.venv/bin/python - <<'PY'
import concurrent.futures as cf, urllib.request, urllib.error, collections
IDS = ["1001","1002","1003","1004","1005","2001","2002","2003","2004"]
def hit(t):
    try:
        return urllib.request.urlopen(
            f"http://127.0.0.1:5199/api/tracks/{t}/waveform?points=120", timeout=15).status
    except urllib.error.HTTPError as e:
        return e.code
c = collections.Counter()
with cf.ThreadPoolExecutor(max_workers=9) as ex:
    for s in ex.map(hit, IDS*3): c[s] += 1
print(dict(c))   # concurrent: mixed 200/404/500 — sequential: all 200
PY
```

Impact in the running app is intermittent and mostly cosmetic today — a deck row
silently misses its mini waveform, because the failing `fetch` rejects inside a
`.then()` no one catches. It is not cosmetic at scale, and it is a correctness
bug in the storage layer, not the UI.

**It also destabilizes this suite.** Roughly one run in three lost a *setup*
request and failed an otherwise deterministic test, so `playwright.config.mjs`
sets `retries: 2` purely to absorb it. That retry allowance is a mitigation for
this defect and nothing else — set it back to `0` when API-01 is fixed, and
repair anything that still flakes rather than retrying it.

### P4-05b — mix title does not persist across reload (by design, v1)

`design-document.md` §11 records this: there is no mix-save entity in v1, so the
title lives only in memory. The test asserts the *current* behaviour and carries
the instruction to flip it when a mix entity ships — the gap stays visible
instead of silently absent.

---

## Still manual

The browser suite took `design-document.md` §12's manual list from seven items
to one. What remains genuinely needs a person:

| ID | Check | Why automation cannot settle it | Automated support in place |
|---|---|---|---|
| P4-03 | Listen for pitch/tempo artifacts across pairs | Perceptual judgement; no assertion distinguishes "correct but unpleasant" from "correct" | Stretch ratio capped (P1-05); variants byte-served (P4-01); rescale ratios asserted (P1-06) |
| P4-10, P4-22–24 | Drag *feel* — is the pull satisfying? | Tuning judgement, not behaviour. The behaviour is fully asserted | Radii, easing and offsets asserted numerically; drag mechanics asserted in the browser |

`PL-01` … `PL-10` are legal and process gates (counsel sign-off, published
policies, commercial licenses). They are not code and have no automated
equivalent; `test_license_flags_exposed_for_commercial_audit` supports the
PL-10 catalog audit by guaranteeing the data needed to perform it.

---

## Adding a case

1. Put it in the suite that can prove it *cheapest*: pure logic → `node:test`;
   pipeline or API → `unittest`; only-real-in-a-browser → Playwright.
2. Name the test with its testing-document ID as the first token, so the grep
   at the top of this file keeps finding it.
3. Add the row here. A test that exists but is not in this table is invisible;
   a row here with no test is a lie. Keep them in sync in the same commit.
