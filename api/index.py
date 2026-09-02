"""Vercel serverless entrypoint.

Vercel's Python runtime discovers a WSGI callable named `app` in this file.
`backend/app.py` only builds one under `if __name__ == "__main__"`, which works
for `python -m backend.app` locally but gives a serverless platform nothing to
import — hence this module.

Two things are load-bearing here:

* **`run_ingestion=False`.** The default startup path ingests the catalog when
  the tracks table is empty. On Vercel that would fire on every cold start of
  every instance, against a read-only filesystem, inside the function timeout.
  Ingestion is a local batch job (backend/publish.py).

* **Module-scope construction.** The app is built once per instance at import,
  so the database connection and blob-store resolution are paid on cold start
  rather than per request.
"""
import os
import sys
from pathlib import Path

# Vercel invokes this file directly, so the repository root is not necessarily
# on sys.path the way it is under `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("BLOB_BACKEND", "vercel")

from backend.app import create_app  # noqa: E402

app = create_app(run_ingestion=False)
