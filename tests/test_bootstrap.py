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
            self.assertEqual(cli("init", "--repo", "../repo", "--target", directory).returncode, 2)

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

    def test_refuse_partial_bootstrap_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            orbit_dir = Path(directory) / ".orbit"
            orbit_dir.mkdir()
            marker = orbit_dir / "operator-data.txt"
            marker.write_text("preserve me")
            result = cli("init", "--repo", "org/repo", "--target", directory)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(marker.read_text(), "preserve me")
            self.assertFalse((orbit_dir / "config.json").exists())

    def test_refuse_symlinked_bootstrap_directory(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            (Path(directory) / ".orbit").symlink_to(outside, target_is_directory=True)
            result = cli("init", "--repo", "org/repo", "--target", directory)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_reject_symlinked_config(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 0)
            config = Path(directory) / ".orbit/config.json"
            config.unlink()
            external = Path(outside) / "config.json"
            external.write_text("{}")
            config.symlink_to(external)
            self.assertEqual(cli("validate", "--target", directory).returncode, 2)

    def test_reject_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            target = Path(directory) / "target"
            target.symlink_to(outside, target_is_directory=True)
            result = cli("init", "--repo", "org/repo", "--target", str(target))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_reject_malformed_nested_config(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cli("init", "--repo", "org/repo", "--target", directory).returncode, 0)
            path = Path(directory) / ".orbit/config.json"
            data = json.loads(path.read_text())
            data["delivery"] = []
            path.write_text(json.dumps(data))
            result = cli("validate", "--target", directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("draft", result.stderr)


if __name__ == "__main__":
    unittest.main()
