import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/precommit-runner"))
import replenish as jit


CONFIG = base64.b64encode(b"fixture single-use configuration").decode()


class AppTokenTests(unittest.TestCase):
    def test_exchange_scopes_new_tokens_to_precommit_administration(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app-key.pem").write_text("fixture")
            with (
                patch.dict(
                    jit.os.environ, CREDENTIALS_DIRECTORY=directory, GITHUB_APP_ID="1"
                ),
                patch.object(jit.jwt, "encode", return_value="fixture-assertion"),
                patch.object(
                    jit,
                    "api",
                    side_effect=[{"id": 5}, {"token": "fixture-installation"}],
                ) as api,
            ):
                self.assertEqual(jit.installation_token(), "fixture-installation")
            self.assertEqual(
                api.call_args_list[0].args,
                ("GET", jit.REPO + "/installation", "fixture-assertion"),
            )
            self.assertEqual(
                api.call_args_list[-1].args,
                (
                    "POST",
                    "/app/installations/5/access_tokens",
                    "fixture-assertion",
                    {
                        "repositories": ["gcc-precommit-ci"],
                        "permissions": {"administration": "write"},
                    },
                ),
            )


class ReplenishmentTests(unittest.TestCase):
    def setUp(self):
        self.token = self.enterContext(patch.object(jit, "installation_token"))
        self.api = self.enterContext(patch.object(jit, "api"))
        self.command = self.enterContext(patch.object(jit, "command"))

    def test_active_worker_never_renews_or_modifies_a_registration(self):
        self.command.return_value = "LoadState=loaded\nActiveState=active"
        jit.replenish(False)
        self.assertEqual(self.command.call_count, 1)
        self.token.assert_not_called()
        self.api.assert_not_called()

    def test_live_container_is_not_replaced_on_service_state_mismatch(self):
        self.command.side_effect = [
            "LoadState=loaded\nActiveState=failed",
            f"{jit.CONTAINER} running",
        ]
        jit.replenish(False)
        self.token.assert_not_called()
        self.assertEqual(self.command.call_count, 2)

    def test_reboot_cleanup_only_removes_the_stopped_precommit_container(self):
        self.command.side_effect = [
            "LoadState=loaded\nActiveState=inactive",
            f"unrelated running\n{jit.CONTAINER} exited",
            "",
        ]
        self.assertTrue(jit.worker_idle())
        self.command.assert_called_with("podman", "rm", jit.CONTAINER)

    def test_uncertain_registration_never_allows_deletion(self):
        for status, busy in (("online", False), ("offline", True), ("unknown", False)):
            with self.subTest(status=status, busy=busy):
                self.api.reset_mock()
                self.api.return_value = {
                    "runners": [
                        {
                            "name": jit.NAME,
                            "status": status,
                            "busy": busy,
                            "labels": [{"name": jit.NAME}],
                        }
                    ]
                }
                self.assertIsNone(jit.next_config())
                self.assertEqual(self.api.call_count, 1)

    def test_foreign_registration_is_retained(self):
        self.api.return_value = {
            "runners": [{"name": jit.NAME, "labels": [{"name": "other"}]}]
        }
        with self.assertRaises(RuntimeError):
            jit.next_config()
        self.assertEqual(self.api.call_count, 1)

    def test_lost_jit_is_replaced_by_one_dedicated_listener(self):
        stale = {
            "name": jit.NAME,
            "labels": [{"name": jit.NAME}],
            "id": 21,
            "status": "offline",
            "busy": False,
        }
        self.api.side_effect = [
            {"runners": [stale]},
            None,
            {"encoded_jit_config": CONFIG},
        ]
        self.assertEqual(jit.next_config(), CONFIG)
        delete, create = self.api.call_args_list[1:]
        self.assertEqual(delete.args[:2], ("DELETE", f"{jit.RUNNERS}/21"))
        self.assertEqual(create.args[:2], ("POST", jit.RUNNERS + "/generate-jitconfig"))
        self.assertEqual(
            create.args[3]["labels"], ["self-hosted", "Linux", "X64", jit.NAME]
        )

    def test_registration_api_failure_prevents_changes_to_local_runner(self):
        self.command.side_effect = ["LoadState=loaded\nActiveState=inactive", ""]
        self.api.side_effect = RuntimeError("HTTP 503")
        with self.assertRaisesRegex(RuntimeError, "503"):
            jit.replenish(False)
        self.assertEqual(self.command.call_count, 2)

    def test_manual_rollout_uses_only_external_jit_on_stdin(self):
        self.command.side_effect = [
            "LoadState=loaded\nActiveState=inactive",
            "",
            "",
            "",
            "",
        ]
        with patch.object(sys, "stdin", io.StringIO(CONFIG + "\n")):
            jit.replenish(True)
        self.token.assert_not_called()
        self.api.assert_not_called()
        secret, start = self.command.call_args_list[-2:]
        self.assertEqual(secret.args, ("podman", "secret", "create", jit.SECRET, "-"))
        self.assertEqual(secret.kwargs, {"stdin": CONFIG})
        self.assertEqual(start.args, ("systemctl", "--user", "start", jit.SERVICE))

    def test_empty_manual_input_leaves_current_secret_intact(self):
        self.command.side_effect = ["LoadState=loaded\nActiveState=inactive", ""]
        with patch.object(sys, "stdin", io.StringIO("")), self.assertRaises(ValueError):
            jit.replenish(True)
        self.assertEqual(self.command.call_count, 2)
