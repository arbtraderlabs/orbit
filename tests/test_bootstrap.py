import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))


def cli(*args):
    return subprocess.run([sys.executable, "-m", "orbit", *args], cwd=ROOT, env=ENV, text=True, capture_output=True)


class BootstrapTests(unittest.TestCase):
    def test_unrelated_fresh_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            self.assertEqual(cli("init", "--repo", "example/unrelated-project", "--target", str(p)).returncode, 0)
            self.assertEqual(cli("validate", "--target", str(p)).returncode, 0)
            self.assertEqual(cli("doctor", "--target", str(p)).returncode, 0)
            data = json.loads((p / ".orbit/config.json").read_text())
            self.assertEqual(data["repository"], "example/unrelated-project")
            self.assertFalse(data["safety"]["auto_merge"])
            self.assertTrue((p / ".orbit/AGENT_BOOTSTRAP.md").exists())

    def test_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 0)
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 2)

    def test_reject_invalid_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("init", "--repo", "not-a-slug", "--target", directory).returncode, 2)

    def test_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("validate", "--target", directory).returncode, 2)

    def test_reject_unsafe_config(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 0)
            path = Path(directory) / ".orbit/config.json"
            data = json.loads(path.read_text())
            data["safety"]["auto_merge"] = True
            path.write_text(json.dumps(data))
            self.assertEqual(cli("validate", "--target", directory).returncode, 2)

    def test_reject_bad_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 0)
            path = Path(directory) / ".orbit/config.json"
            data = json.loads(path.read_text())
            data["limits"]["admissions_per_day"] = 31
            path.write_text(json.dumps(data))
            self.assertEqual(cli("validate", "--target", directory).returncode, 2)


if __name__ == "__main__":
    unittest.main()
