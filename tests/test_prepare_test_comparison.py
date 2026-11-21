"""Check the mapping from raw Actions artifacts to the configured CI builds."""

import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_test_comparison as prep


def archive(files):
    data = io.BytesIO()
    with ZipFile(data, "w") as bundle:
        for name, contents in files.items():
            bundle.writestr(name, contents)
    return data.getvalue()


class ComparisonInputs(unittest.TestCase):
    def test_matrix_covers_configured_patch_boards(self):
        matrix = json.loads(prep.MATRIX.read_text())
        groups = prep.artifact_groups(matrix, "candidate", "a" * 40)
        self.assertEqual(len(groups), 3)
        self.assertEqual(sum(map(len, groups.values())), len(matrix))
        self.assertEqual(len(prep.artifact_groups(matrix, "baseline", "a" * 40)), 7)
        for name, targets in groups.items():
            _, mode, arch, abi, *_ = name.split("-")
            script = arch + "-" + abi
            if mode == "newlib":
                script += "-reduced-multilib-set"
            configure = (ROOT / "configure-scripts" / script).read_text()
            specs = re.search(r'--with-multilib-generator="([^"]+)"', configure)[1]
            expected = {
                tuple(spec.removesuffix("--").split("-")) for spec in specs.split(";")
            }
            self.assertEqual(targets, expected)

    def test_collects_only_sums_from_the_selected_producer(self):
        name = "gcc-linux-rv64gcv-lp64d-" + "a" * 40 + "-multilib-sum-files"
        for run in [None, 42]:
            prefix = "sum_files/" if run is None else ""
            sums = archive({prefix + "gcc.sum": b"PASS: test\n", "../unexpected": b"x"})
            outer = archive({name + ".zip": sums}) if run is None else sums
            record = dict(
                id=2,
                workflow_run=dict(id=42),
                name=name,
                expired=False,
                size_in_bytes=len(outer),
            )
            with self.subTest(run=run), tempfile.TemporaryDirectory() as tmp:
                storage = Path(tmp)

                def api(args, output=None):
                    if output is None:
                        scope = "/runs/42/artifacts?" if run else "/actions/artifacts?"
                        self.assertIn(scope, args[0])
                        return json.dumps(record).encode()
                    self.assertTrue(args[0].endswith("/artifacts/2/zip"))
                    output.write(outer)

                with patch.object(prep, "gh_api", side_effect=api):
                    prep.collect(name, "rise/repo", run, storage)
                dest = storage / name
                self.assertEqual(
                    sorted(p.name for p in dest.iterdir()), ["gcc.sum", "source.json"]
                )
                self.assertEqual(
                    json.loads((dest / "source.json").read_text()),
                    dict(repository="rise/repo", id=2, run_id=42),
                )
                self.assertFalse((storage / "unexpected").exists())

    def test_expired_ambiguous_and_unreadable_sources_are_not_promoted(self):
        name = "test-sum-files"
        record = dict(
            id=2, workflow_run=dict(id=42), name=name, expired=False, size_in_bytes=1
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            for rows in ([dict(record, expired=True)], [record, record], []):
                with self.subTest(rows=rows), patch.object(
                    prep,
                    "gh_api",
                    return_value=b"\n".join(json.dumps(r).encode() for r in rows),
                ):
                    with self.assertRaises(ValueError):
                        prep.collect(name, "rise/repo", None, dest)
                    self.assertEqual(list(dest.iterdir()), [])

            def missing(args, output=None):
                if output is None:
                    return json.dumps(record).encode()
                output.write(archive({"ignored.zip": b"other run"}))

            with patch.object(prep, "gh_api", side_effect=missing), self.assertRaises(
                ValueError
            ):
                prep.collect(name, "rise/repo", None, dest)
            self.assertEqual(list(dest.iterdir()), [])


@unittest.skipUnless(
    (ROOT / "riscv-gnu-toolchain/scripts").is_dir(),
    "initialize the toolchain submodule to run the integration test",
)
class PinnedToolchainComparison(unittest.TestCase):
    def test_first_failure_on_clean_board_survives_partial_matrix(self):
        import subprocess
        from precommit_result import render

        matrix = json.loads(prep.MATRIX.read_text())
        row = next(
            r for r in matrix if r["mode"] == "newlib" and r["arch"] == "rv64gcv"
        )
        baseline = "a" * 40
        candidate = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").symlink_to(
                ROOT / "riscv-gnu-toolchain/scripts", target_is_directory=True
            )
            (root / "summaries").mkdir()

            def run_script(script, *args):
                subprocess.run(
                    [sys.executable, "-B", script, *args],
                    cwd=root,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )

            for role, revision in (("baseline", baseline), ("candidate", candidate)):
                source = root / f"{role}-sums" / row[role].format(sha=revision)
                source.mkdir(parents=True)
                for language in ("gcc", "g++"):
                    failed = language == "gcc" and role == "candidate"
                    (source / f"{language}.sum").write_text(
                        "Target is riscv64-unknown-elf\n"
                        "Running target riscv-sim/-march=rv64gcv/-mabi=lp64d/-mcmodel=medlow\n"
                        "Running /build/gcc/testsuite/gcc.dg/dg.exp ...\n"
                        "PASS: gcc.dg/control.c -O2 execution test\n"
                        f"{'FAIL' if failed else 'PASS'}: gcc.dg/first.c -O2 execution test\n"
                        f"\t\t=== {language} Summary ===\n"
                        f"# of expected passes\t{1 if failed else 2}\n"
                        f"# of unexpected failures\t{int(failed)}\n"
                    )
                run_script(prep.__file__, "filter", role, revision)

            run_script(
                "scripts/compare_all_artifacts.py",
                "-hash",
                candidate,
                "--baseline-hash",
                baseline,
            )
            run_script(
                "scripts/precommit_evidence.py",
                "--baseline",
                baseline,
                "--candidate",
                candidate,
                "--matrix",
                prep.MATRIX,
                "--baseline-sums",
                "baseline-sums",
                "--candidate-sums",
                "candidate-sums",
            )
            run_script(
                "scripts/aggregate.py", "-chash", candidate, "-o", "temp_comment.md"
            )
            self.assertEqual(
                json.loads((root / "evidence/verdict.json").read_text()),
                {"complete": False, "confirmed": 1, "unverified": 0},
            )
            state, _, labels = render(root, True, True, True)
            self.assertEqual(state, "fail")
            self.assertIn("new-regressions", labels.split(","))
            self.assertIn(
                "gcc.dg/first.c -O2 execution test", (root / "comment.md").read_text()
            )
