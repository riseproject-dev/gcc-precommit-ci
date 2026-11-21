import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from precommit_result import render


class ResultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "evidence").mkdir()
        (self.root / "evidence/evidence.md").write_text("Evidence rows")
        (self.root / "temp_comment.md").write_text(
            "---\ntitle: Patch status\n---\n\n# Summary\n|New Failures|gcc|g++|\n"
        )
        (self.root / "labels.txt").write_text("")
        self.set_verdict()

    def set_verdict(self, confirmed=0, unverified=0, complete=True):
        (self.root / "evidence/verdict.json").write_text(
            json.dumps(
                dict(confirmed=confirmed, unverified=unverified, complete=complete)
            )
        )

    def test_complete_known_failures_are_success(self):
        (self.root / "labels.txt").write_text("resolved-regressions")
        state, _, labels = render(self.root, True, True, True)
        self.assertEqual(state, "success")
        self.assertEqual(labels, "resolved-regressions")

    def test_unverified_observation_is_visible_and_warns(self):
        self.set_verdict(unverified=1)
        (self.root / "labels.txt").write_text("new-regressions")
        state, _, labels = render(self.root, True, True, True)
        self.assertEqual((state, labels), ("warning", ""))
        body = (self.root / "comment.md").read_text()
        self.assertIn("Evidence rows", body)
        self.assertIn("Newly observed results|gcc|", body)
        self.assertNotIn("|New Failures|", body)

    def test_unexplained_difference_between_filtered_and_raw_reports(self):
        (self.root / "labels.txt").write_text("new-regressions")
        state, _, labels = render(self.root, True, True, True)
        self.assertEqual((state, labels), ("warning", "invalid"))
        self.assertIn("could not be matched", (self.root / "comment.md").read_text())

    def test_confirmed_result_survives_partial_matrix(self):
        self.set_verdict(confirmed=2, complete=False)
        (self.root / "labels.txt").write_text("build-failure")
        state, description, labels = render(self.root, False, True, True)
        self.assertEqual(state, "fail")
        self.assertIn("incomplete coverage", description)
        self.assertEqual(labels, "build-failure,invalid,new-regressions")

    def test_incomplete_inputs_never_report_success(self):
        for flags in ((False, True, True), (True, False, True), (True, True, False)):
            with self.subTest(flags=flags):
                state, _, labels = render(self.root, *flags)
                self.assertEqual((state, labels), ("warning", "invalid"))
        self.set_verdict(complete=False)
        self.assertEqual(render(self.root, True, True, True)[0], "warning")

    def test_build_failure_is_distinguished_from_regression(self):
        (self.root / "labels.txt").write_text("build-failure")
        state, description, labels = render(self.root, True, True, True)
        self.assertEqual(state, "warning")
        self.assertIn("build failure", description)
        self.assertEqual(labels, "build-failure,invalid")

    def test_missing_or_malformed_evidence(self):
        p = self.root / "evidence/verdict.json"
        p.unlink()
        self.assertEqual(render(self.root, True, True, True)[0], "warning")
        for raw in (
            "{}",
            "not JSON",
            '{"confirmed":true,"unverified":0,"complete":true}',
        ):
            with self.subTest(raw=raw):
                p.write_text(raw)
                self.assertEqual(render(self.root, True, True, True)[0], "warning")

    def test_missing_aggregate_preserves_verified_rows(self):
        self.set_verdict(confirmed=1)
        (self.root / "temp_comment.md").unlink()
        state, _, labels = render(self.root, True, True, True)
        self.assertEqual((state, labels), ("fail", "invalid,new-regressions"))
        self.assertIn("Evidence rows", (self.root / "comment.md").read_text())


if __name__ == "__main__":
    unittest.main()
