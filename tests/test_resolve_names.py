import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class ResolveNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = Path(__file__).resolve().parents[1]
        for name in ("resolve_names.js", "export_all.sh", "build_records.py"):
            shutil.copyfile(source / name, self.root / name)
        consent = self.root / "get_consent_cookie.sh"
        consent.write_text("#!/usr/bin/env bash\nexit 0\n")
        consent.chmod(0o755)
        browser = self.root / "bin" / "chromium"
        browser.parent.mkdir()
        browser.write_text("#!/usr/bin/env bash\nexit 1\n")
        browser.chmod(0o755)
        package = self.root / "node_modules" / "puppeteer-core"
        package.mkdir(parents=True)
        package.joinpath("index.js").write_text("""
const fs = require('fs');
const timeout = global.setTimeout;
global.setTimeout = (fn, ms) => timeout(fn, Math.min(ms, 2));
let cid, count = 0;
const page = {
  setCookie: async () => {},
  goto: async url => {
    cid = new URL(url).searchParams.get('cid');
    fs.appendFileSync(process.env.MOCK_REQUESTS, cid + '\\n');
    if (++count === Number(process.env.MOCK_INTERRUPT_AT)) {
      process.kill(process.pid, 'SIGINT');
      await new Promise(() => {});
    }
    if (count === Number(process.env.MOCK_HOLD_AT)) {
      await new Promise(() => setInterval(() => {}, 1000));
    }
  },
  url: () => 'https://www.google.com/maps/place/Synthetic/0x' + cid + ':0x' + cid,
  evaluate: async () => ({h1: 'Synthetic', addr: 'Address: Synthetic address', cat: 'Test'}),
  title: async () => 'Synthetic'
};
exports.launch = async () => ({newPage: async () => page, close: async () => {}});
""")
        self.data = self.root / "data"
        self.data.mkdir()
        self.timeline = self.data / "Timeline-latest.json"
        self.timeline.write_text(json.dumps({"semanticSegments": [
            {"startTime": "2025-01-01T12:00:00+00:00", "endTime": "2025-01-01T13:00:00+00:00",
             "visit": {"topCandidate": {"featureId": f"0x{i}:0x{i}"}}} for i in range(1, 4)
        ]}))
        self.requests = self.root / "requests.txt"
        self.cache = self.data / "place_cache_browser.json"
        self.env = dict(os.environ, RESOLVE_DELAY_MS="0", MOCK_REQUESTS=str(self.requests),
                        PATH=str(browser.parent) + os.pathsep + os.environ["PATH"])
        for name in ("GOOGLE_MAPS_API_KEY", "NODE_PATH", "RESOLVE", "RESOLVE_LIMIT", "CONSENT_JAR", "MOCK_INTERRUPT_AT", "MOCK_HOLD_AT"):
            self.env.pop(name, None)

    def export(self, *flags):
        return subprocess.run(["bash", "export_all.sh", "--resume", *flags, str(self.data)],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)

    def test_verbose_progress_and_resume_reuse_completed_cache(self):
        first = self.export("--verbose")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("no fetch or decode", first.stdout)
        self.assertIn("3/3 (100%)", first.stdout)
        self.assertIn("ETA ~0m", first.stdout)
        self.assertIn("3/3 (100%)", self.data.joinpath("resolve.log").read_text())
        self.assertEqual(len(json.loads(self.cache.read_text())), 3)
        self.assertTrue(self.data.joinpath("Timeline-full.json").is_file())
        second = self.export()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("ETA", second.stdout + second.stderr)
        self.assertEqual(self.requests.read_text().splitlines(), ["1", "2", "3"])

    def test_interruption_preserves_each_completed_lookup(self):
        stopped = subprocess.run(["node", "resolve_names.js", str(self.timeline)], cwd=self.root,
                                 env=dict(self.env, MOCK_INTERRUPT_AT="2"),
                                 capture_output=True, text=True, timeout=15)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertEqual(list(json.loads(self.cache.read_text())), ["0x1:0x1"])
        self.assertFalse(Path(str(self.cache) + ".tmp").exists())
        resumed = self.export("--verbose")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.requests.read_text().splitlines(), ["1", "2", "2", "3"])
        self.assertEqual(len(json.loads(self.cache.read_text())), 3)

    def test_resume_retries_cached_errors(self):
        self.cache.write_text(json.dumps({
            "0x1:0x1": {"name": "Synthetic", "category": "Test"},
            "0x2:0x2": {"name": None, "category": None, "error": "consent-wall (refresh cookie)"},
            "0x3:0x3": {"name": None, "category": None},
        }))
        result = self.export("--verbose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests.read_text().splitlines(), ["2"])
        self.assertNotIn("error", json.loads(self.cache.read_text())["0x2:0x2"])

    def test_ctrl_c_stops_pipeline_without_building_partial_records(self):
        process = subprocess.Popen(
            ["bash", "export_all.sh", "--resume", "--verbose", str(self.data)],
            cwd=self.root, env=dict(self.env, MOCK_HOLD_AT="2"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.requests.exists() and len(self.requests.read_text().splitlines()) == 2:
                    break
                time.sleep(0.01)
            else:
                self.fail("resolver did not reach the in-flight lookup")
            os.killpg(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            self.assertIn("Interrupted; resume with:", stderr)
            self.assertFalse(self.data.joinpath("Timeline-full.json").exists())
            self.assertEqual(list(json.loads(self.cache.read_text())), ["0x1:0x1"])
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def test_resume_without_export_fails_before_starting_browser(self):
        self.timeline.unlink()
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertFalse(self.requests.exists())


if __name__ == "__main__":
    unittest.main()
