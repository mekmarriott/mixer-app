# DJ mixing app — implementation recap & dependencies

*General research summary, not legal advice — have counsel review before any real launch, especially the copyleft and licensing items flagged below.*

## Implementation steps

### Phase 1 — Ingestion & analysis pipeline
1. Pull starter catalog from Jamendo API (filtered to `audiodownload_allowed=true`, 2-3 compatible genres/BPM ranges — e.g. house 120-128 BPM, downtempo 85-95 BPM)
2. **Filter out ND-licensed tracks** (see Legal notes below — this is a hard technical gate, not just paperwork)
3. Run each track through Essentia once (native tempo) to extract: BPM, key, beat grid, frame-level MFCC/chroma/energy features
4. Run structural segmentation (self-similarity matrix + novelty peaks) on the frame-level features → labeled sections (intro/verse/build/drop/breakdown/outro)
5. Compute prefix-sum arrays over energy/spectral-flux frames (enables O(1) sliding-window lookups later)
6. Define a shared fixed-BPM grid per genre bucket (e.g. house: 120,121...128) within a safe stretch tolerance (~±8-10%)
7. Batch-render each track at every grid point in its bucket using an offline, high-quality time-stretch — rescale the original beat grid/segments by the stretch ratio rather than re-running Essentia per variant
8. Cache everything: BPM, key, segments, prefix-sum features, per-track CC license variant, and paths to each rendered variant

**Exit criteria:** every track has cached analysis + a full set of tempo-matched audio variants ready to serve.

### Phase 2 — Matching & recommendation logic
1. Recommend next-tracks only from those sharing a BPM grid point with the current track
2. Score candidates: BPM fit (from shared grid, so near-binary) + Camelot-wheel key compatibility + energy continuity
3. Surface score breakdown (not just one number) in the API response

**Depends on:** Phase 1 cached data for the full catalog.

### Phase 3 — Transition-point detection
1. Use segmentation to find candidate *regions* (outro/breakdown zones)
2. Within those regions, generate overlapping candidate windows via beat-grid-aligned sliding window (8/16-bar, 1-2 bar hop)
3. Score each window pair (energy compatibility, downbeat phase alignment, spectral/bass compatibility, segment-role fit) using the precomputed prefix sums — cheap, no re-analysis
4. Store the scored curve per track pair (or compute on demand — it's cheap) for the UI graph

**Depends on:** Phase 1 (segments + prefix sums).

### Phase 4 — Client-side mixing & tactile UI
1. Client loads the two pre-rendered, tempo-matched variants (already in the same BPM — no live stretching needed)
2. Web Audio API: `GainNode`-based crossfade, scheduled against the beat grid
3. Waveform + transition-score graph rendered from cached data, overlaid on a drag-and-drop timeline
4. **Display attribution** (artist, title, license link) for both tracks currently loaded — see Legal notes
5. Dragging updates playback live — no server round-trip since all the heavy lifting already happened at ingestion

**Depends on:** Phases 1-3. This is where bugs upstream become visible.

---

## Dependencies

### Music source

| Tool | Role | Legal & compliance obligations |
|---|---|---|
| **Jamendo API** | Track catalog, search, download | Free for non-commercial use; commercial use requires contacting Jamendo for a quote. All Jamendo CC licenses start with **BY (Attribution)** — every track requires displaying artist name, track title, and a link to that track's specific CC license, wherever it's played or listed. Jamendo supports 6 CC variants (no CC0) that vary **per track**, so license type must be stored per-track, not assumed catalog-wide: <br>• **ND (No Derivatives)** — cannot create derivative works. **This directly conflicts with the time-stretch rendering pipeline** (Phase 1, step 7) — ND tracks must be excluded from BPM-variant generation, or restricted to unmodified playback only. <br>• **SA (ShareAlike)** — any derivative you distribute (e.g. a future exported/shareable mix) must carry the same CC-SA license. Relevant if/when mix export ships. <br>• **NC (NonCommercial)** — fine for prototype; blocks monetized use until resolved (see commercial-use gating below). |

### Analysis

| Tool | Role | Legal & compliance obligations |
|---|---|---|
| **Essentia** (Python/C++) | BPM, key, beat grid, segmentation features | AGPLv3 (free) or commercial license from Music Technology Group (UPF, price on request). **AGPL's copyleft is triggered by network use, not just distribution** — running Essentia server-side behind an API that users interact with over the network likely obligates making the corresponding source of that service available to those users. This is a real product/business decision, not a checkbox — confirm with counsel before real launch whether your architecture triggers it, and whether to open-source, isolate the component, or obtain a commercial license. Must also include AGPL license text + copyright notices in an open-source credits page regardless. |
| **Essentia pre-trained models** (optional) | Higher-level classification | CC BY-NC-ND — attribution required, non-commercial only, **no modification or redistribution of the model itself**. Separate terms from core Essentia; don't assume the same license covers both. |

### Offline time-stretching (BPM-grid rendering)

| Tool | Role | Legal & compliance obligations |
|---|---|---|
| **Rubber Band Library** (`librubberband` / `pyrubberband`) | High-quality offline time-stretch, run once per track per grid point | GPL or commercial dual license. Unlike AGPL, standard GPL's copyleft is generally triggered by **distributing** the software, not by network/SaaS use alone (the "ASP loophole" AGPL was written to close) — but whether your specific server-side batch-rendering architecture avoids triggering it is a genuine, architecture-dependent legal question, not something to assume either way. Get counsel to confirm before real launch, or use the commercial license to sidestep the question entirely. License text/notice required in the open-source credits page regardless of which path you take. |

### Client-side playback

| Tool | Role | Legal & compliance obligations |
|---|---|---|
| **Web Audio API** (native browser) | Playback, gain-based crossfade, scheduling | None — browser standard, not a licensed dependency. |
| **wavesurfer.js** | Waveform rendering + timeline interaction | Permissive (BSD-3-Clause) — no functional restriction; retain license/copyright notice via the open-source credits page. |
| **Tone.js** (optional) | Higher-level scheduling/transport wrapper | Permissive (MIT) — same treatment as wavesurfer.js. |

### Hosting — app & compute

| Component | Recommendation | Legal & compliance obligations |
|---|---|---|
| Frontend | **Vercel** or **Netlify** (free tier) | Governed by platform Terms of Service / Acceptable Use Policy, not a code license — standard hosting compliance (no illegal/infringing content at scale). |
| Backend / batch jobs | **Render** or **Railway** (free/low-cost tier) | Same — ToS/AUP compliance, not a code-license obligation. |

### Hosting — database

| Component | Recommendation | Legal & compliance obligations |
|---|---|---|
| Metadata DB | **Supabase** (Postgres, free tier: 500MB database, 50K MAUs) | Once the app stores any user data (accounts, playback history), you take on a baseline obligation for a **Privacy Policy**, and GDPR/CCPA-style disclosures if serving EU/California users. This is a consequence of *having user data at all*, not specific to Supabase — but Supabase is where that data will live. |

### Hosting — file storage

| Component | Recommendation | Legal & compliance obligations |
|---|---|---|
| Audio file storage | **Cloudflare R2** | ToS/AUP compliance for hosted content. **If user-uploaded audio is ever added** (raised earlier as a fallback for "recognizable" music), this becomes a real content-hosting liability: you'd need a Terms of Service clause requiring uploaders represent they own/have rights to what they upload, plus a DMCA takedown policy and a registered DMCA agent if serving US users. Not needed for the Jamendo-only prototype — only if/when uploads ship. |

**Storage math:** 500 tracks × 20 BPM-grid variants × ~4MB/track ≈ **40GB**, plus ~2GB for original masters ≈ **~42GB total**. After the 10GB free tier, ~32GB paid at $0.015/GB/month ≈ **under $0.50/month**.

---

## Standing items to resolve before real launch (not blockers now)
- Essentia commercial license terms (AGPL fine for prototype)
- Rubber Band commercial license terms (or legal confirmation that server-side-only use doesn't trigger GPL copyleft)
- Jamendo commercial-use licensing conversation
- Confirm Jamendo's CC terms cover storing *derivative* (time-stretched) renders, not just originals — this is separate from the ND-exclusion rule, since even BY/BY-NC/BY-SA tracks are derivatives once stretched
- Privacy Policy, Terms of Service, and (if uploads ship) DMCA policy — see `requirements.md` for the full checklist
