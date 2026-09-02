// Playwright config — the browser-automation suite (docs/automation-test-manifest.md).
//
// These tests cover the testing-document items that unit tests structurally
// cannot: real drag-and-drop, canvas rendering, WebAudio playback timing, and
// network behaviour during interaction. Everything else stays in the faster
// unittest / node:test suites — see the manifest for the full ID map.
import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(fileURLToPath(import.meta.url));

// The interpreter that runs the server under test. A local checkout has
// .venv; CI installs into the runner's own Python and has none, so hardcoding
// .venv/bin/python made the webServer fail to start there — which surfaces as
// "Process from config.webServer exited early" rather than anything about a
// missing interpreter. $PYTHON overrides both.
const VENV_PYTHON = path.join(ROOT, ".venv", "bin", "python");
const PYTHON = process.env.PYTHON
  || (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3");

// Deliberately NOT 5050: `python -m backend.app` uses that port, so a dev
// server (or another working session) is usually already sitting on it. Its
// catalog is whatever happens to be in ./data — testing against that would be
// non-deterministic, so the suite runs its own server on its own port.
const PORT = 5199;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "**/*.spec.mjs",

  // One shared Flask server + one SQLite file: the suite runs serially.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // No retries. These were a mitigation for the API-01 defect (the shared
  // SQLite connection race), which is fixed twice over: backend/db gives each
  // thread its own request-scoped connection, and backend/dbguard caps how many
  // requests may be inside the database at once. If anything flakes now, the
  // test is wrong and should be repaired rather than retried.
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],

  // Playback assertions wait on real audio time; keep the per-test budget generous.
  timeout: 60_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // Canvas pixel assertions (P4-15, P4-21) assume 1 device px per CSS px.
    deviceScaleFactor: 1,
    viewport: { width: 1280, height: 900 },
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        deviceScaleFactor: 1,
        viewport: { width: 1280, height: 900 },
        launchOptions: {
          args: [
            // The mix only plays after a click, which already counts as user
            // activation — this just removes any ambiguity in headless.
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
          ],
        },
      },
    },
  ],

  // Ingestion runs on first boot into a dedicated data dir, so the e2e catalog
  // is independent of whatever is in ./data. First run takes ~1 min; after that
  // the dir is cached and startup is immediate. Delete data-e2e/ to rebuild.
  //
  // DJMIXER_TRACKS points at the suite's own catalog rather than
  // config/tracks.json: these specs need a fixed set of mixable, harmonically
  // adjacent tracks, and the shipped catalog is a live Jamendo playlist that
  // needs network, takes ~10 min to ingest, and is ~90% ND-licensed (so almost
  // nothing in it can be dragged together at all).
  webServer: {
    // create_app() is invoked directly rather than via `python -m backend.app`
    // so the port can be chosen here without patching the backend.
    command:
      `"${PYTHON}" -c "from backend.app import create_app; ` +
      `create_app().run(host='127.0.0.1', port=${PORT})"`,
    url: `${BASE_URL}/api/health`,
    // Must be ABSOLUTE. Ingestion stores file paths in SQLite as given, and
    // Flask's send_file resolves relative paths against the app root
    // (backend/), not the cwd — a relative value here yields 404/500 on every
    // audio and waveform request.
    env: {
      DJMIXER_DATA: path.join(ROOT, "data-e2e"),
      DJMIXER_TRACKS: path.join(ROOT, "tests", "e2e", "tracks.e2e.json"),
      // Empty, not absent. .env.local points the app at the shared local
      // PostgreSQL, and this suite needs its own throwaway catalog under
      // data-e2e/ rather than whatever a dev server left behind. An empty
      // value is falsy in config.database_url(), so the server falls back to
      // SQLite; unsetting it instead would let .env.local win.
      DJMIXER_DATABASE_URL: "",
      PYTHONUNBUFFERED: "1",
    },
    timeout: 300_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
