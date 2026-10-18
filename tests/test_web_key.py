import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class WebKeyTest(unittest.TestCase):
    def run_driver(self, payloads, existing_key=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "web_key.py", project / "web_key.py")
            (project / "gpsoauth.py").write_text(
                "def perform_oauth(*args, **kwargs):\n"
                "    return {'Auth': 'https://accounts.google.com/fake-private-auth'}\n"
            )
            package = project / "node_modules" / "puppeteer-core"
            package.mkdir(parents=True)
            package.joinpath("payloads.json").write_text(json.dumps(payloads))
            package.joinpath("index.js").write_text("""
const assert = require('node:assert/strict');
const payloads = require('./payloads.json');
assert(!process.argv.some(arg => arg.includes('fake-private-auth')));
let deliver;
const timers = [];
const page = {
  exposeFunction: async (name, callback) => { deliver = callback; },
  evaluateOnNewDocument: async () => {},
  goto: async url => {
    assert.equal(url, 'https://accounts.google.com/fake-private-auth');
    const send = payload => deliver('setVaultSharedKeys', ['account', JSON.stringify(payload)]);
    send(payloads[0]);
    payloads.slice(1).forEach((payload, i) => timers.push(setTimeout(() => send(payload), 20 * (i + 1))));
  },
  title: async () => 'Test',
  url: () => 'https://accounts.google.com/',
  evaluate: async () => ''
};
exports.launch = async () => ({newPage: async () => page, close: async () => timers.forEach(clearTimeout)});
""")
            master = root / "master.txt"
            master.write_text("fake-master")
            key = root / "key.b64"
            if existing_key is not None:
                key.write_text(existing_key)
            env = dict(os.environ)
            env.pop("NODE_PATH", None)
            result = subprocess.run(
                [sys.executable, str(project / "web_key.py"), "--email", "test@example.com",
                 "--android-id", "0123456789abcdef", "--master-token-file", str(master),
                 "--timeout", "1", "-o", str(key)],
                cwd=root, env=env, capture_output=True, text=True, timeout=15,
            )
            contents = key.read_text() if key.exists() else None
            mode = key.stat().st_mode & 0o777 if key.exists() else None
            return result, contents, mode

    def test_waits_for_requested_domain_and_selects_latest_valid_key(self):
        expected = bytes([7]) * 32
        result, contents, mode = self.run_driver([
            {"chromesync": [{"epoch": 989, "key": base64.b64encode(b"x" * 16).decode()}]},
            {"other_domain": [{"epoch": 999, "key": base64.b64encode(b"y" * 32).decode()}],
             "on_device_location_history": [
                 {"epoch": 1, "key": base64.b64encode(b"z" * 16).decode()},
                 {"epoch": 3, "key": {"keyMaterial": {str(i): b for i, b in enumerate(expected)}}},
                 {"epoch": 2, "key": base64.b64encode(b"a" * 32).decode()},
             ]},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base64.b64decode(contents), expected)
        self.assertEqual(mode, 0o600)
        self.assertIn("epoch 3", result.stderr)

    def test_accepts_urlsafe_base64_and_full_domain_name(self):
        expected = b"\xff" * 32
        result, contents, _ = self.run_driver([
            {"users/me/securitydomains/on_device_location_history": [
                {"epoch": 4, "keyMaterial": base64.urlsafe_b64encode(expected).decode().rstrip("=")}
            ]}
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base64.b64decode(contents), expected)

    def test_invalid_or_wrong_domain_keys_do_not_overwrite_output_or_leak(self):
        secret = base64.b64encode(b"secret" * 5 + b"!!").decode()
        for payload in (
            {"other_domain": [{"epoch": 1, "key": secret}]},
            {"on_device_location_history": [{"epoch": 1, "key": base64.b64encode(b"x" * 16).decode()}]},
            {"on_device_location_history": [{"epoch": 1, "key": "!" + secret}]},
            {"on_device_location_history": [{"epoch": 1, "key": {str(i): 256 for i in range(32)}}]},
        ):
            with self.subTest(payload=payload):
                result, contents, _ = self.run_driver([payload], existing_key="keep-existing-key")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no 32-byte key delivered", result.stderr)
                self.assertEqual(contents, "keep-existing-key")
                self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
