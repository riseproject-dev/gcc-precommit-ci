"""Exercise recovery workflow shell steps and their GitHub job conditions."""

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from collect_testsuite_report import collect
from patch_matrix import patch_targets


WORKFLOWS = ROOT / ".github/workflows"
DOWNLOAD_ACTION = (
    ROOT / ".github/actions/common/download-comparison-artifacts/action.yaml"
)


def step_blocks(path):
    """Read named step blocks without depending on a YAML package on runners."""
    text = path.read_text()
    starts = list(re.finditer(r"^( +)- name: (.+)$", text, re.MULTILINE))
    for match in starts:
        indent = len(match[1])
        lines = text[match.end() :].splitlines()[1:]
        block = []
        for line in lines:
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line)
        yield match[2], "\n".join(block)


def named_step(path, name):
    return dict(step_blocks(path))[name]


def shell_body(block):
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def condition(block):
    return re.search(r"if: (\$\{\{.+\}\})", block)[1]


def evaluate(expression, values, workspace=None, cancelled=False):
    """Evaluate the small expression subset used by the recovery conditions."""
    expression = expression.removeprefix("${{").removesuffix("}}")
    for name, value in sorted(values.items(), key=lambda item: -len(item[0])):
        expression = expression.replace(name, repr(value))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)

    def hash_files(pattern):
        path = workspace / pattern
        return "file-hash" if path.is_file() else ""

    return eval(
        expression.strip(),
        {"__builtins__": {}},
        {
            "cancelled": lambda: cancelled,
            "contains": lambda value, member: member in value,
            "format": lambda template, *args: template.format(*args),
            "hashFiles": hash_files,
        },
    )


class WorkflowRecoveryTests(unittest.TestCase):
    def test_reporting_can_be_bound_to_a_reviewed_dispatch(self):
        sha = "1" * 40
        modes = (
            (sha, "Patchworks", "workflow_dispatch", sha, "true"),
            (sha, "Patchworks", "workflow_dispatch", "2" * 40, "false"),
            (sha, "Patchworks", "schedule", sha, "false"),
            (sha, "Staging", "workflow_dispatch", sha, "false"),
            ("true", "Patchworks", "schedule", sha, "true"),
            ("false", "Patchworks", "workflow_dispatch", sha, "false"),
        )
        for name in (
            "run-checks",
            "lint",
            "test-regression",
            "generate-precommit-summary",
        ):
            workflow = (WORKFLOWS / (name + ".yaml")).read_text()
            match = re.search(
                r"  PATCHWORK_REPORTING_ENABLED: >-\n((?:    .+\n)+)", workflow
            )
            self.assertIsNotNone(match)
            expr = " ".join(match[1].split())
            for mode, source, event, commit, expected in modes:
                with self.subTest(workflow=name, event=event, mode=mode):
                    result = evaluate(
                        expr,
                        {
                            "vars.PATCHWORK_REPORTING_ENABLED": mode,
                            "github.workflow": source,
                            "github.event_name": event,
                            "github.sha": commit,
                        },
                    )
                    self.assertEqual(result, expected)

    def test_summary_requires_original_run_context(self):
        text = (WORKFLOWS / "generate-precommit-summary.yaml").read_text()
        trigger = text.split("\non:\n", 1)[1].split("\nenv:\n", 1)[0]
        self.assertEqual(
            re.findall(r"^  ([a-z_]+):$", trigger, re.MULTILINE),
            ["workflow_call"],
        )
        caller = (
            (WORKFLOWS / "run-checks.yaml")
            .read_text()
            .split("\n  summarize:\n", 1)[1]
            .split("\n  link-staging-issue:\n", 1)[0]
        )
        for name in ("issue_num", "build_comment_id", "test_comment_id"):
            with self.subTest(input=name):
                self.assertRegex(
                    trigger,
                    rf"(?m)^      {name}:\n        required: true\n        type: string$",
                )
                self.assertRegex(
                    caller,
                    rf"(?m)^      {name}: "
                    + re.escape("${{ needs.")
                    + r".+\.outputs\..+ "
                    + re.escape("}}")
                    + "$",
                )
        self.assertIn("patch_id: ${{ inputs.patch_id }}", caller)
        self.assertIn(' -pid "$PATCH_ID"', text)
        self.assertNotIn("debug-url.md", text)

    def test_manual_override_consumes_extracted_metadata(self):
        path = WORKFLOWS / "call-patchworks-api.yaml"
        download = shell_body(named_step(path, "Download patch"))
        patch_id = shell_body(named_step(path, "Get patch id"))
        self.assertNotIn("Extract patch", dict(step_blocks(path)))
        # The shared downloader extracts the archive into -outdir; it does not
        # leave the downloaded ZIP behind. Model that contract without HTTP.
        stub = """
            python() {
              while [ "$1" != '-outdir' ]; do shift; done
              mkdir -p "$2"
              printf 'Applied patches: 1 -> 1\\n12345\\n' > "$2/fixture-patch"
            }
        """
        script = textwrap.dedent(stub) + download + "\n" + patch_id
        script = re.sub(
            r"\$\{\{\s*steps.patch-name.outputs.patch_name\s*\}\}",
            "fixture-patch",
            script,
        )
        script = script.replace("${{ secrets.GITHUB_TOKEN }}", "fixture-token")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("patch_id=12345", output.read_text())

    def test_final_patchwork_state_comes_from_published_verdict(self):
        workflow = WORKFLOWS / "generate-precommit-summary.yaml"
        block = named_step(workflow, "Publish testsuite verdict")
        self.assertIn("STATE: ${{ steps.verdict.outputs.state }}", block)
        self.assertIn('-state "$STATE"', block)
        expr = condition(block)
        values = {
            "github.workflow": "Patchworks",
            "steps.test-report.outcome": "success",
            "steps.verdict.outcome": "success",
        }
        self.assertTrue(evaluate(expr, values))
        for key, value in (
            ("github.workflow", "Staging"),
            ("steps.test-report.outcome", "failure"),
            ("steps.verdict.outcome", "skipped"),
        ):
            self.assertFalse(evaluate(expr, {**values, key: value}))

    def test_summary_uses_comparison_job_artifact_ids(self):
        workflow = WORKFLOWS / "generate-precommit-summary.yaml"
        for name, key in (
            ("Download summaries artifact", "summaries"),
            ("Download current logs artifact", "current_logs"),
            ("Download verified comparison", "evidence"),
        ):
            output = f"needs.compare-artifacts.outputs.{key}_artifact_id"
            block = named_step(workflow, name)
            self.assertIn("artifact-ids: ${{ " + output + " }}", block)
            self.assertIn(f"if: {output} != ''", block)
            self.assertIn("merge-multiple: true", block)

    def test_empty_patch_selection_skips_build_setup(self):
        for workflow in ("patchworks.yaml", "downtime-runner.yaml"):
            with self.subTest(workflow=workflow):
                text = (WORKFLOWS / workflow).read_text()
                setup = text.split("\n  init-submodules:\n", 1)[1]
                expression = condition(setup)
                output = "needs.fetch_patches.outputs.patch_targets"
                self.assertFalse(evaluate(expression, {output: "[]"}))
                self.assertTrue(evaluate(expression, {output: '["one-patch"]'}))
                matrix = text.split("\n  patch_matrix:\n", 1)[1]
                self.assertIn("init-submodules", re.search(r"needs: (.+)", matrix)[1])

    def test_only_selected_run_reports_enter_comparison(self):
        name = "gcc-linux-rv64gcv-lp64d-" + "a" * 40 + "-multilib-report.log"
        step = named_step(DOWNLOAD_ACTION, "Collect the expected report from this run")
        self.assertIn("actions/runs/$GITHUB_RUN_ID/artifacts", step)
        self.assertIn("failed_testsuite.txt", step)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            archive = workspace / "report.zip"
            output = workspace / "current_logs"
            output.mkdir()
            text = b"\t\t=== gcc: Unexpected fails for rv64gcv lp64d medlow  ===\n\nSummary\n"
            with ZipFile(archive, "w") as zipped:
                zipped.writestr(name, text)
                zipped.writestr("extra.log", "Unrelated file")
            collect(archive, name, output)
            self.assertEqual([p.name for p in output.iterdir()], [name])
            self.assertEqual((output / name).read_bytes(), text)
            with ZipFile(archive, "w") as zipped:
                zipped.writestr(name, b"incomplete without the summary separator")
            with self.assertRaises(ValueError):
                collect(archive, name, output)
            self.assertFalse((output / name).with_suffix(".partial").exists())

    def test_discovery_preserves_patch_ids_for_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            name = "69792-v2_RISCV_changes-1"
            path = metadata / name
            path.write_text("Patch and series metadata\n149346")
            self.assertEqual(patch_targets([], metadata), [])
            self.assertEqual(
                patch_targets([name], metadata),
                [{"patch_name": name, "patch_id": "149346"}],
            )
            path.write_text("Patch and series metadata\ninvalid")
            with self.assertRaises(ValueError):
                patch_targets([name], metadata)

    def test_publishing_errors_are_not_ignored(self):
        paths = list(WORKFLOWS.glob("*.yaml")) + list(
            (ROOT / ".github/actions").rglob("action.yaml")
        )
        publishing_files = set()
        for path in paths:
            for name, block in step_blocks(path):
                if "scripts/post_check_to_patchworks.py" not in block:
                    continue
                publishing_files.add(path.relative_to(ROOT).as_posix())
                with self.subTest(path=path.name, step=name):
                    self.assertNotIn("continue-on-error: true", block)
        self.assertEqual(
            publishing_files,
            {
                ".github/actions/report-build-complete-status/action.yaml",
                ".github/workflows/call-patchworks-api.yaml",
                ".github/workflows/generate-precommit-summary.yaml",
                ".github/workflows/lint.yaml",
                ".github/workflows/run-checks.yaml",
                ".github/workflows/test-regression.yaml",
            },
        )

    def test_recovery_checkpoint_advances_only_completed_windows(self):
        text = (WORKFLOWS / "downtime-runner.yaml").read_text()
        block = text.split("\n  checkpoint:\n", 1)[1]
        expression = condition(block)
        base = {
            "needs.check_last_runner.result": "success",
            "needs.fetch_patches.result": "success",
            "needs.fetch_patches.outputs.patch_targets": "['fixture']",
            "needs.init-submodules.result": "success",
            "needs.patch_matrix.result": "success",
        }
        self.assertTrue(evaluate(expression, base))
        empty = dict(base)
        empty.update(
            {
                "needs.fetch_patches.outputs.patch_targets": "[]",
                "needs.init-submodules.result": "skipped",
                "needs.patch_matrix.result": "skipped",
            }
        )
        self.assertTrue(evaluate(expression, empty))
        for job in (
            "check_last_runner",
            "fetch_patches",
            "init-submodules",
            "patch_matrix",
        ):
            for result in ("failure", "cancelled", "skipped"):
                with self.subTest(job=job, result=result):
                    failed = dict(base, **{f"needs.{job}.result": result})
                    self.assertFalse(evaluate(expression, failed))
        self.assertFalse(evaluate(expression, base, cancelled=True))


if __name__ == "__main__":
    unittest.main()
