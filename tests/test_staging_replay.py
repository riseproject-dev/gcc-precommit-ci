from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import staging_replay as replay


class StagingReplayTests(unittest.TestCase):
    def setUp(self):
        get = self.enterContext(patch.object(replay.requests, "get"))
        self.responses = []
        get.side_effect = self.responses
        self.staging = Mock()
        self.details = Mock(return_value={"date": "2026-11-17T07:17:22Z"})

    def page(self, items):
        self.responses.append(Mock(json=Mock(return_value=items)))

    def outputs(self):
        return replay.replay_outputs(
            "org/repository", "test-credential", self.staging, self.details
        )

    def test_no_eligible_closed_issue_skips_replay(self):
        self.page([{"title": "ordinary PR", "pull_request": {}}])
        self.staging.filter_issue.return_value = False
        self.assertEqual(self.outputs(), {"found": "false"})
        self.staging.parse_info.assert_not_called()
        self.details.assert_not_called()
        workflow = (ROOT / ".github/workflows/staging.yaml").read_text()
        caller = workflow.split("\n  run-workflow:\n", 1)[1]
        self.assertIn("needs.get-patch-info.outputs.found == 'true'", caller)

    def test_eligible_history_preserves_identifiers_and_timestamps(self):
        issue, comment = {"number": 2}, {"body": "apply report"}
        self.page([issue])
        self.page([{}, comment, {}])
        self.staging.filter_issue.return_value = True
        baseline, trunk = "a" * 40, "b" * 40
        self.staging.parse_info.return_value = ("149346", baseline, trunk)
        self.assertEqual(
            self.outputs(),
            {
                "found": "true",
                "timestamp": "2026-11-17T07:17:22+00:00",
                "baseline_hash": baseline,
                "tot_hash": trunk,
                "patch_id": "149346",
                "issue_num": "2",
            },
        )
        self.staging.parse_info.assert_called_once_with(issue, comment)
        self.details.assert_called_once_with("149346")

    def test_api_error_fails_before_selection(self):
        failed = Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("HTTP 403")
        self.responses.append(failed)
        with self.assertRaises(requests.HTTPError):
            self.outputs()
        self.staging.filter_issue.assert_not_called()

    def test_malformed_response_is_not_an_empty_sample(self):
        self.page({})
        with self.assertRaisesRegex(ValueError, "Expected a list"):
            self.outputs()

    def test_incomplete_issue_cannot_be_skipped_or_replayed(self):
        self.page([{"number": 2}])
        self.page([])
        self.staging.filter_issue.return_value = True
        with self.assertRaisesRegex(ValueError, "Missing staging metadata"):
            self.outputs()
        self.details.assert_not_called()

    def test_malformed_hash_cannot_be_forwarded_to_actions(self):
        self.page([{"number": 2}])
        self.page([{}, {}, {}])
        self.staging.filter_issue.return_value = True
        self.staging.parse_info.return_value = ("149346", "aaa\nfound=true", "b" * 40)
        with self.assertRaisesRegex(ValueError, "Invalid staging metadata"):
            self.outputs()
        self.details.assert_not_called()
