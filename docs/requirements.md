# Requirements — compliance & dependency restrictions

*Compiled from `dj-app-project-plan.md`. General research summary, not legal advice — have counsel review the copyleft (AGPL/GPL) and content-licensing items before real launch. Prototype-stage development is not blocked by any of these; they gate a real/monetized launch.*

## 1. Attribution & display requirements

- [ ] Store the specific CC license variant (BY / BY-NC / BY-SA / BY-ND / BY-NC-SA / BY-NC-ND) per Jamendo track — not assumed catalog-wide
- [ ] Display artist name, track title, and a link to that track's specific CC license wherever it is played or listed in the app (e.g. now-playing view, track list, mixing timeline)
- [ ] Publish an open-source credits/licenses page listing: Essentia (AGPLv3), Rubber Band (GPL), wavesurfer.js (BSD-3-Clause), Tone.js (MIT) — include license text or a link to it for each

## 2. Content-filtering requirements (technical, driven by license terms)

- [ ] Exclude **ND-licensed** tracks from the BPM-variant time-stretch rendering pipeline (Phase 1, step 7) — the stretch operation creates a derivative work, which ND licenses prohibit. Either skip these tracks entirely or restrict them to unmodified native-tempo playback only, with no mixing features.
- [ ] Tag **SA-licensed** tracks so that any future mix-export/sharing feature can attach the correct pass-through CC-SA license to the resulting derivative work
- [ ] Track **NC-licensed** tracks distinctly — fine for the free/non-commercial prototype, but must be resolved (via Jamendo's commercial licensing) before any monetized launch
- [ ] **Resolve the Jamendo API caching terms against the pre-rendered-variant architecture.** The API terms state that applications "must not be specifically designed to cache the content nor offering an offline access to the content", permitting caching "only to the extent reasonably necessary for the operation of the Application". This app permanently stores each downloaded master **and** every rendered BPM variant under `data/`, and the pre-rendering design depends on doing so. This is a *separate question from the per-track CC licence*: a CC licence may permit a copy that the API terms restrict, so satisfying §1/§2 does not settle this. Options are to confirm the cache is "reasonably necessary" for this architecture, negotiate terms with Jamendo, or re-render variants on demand rather than persisting them. Raised during the move from synthesized to live audio; **not resolved.**

## 3. Source-availability / copyleft requirements

- [ ] Get counsel confirmation on whether AGPL (Essentia) obligates open-sourcing the backend service, given the specific architecture (network-accessible service using Essentia server-side)
- [ ] Get counsel confirmation on whether GPL (Rubber Band) is triggered by a server-side-only, non-distributed usage pattern, or whether it isn't (the AGPL "network use" clause doesn't exist in standard GPL)
- [ ] Decide and document one path before monetized launch: open-source the app, obtain commercial licenses for Essentia/Rubber Band, or restructure service boundaries to avoid triggering copyleft obligations

## 4. Legal/consent documents needed before real launch

- [ ] **Privacy Policy** — covering any data collected (accounts, playback history, analytics), required as soon as the app stores user data via Supabase
- [ ] **Terms of Service** — including any user-generated-content clauses if uploads are ever added
- [ ] **Cookie/tracking consent banner** — if analytics/tracking is added and the app serves EU users (GDPR/ePrivacy)
- [ ] **DMCA / copyright takedown policy + registered agent** — only required if/when user-uploaded audio ships (not needed for the Jamendo-only prototype)

## 5. Commercial-use gating

Do not enable monetization/paid features until all of the following are resolved:

- [ ] Essentia commercial license (or confirmed AGPL compliance path)
- [ ] Rubber Band commercial license (or confirmed GPL compliance path)
- [ ] Jamendo commercial-use license/quote
- [ ] Full catalog re-audit: confirm every track in active use is either NC-compatible with your model or covered by a commercial license
