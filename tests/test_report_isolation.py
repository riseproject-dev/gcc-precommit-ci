"""Check credential boundaries and safe handling of logs from patch builds."""

import io
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_log_preview


class ReportingTests(unittest.TestCase):
    def test_patch_execution_jobs_use_only_read_permissions(self):
        folder = ROOT / ".github/workflows"
        worker = (folder / "regression-worker.yaml").read_text()
        defaults, worker_jobs = worker.split("\njobs:\n")
        self.assertEqual(
            defaults.split("\npermissions:\n")[1].rstrip(),
            "  actions: read\n  contents: read",
        )
        self.assertNotIn("secrets", defaults)
        blocks = re.split(r"(?m)^  ([a-z-]+):\n", worker_jobs)
        jobs = dict(zip(blocks[1::2], blocks[2::2], strict=True))
        workers = {"build", "test", "rerun-timeouts", "run-on-self-hosted"}
        self.assertEqual(set(jobs), workers)
        for name in workers:
            with self.subTest(name=name):
                job = jobs[name]
                for forbidden in (
                    "secrets.",
                    "issues: write",
                    "environment:",
                    "report-build-complete-status",
                ):
                    self.assertNotIn(forbidden, job)
                self.assertIn("persist-credentials: false", job)
                if "self-hosted," in job:
                    self.assertIn("rise-precommit-regression-07", job)
                    self.assertNotIn("rise-gcc-ci", job)
        self.assertIn("steps.build-gcc-status.outcome", jobs["run-on-self-hosted"])
        reporter_jobs = (
            (folder / "test-regression.yaml").read_text().split("\njobs:\n")[1]
        )
        blocks = re.split(r"(?m)^  ([a-z-]+):\n", reporter_jobs)
        reporters = dict(zip(blocks[1::2], blocks[2::2], strict=True))
        self.assertIn(
            "uses: ./.github/workflows/regression-worker.yaml", reporters["execute"]
        )
        self.assertNotIn("secrets:", reporters["execute"])
        for name in ("report-queued", "report-complete"):
            self.assertIn("runs-on: ubuntu-24.04", reporters[name])
            self.assertIn("environment: production", reporters[name])
            self.assertNotIn("extract-apply-patches", reporters[name])
            self.assertNotIn("download-artifact@", reporters[name])
        self.assertIn("&& 'success' || 'failure'", reporters["report-complete"])

    def test_only_allowlisted_log_tails_become_text_inputs(self):
        name = "123-linux-rv64gc-multilib-build-log"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as data:
            data.writestr(name + ".log", b"olddata-new")
            data.writestr(name + "-stderr.log", b"bad\xff")
            data.writestr("extra.sh", b"irrelevant")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "previews"
            with patch.object(build_log_preview, "PREVIEW_BYTES", 4):
                build_log_preview.write_previews(archive, name, output)
            self.assertEqual(
                sorted(p.name for p in output.iterdir()), ["stderr.txt", "stdout.txt"]
            )
            self.assertEqual((output / "stdout.txt").read_text(), "-new")
            self.assertEqual((output / "stderr.txt").read_text(), "bad\ufffd")
            with patch.object(build_log_preview, "MAX_LOG_BYTES", 4):
                with self.assertRaises(ValueError):
                    build_log_preview.write_previews(archive, name, output)
