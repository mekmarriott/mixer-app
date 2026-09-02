"""Startup warmup: ingestion, waveform precompute, and readiness reporting.

The server binds its port immediately and reports progress while it works, so a
browser that arrives mid-warmup gets an honest "preparing catalog" screen
instead of a hung request or a half-rendered page.

Warmup ends with every native waveform envelope already in memory, which is
what lets the opening page load without a single per-track database read.
"""
import threading
import time

from . import config, deck, licensing
from .db import status as db_status
from .timing import Timer

# `ingest` is imported lazily inside _ingest() rather than at module scope. It
# pulls in analysis/stretch/synth and, through them, scipy (105 MB installed) —
# none of which the request path executes. app.py imports this module at module
# scope, so a top-level import here would put the whole scipy tree into every
# serverless cold start, against a 250 MB bundle limit.
# See docs/infrastructure-plan.md §1.3.

# Phases, in order. Everything except `ready` and `failed` is transient.
INGESTING = "ingesting"
PRECOMPUTING = "precomputing"
READY = "ready"
FAILED = "failed"


#: Rows given stored energies per warmup pass. Small on purpose: the pass runs
#: on a cold start that already has a 30 second ceiling to respect, and the
#: backlog is finite and shrinks with every start.
ENERGY_BACKFILL_PER_RUN = 25


class Warmup:
    def __init__(self, database, cache):
        self.database = database
        self.cache = cache
        self._lock = threading.Lock()
        self._state = {
            "phase": INGESTING,
            "ready": False,
            "done": 0,
            "total": 0,
            "message": "Starting up",
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }
        self.deck = []          # zero-state genre groups, built at the end

    # ---------------------------------------------------------------- state
    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def snapshot(self):
        with self._lock:
            s = dict(self._state)
        s["elapsed_s"] = round((s["finished_at"] or time.time()) - s["started_at"], 1)
        # Ready means done, whatever the phase counters happen to hold.
        if s["ready"]:
            s["percent"] = 100
        elif s["total"]:
            s["percent"] = round(100.0 * s["done"] / s["total"])
        else:
            s["percent"] = 0
        return s

    @property
    def ready(self):
        with self._lock:
            return self._state["ready"]

    # ----------------------------------------------------------------- run
    def run(self, run_ingestion=True):
        """Ingest if needed, then precompute. Safe to call on a thread."""
        try:
            if run_ingestion:
                self._ingest()
            self._precompute()
            self._set(phase=READY, ready=True, message="Ready",
                      finished_at=time.time())
        except Exception as exc:            # surfaced to the client, not swallowed
            self._set(phase=FAILED, ready=False,
                      error=f"{type(exc).__name__}: {exc}",
                      message="Startup failed", finished_at=time.time())
            raise
        # Deliberately after READY is published, and deliberately swallowing:
        # this is catch-up work for rows that predate the energy columns, and
        # it must not delay a boot or be able to fail one. Whatever it does not
        # finish, the next cold start continues.
        try:
            self._backfill_energies()
        except Exception:
            pass

    def _backfill_energies(self):
        """Derive the stored energy columns for rows still missing them.

        Matching falls back to reading analysis_json and segments_json when
        these are NULL — precisely the per-candidate blob read the columns
        exist to avoid — so an un-backfilled catalog scores at the old cost.
        Deriving them needs no network and no re-ingest: the blobs are already
        in the row.

        Bounded per run because this shares the cold start's budget with
        everything else. The work is idempotent and the remainder is picked up
        by later starts, so a large catalog converges over a few of them rather
        than putting one boot at risk.
        """
        from . import matching                       # lazy: see module header
        with self.database.reading() as q:
            pending = [r.id for r in q.list_tracks_missing_energies()]
        pending = pending[:ENERGY_BACKFILL_PER_RUN]
        for tid in pending:
            with self.database.reading() as q:
                analysis = q.get_track_analysis(id=tid)
                segments = q.get_track_segments(id=tid)
            if not analysis or not segments:
                continue
            try:
                outro, intro = matching.region_energies(analysis, segments)
            except (KeyError, IndexError, TypeError):
                continue                    # re-ingestion's problem, not this
            with self.database.writing() as q:
                q.set_track_energies(id=tid, outro_energy=outro,
                                     intro_energy=intro)

    def start_async(self, run_ingestion=True):
        t = threading.Thread(target=self.run, kwargs={"run_ingestion": run_ingestion},
                             name="warmup", daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------- phase 1
    def _ingest(self):
        """Ingest whatever is not already done.

        Resumable rather than all-or-nothing: a track at `ready` costs no
        request and is counted as done immediately, so a restart with a full
        catalog still finishes in milliseconds, while a catalog left half
        ingested by an interrupted run resumes from where it stopped instead
        of re-downloading everything.

        One track failing does not abort warmup — the rest of the catalog is
        still worth serving, and the failures are reported in the phase
        message and by GET /api/ingest.
        """
        cfg = config.load_tracks_config()
        entries = cfg["tracks"]
        catalog = self.database.catalog
        self._set(phase=INGESTING, total=len(entries), done=0,
                  message=f"Ingesting {len(entries)} tracks")
        config.ensure_dirs()

        timer = Timer(self.database)
        failures = []
        for i, entry in enumerate(entries, 1):
            tid = str(entry["id"])
            if catalog.status_of(tid) == db_status.READY:
                self._set(done=i)
                continue
            self._set(message=f"Analyzing {entry.get('name', entry['id'])}"
                              f" ({i}/{len(entries)})")
            from . import ingest      # lazy: see module header
            try:
                ingest.ingest_track(self.database, entry, cfg["mode"], timer)
            except Exception as exc:
                failures.append(f"{tid}: {type(exc).__name__}: {exc}")
            self._set(done=i)

        if failures:
            self._set(message=f"{len(failures)} track(s) incomplete "
                              f"(see /api/ingest); serving the rest")

    # ------------------------------------------------------------- phase 2
    @staticmethod
    def _row(t):
        """A deck row from a summary. Costs no analysis read — the summary is
        the blob-free projection, which is what makes selecting the deck cheap
        enough to do before any waveform work."""
        return {
            "id": t.id, "name": t.name, "artist": t.artist, "genre": t.genre,
            "bpm": t.native_bpm, "camelot": t.camelot,
            "duration_s": t.duration_s, "mixable": t.mixable,
            "license_flags": {"nd": t.license_nd, "sa": t.license_sa,
                              "nc": t.license_nc},
            "attribution": licensing.attribution(t),
            "status": t.status,
            # Nothing stores popularity yet; deck.py orders by it when it
            # appears and falls back to a deterministic shuffle until then.
            "popularity": getattr(t, "popularity", None),
        }

    def _precompute(self):
        """Select the deck, then warm only the waveforms it shows.

        This used to read `analysis_json` for every ready track and warm both
        native envelopes before reporting ready. Those blobs are megabytes
        each, so the cost scaled with the catalog and was paid in full on every
        cold start — 16 seconds for 72 tracks on Vercel, against a 30 second
        function limit, before the first request could be answered.

        Almost none of it was used. The deck shows at most
        DECK_TRACKS_PER_GENRE per genre and omits anything unmixable, and every
        other envelope the UI asks for already has a lazy path through
        WaveformCache.get_or_compute. So the deck is chosen from the blob-free
        summaries first, and only those rows are warmed; the rest of the
        catalog is computed on first request and cached from then on.
        """
        with self.database.reading() as q:
            summaries = [t for t in q.list_track_summaries()
                         if t.status == db_status.READY]

        self._set(message="Building deck")
        groups = deck.genre_groups([self._row(t) for t in summaries])
        wanted = [row for g in groups for row in g["tracks"]]

        self._set(phase=PRECOMPUTING, total=len(wanted), done=0,
                  message=f"Precomputing waveforms for {len(wanted)} deck tracks")
        for i, row in enumerate(wanted, 1):
            with self.database.reading() as q:
                analysis = q.get_track_analysis(id=row["id"])
            if analysis:
                self.cache.warm_native(row["id"], analysis)
            # Inline the thumbnail so the opening deck is a pure snapshot read.
            wf = self.cache.get(row["id"], config.DECK_WAVEFORM_POINTS)
            row["waveform"] = wf["points"] if wf else None
            self._set(done=i)

        self.deck = groups
