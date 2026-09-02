# Requirements — compliance & dependency restrictions

*Compiled from `dj-app-project-plan.md`. General research summary, not legal advice — have counsel review the copyleft (AGPL/GPL) and content-licensing items before real launch. Prototype-stage development is not blocked by any of these; they gate a real/monetized launch.*

## 1. Attribution & display requirements

- [ ] Store the specific CC license variant (BY / BY-NC / BY-SA / BY-ND / BY-NC-SA / BY-NC-ND) per Jamendo track — not assumed catalog-wide
- [ ] Display artist name, track title, and a link to that track's specific CC license wherever it is played or listed in the app (e.g. now-playing view, track list, mixing timeline)
- [ ] Publish an open-source credits/licenses page listing: Essentia (AGPLv3), Rubber Band (GPL), wavesurfer.js (BSD-3-Clause), Tone.js (MIT) — include license text or a link to it for each

## 2. Content-filtering requirements (technical, driven by license terms)

- [ ] Exclude **ND-licensed** tracks from the BPM-variant time-stretch rendering pipeline (Phase 1, step 7) — the stretch operation creates a derivative work, which ND licenses prohibit. **Resolved: skipped entirely, and before the audio is downloaded.** The licence is known from the metadata request, so refusing at that point costs one small request instead of a download, an analysis pass and a variant set — on the live catalogue that is 64 of 72 tracks. The licence variant is still recorded per §1; what is not acquired is the audio.
- [ ] Tag **SA-licensed** tracks so that any future mix-export/sharing feature can attach the correct pass-through CC-SA license to the resulting derivative work
- [ ] Track **NC-licensed** tracks distinctly — fine for the free/non-commercial prototype, but must be resolved (via Jamendo's commercial licensing) before any monetized launch
- [ ] **Resolve the Jamendo API caching terms against the pre-rendered-variant architecture.** The API terms state that applications "must not be specifically designed to cache the content nor offering an offline access to the content", permitting caching "only to the extent reasonably necessary for the operation of the Application". This app permanently stores each downloaded master **and** every rendered BPM variant under `data/`, and the pre-rendering design depends on doing so. This is a *separate question from the per-track CC licence*: a CC licence may permit a copy that the API terms restrict, so satisfying §1/§2 does not settle this. Options are to confirm the cache is "reasonably necessary" for this architecture, negotiate terms with Jamendo, or re-render variants on demand rather than persisting them. Raised during the move from synthesized to live audio; **not resolved.**

## 3. API terms of use — the caching clause (distinct from the CC licenses)

*This is a separate obligation from §2. The CC license governs what you may do
with the **work**; Jamendo's API terms separately govern what you may do with
content obtained **through their service**. A CC-BY track can be freely copied
and adapted under its license while the API terms still restrict caching it —
the two can disagree, and satisfying §2 does not satisfy this.*

Jamendo's API terms of use state:

> Applications must not be specifically designed to cache the content nor
> offering an offline access to the content.

with caching permitted only "to the extent reasonably necessary for the
operation of the Application", and applications required to "reflect changes
made to the content as soon as reasonably possible."

**Why this bears directly on the architecture.** The app does not incidentally
cache. Phase 1 downloads every master, stores it permanently, renders and
stores multiple time-stretched derivative variants per track, and serves all of
it from our own object storage indefinitely. A user plays audio from our
bucket, not from Jamendo. That is closer to a redistributed offline copy of the
catalog than to a cache, and the wording "specifically designed to cache the
content" is the part that does not obviously bend — pre-rendering *is* the
design, not an optimisation layered on top of it.

The strongest counter-argument is that this is what the CC licenses expressly
permit (copying, and for non-ND tracks, adapting), that `audiodownload_allowed`
is Jamendo's own signal that download is sanctioned per track, and that
serving a stretched variant is arguably "necessary for the operation of the
Application" since the app cannot function without pre-rendered variants. That
argument may well be right. It is not one to rely on without asking.

- [ ] **Ask Jamendo directly** whether permanent storage of masters plus
      rendered derivative variants, served from our own storage, is within the
      API terms. This is the cheapest possible resolution: they answer the
      question authoritatively, and a written answer resolves it permanently.
      Do this **before** a large batch ingest, not after — at 10,000 tracks the
      remediation cost of a "no" is deleting a catalog that took ~167
      core-hours to build.
- [ ] Confirm the answer covers **derivative** variants specifically, not just
      the original files — the stretch renders are the harder case, and are
      also the ones that dominate storage.
- [ ] If the answer is no, or is not obtainable, decide between: rendering
      variants on demand and evicting them (turning permanent storage into a
      genuine cache), a commercial/content agreement with Jamendo, or moving to
      a source whose terms permit redistribution outright.
- [ ] Revisit "reflect changes to the content as soon as reasonably possible" —
      a permanently cached catalog does not currently notice a track being
      delisted or relicensed upstream. Some periodic re-validation is likely
      needed regardless of how the clause above is resolved.

*Note that this clause is not triggered at prototype scale in the way it is at
catalog scale. Nine synthesized tracks raise no question at all; ten thousand
real ones served from our own CDN is a different proposition, and the terms do
not draw the line for us.*

## 4. Source-availability / copyleft requirements

- [ ] Get counsel confirmation on whether AGPL (Essentia) obligates open-sourcing the backend service, given the specific architecture (network-accessible service using Essentia server-side)
- [ ] Get counsel confirmation on whether GPL (Rubber Band) is triggered by a server-side-only, non-distributed usage pattern, or whether it isn't (the AGPL "network use" clause doesn't exist in standard GPL)
- [ ] Decide and document one path before monetized launch: open-source the app, obtain commercial licenses for Essentia/Rubber Band, or restructure service boundaries to avoid triggering copyleft obligations

## 5. Legal/consent documents needed before real launch

- [ ] **Privacy Policy** — covering any data collected (accounts, playback history, analytics), required as soon as the app stores user data via Supabase
- [ ] **Terms of Service** — including any user-generated-content clauses if uploads are ever added
- [ ] **Cookie/tracking consent banner** — if analytics/tracking is added and the app serves EU users (GDPR/ePrivacy)
- [ ] **DMCA / copyright takedown policy + registered agent** — only required if/when user-uploaded audio ships (not needed for the Jamendo-only prototype)

## 6. Commercial-use gating

Do not enable monetization/paid features until all of the following are resolved:

- [ ] Essentia commercial license (or confirmed AGPL compliance path)
- [ ] Rubber Band commercial license (or confirmed GPL compliance path)
- [ ] Jamendo commercial-use license/quote — raise the §3 caching question in the same conversation rather than separately
- [ ] Full catalog re-audit: confirm every track in active use is either NC-compatible with your model or covered by a commercial license
