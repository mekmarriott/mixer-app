"""Per-track ingestion state.

`status` is a HIGH-WATER MARK of completed work, ordered so that resuming an
interrupted ingestion is "skip every stage at or below it":

    pending -> fetched -> analyzed -> ready

Failure is recorded separately, in `status_error`, precisely so a failed
attempt does not erase the record of work that did succeed. Folding failure
into the status — a 'failed' value overwriting 'fetched' — would mean a crash
during analysis lost the knowledge that the audio was already downloaded, and
the retry would fetch it all over again. That is the one thing this state is
for, so the two facts are kept apart.
"""
PENDING = "pending"      # known from config, nothing done yet
FETCHED = "fetched"      # audio downloaded + master persisted to disk
ANALYZED = "analyzed"    # analysis + segments cached
READY = "ready"          # variants rendered (or ND: none needed)

ORDER = {PENDING: 0, FETCHED: 1, ANALYZED: 2, READY: 3}

#: Column stamped when a track reaches each stage.
STAMP = {FETCHED: "fetched_at", ANALYZED: "analyzed_at", READY: "ready_at"}


def at_least(status, target):
    """True when `status` represents at least as much completed work."""
    return ORDER.get(status, 0) >= ORDER.get(target, 0)


def is_failed(track):
    """True when a track stopped short of ready with a recorded error.

    `track` is a row carrying `status` and `status_error` — a `db.Track` or a
    `db.ListTrackStatusesRow`.
    """
    return bool(track.status_error) and track.status != READY
