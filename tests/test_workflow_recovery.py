"""Exercise recovery workflow shell steps and their GitHub job conditions."""

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
        patches = [
            block
            for name, block in step_blocks(
                WORKFLOWS / "generate-precommit-summary.yaml"
            )
            if name == "Download patches artifact"
        ]
        self.assertEqual(len(patches), 2)
        for block in patches:
            self.assertIn("uses: actions/download-artifact@v4", block)
            self.assertNotIn("if:", block)

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

    def test_invalid_summary_cannot_report_success(self):
        block = named_step(
            WORKFLOWS / "generate-precommit-summary.yaml", "Report Failure"
        )
        expression = condition(block)
        for labels in ("invalid", "resolved-regressions,invalid", "testsuite-failure"):
            with self.subTest(labels=labels):
                self.assertTrue(
                    evaluate(
                        expression,
                        {
                            "github.workflow": "Patchworks",
                            "steps.issue-labels.outputs.issue_labels": labels,
                        },
                    )
                )
        self.assertFalse(
            evaluate(
                expression,
                {
                    "github.workflow": "Patchworks",
                    "steps.issue-labels.outputs.issue_labels": "resolved-regressions",
                },
            )
        )

    def test_available_report_skips_fallbacks_and_failure_marker(self):
        report = "gcc-linux-rv64gcv-lp64d-fixture-multilib-report.log"
        values = {
            "inputs.report-artifact-name": report,
            "steps.same-workflow-binary.outcome": "failure",
        }
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "riscv-gnu-toolchain/current_logs" / report
            path.parent.mkdir(parents=True)
            path.write_text("Testsuite report\n")
            for name in (
                "Download report from another workflow",
                "Download rv32 non-multilib binary",
                "Download binary from another workflow",
                "Record missing testsuite report",
            ):
                with self.subTest(step=name):
                    self.assertFalse(
                        evaluate(
                            condition(named_step(DOWNLOAD_ACTION, name)),
                            values,
                            workspace,
                        )
                    )
            path.unlink()
            block = named_step(DOWNLOAD_ACTION, "Record missing testsuite report")
            self.assertTrue(evaluate(condition(block), values, workspace))
            subprocess.run(
                ["bash", "-e", "-c", shell_body(block)],
                cwd=directory,
                env=dict(os.environ, REPORT_ARTIFACT_NAME=report),
                check=True,
            )
            marker = path.parent / "failed_testsuite.txt"
            self.assertIn(report + "|Testsuite report artifact", marker.read_text())

    def test_publishing_errors_are_not_ignored(self):
        paths = list(WORKFLOWS.glob("*.yaml")) + list(
            (ROOT / ".github/actions").rglob("action.yaml")
        )
        publishing_steps = 0
        for path in paths:
            for name, block in step_blocks(path):
                if "scripts/post_check_to_patchworks.py" not in block:
                    continue
                publishing_steps += 1
                with self.subTest(path=path.name, step=name):
                    self.assertNotIn("continue-on-error: true", block)
        self.assertGreater(publishing_steps, 15)

    def test_recovery_checkpoint_advances_only_completed_windows(self):
        text = (WORKFLOWS / "downtime-runner.yaml").read_text()
        block = text.split("\n  checkpoint:\n", 1)[1]
        expression = condition(block)
        base = {
            "needs.check_last_runner.result": "success",
            "needs.fetch_patches.result": "success",
            "needs.fetch_patches.outputs.list_of_patch_names": "['fixture']",
            "needs.init-submodules.result": "success",
            "needs.patch_matrix.result": "success",
        }
        self.assertTrue(evaluate(expression, base))
        empty = dict(base)
        empty.update(
            {
                "needs.fetch_patches.outputs.list_of_patch_names": "[]",
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
