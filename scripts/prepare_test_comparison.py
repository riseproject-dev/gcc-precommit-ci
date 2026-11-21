"""Collect the raw CI evidence and filter both sides using the pre-commit allowlist."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from time import sleep
from urllib.parse import urlencode
from zipfile import BadZipFile, ZipFile
import zlib


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "configure-scripts/precommit-matrix.json"
TOOLS = ("gcc", "g++", "gfortran")
MAX_ARCHIVE = 256 * 1024**2
MAX_SUM = 512 * 1024**2


def artifact_groups(matrix, role, revision):
    groups = defaultdict(set)
    for row in matrix:
        name = row[role].format(sha=revision)
        groups[name].add((row["arch"], row["abi"]))
    return groups


def extract_sums(bundle, directory, prefix):
    """Copy only the expected regular .sum names, regardless of other ZIP members."""
    found = False
    for tool in TOOLS:
        name = f"{tool}.sum"
        members = [item for item in bundle.infolist() if item.filename == prefix + name]
        if not members:
            continue
        if len(members) != 1 or members[0].file_size > MAX_SUM:
            raise ValueError(f"Ambiguous or oversized {name}")
        with bundle.open(members[0]) as source, (directory / name).open("wb") as output:
            shutil.copyfileobj(source, output)
        found = True
    if not found:
        raise ValueError("The archive contains no GCC language summaries")


def unpack_archive(path, directory, name, baseline):
    with ZipFile(path) as outer:
        if not baseline:
            extract_sums(outer, directory, "")
            return
        # Post-commit uploads a named ZIP, pre-commit uploads the .sum files directly.
        inner_name = name + ".zip"
        members = [item for item in outer.infolist() if item.filename == inner_name]
        if len(members) != 1 or members[0].file_size > MAX_ARCHIVE:
            raise ValueError("Missing, ambiguous or oversized post-commit archive")
        with TemporaryFile() as inner:
            with outer.open(members[0]) as source:
                shutil.copyfileobj(source, inner)
            with ZipFile(inner) as bundle:
                extract_sums(bundle, directory, "sum_files/")


def gh_api(args, output=subprocess.PIPE):
    for attempt in range(2):
        try:
            return subprocess.run(
                ["gh", "api", *args],
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
                timeout=300,
            ).stdout
        except (OSError, subprocess.SubprocessError) as err:
            # gh diagnostics can include a signed download URL.
            transient = isinstance(err, subprocess.TimeoutExpired) or re.search(
                rb"timeout|timed out|connection reset|unexpected EOF|HTTP 5\d\d",
                getattr(err, "stderr", None) or b"",
                re.IGNORECASE,
            )
            if attempt or not transient:
                raise ValueError("GitHub Actions artifact request failed") from err
            if output != subprocess.PIPE:
                output.seek(0)
                output.truncate()
            sleep(1)


def collect(name, repo, run_id, storage):
    endpoint = f"repos/{repo}/actions"
    query = urlencode({"name": name, "per_page": 100})
    scope = f"/runs/{run_id}" if run_id else ""
    lines = gh_api(
        [
            f"{endpoint}{scope}/artifacts?{query}",
            "--paginate",
            "--jq",
            ".artifacts[] | @json",
        ]
    )
    records = [json.loads(line) for line in lines.splitlines()]
    matches = [
        row for row in records if row["name"] == name and row["expired"] is False
    ]
    if len(matches) != 1:
        raise ValueError("Expected one unexpired artifact matching this build")
    record = matches[0]
    origin = run_id or record["workflow_run"]["id"]
    if not (0 < record["size_in_bytes"] <= MAX_ARCHIVE):
        raise ValueError("Empty or oversized Actions artifact")
    with TemporaryDirectory(dir=storage) as tmp:
        tmp = Path(tmp)
        with (tmp / "archive.zip").open("wb") as output:
            gh_api([f"{endpoint}/artifacts/{record['id']}/zip"], output)
        extracted = tmp / "result"
        extracted.mkdir()
        unpack_archive(tmp / "archive.zip", extracted, name, baseline=run_id is None)
        (extracted / "source.json").write_text(
            json.dumps({"id": record["id"], "run_id": int(origin), "repository": repo})
        )
        extracted.rename(storage / name)


def prepare_reports(groups, storage, destination, revision, toolchain):
    # Import the splitter from the toolchain gitlink used by this workflow.
    sys.path.insert(0, str(toolchain / "scripts"))
    from separate_multilib_results import read_report, split_report_name

    for name, targets in groups.items():
        directory = storage / name
        sums = [
            directory / f"{tool}.sum"
            for tool in TOOLS
            if (directory / f"{tool}.sum").is_file()
        ]
        if not sums:
            continue  # The evidence check requires every target in MATRIX.
        mode = name.split("-")[1]
        with TemporaryDirectory() as temp:
            filtered = Path(temp) / "report.log"
            with filtered.open("wb") as output:
                rc = subprocess.run(
                    [
                        sys.executable,
                        toolchain / "scripts/testsuite-filter",
                        "gcc",
                        "glibc" if mode == "linux" else mode,
                        ROOT / "test/allowlist",
                        ",".join(map(str, sums)),
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            # testsuite-filter exits 1 both for unexpected results and for a crash.
            try:
                if rc.returncode not in (0, 1) or rc.stderr:
                    raise ValueError("testsuite-filter did not complete")
                for board, report in read_report(filtered).items():
                    if (board.arch, board.abi) in targets:
                        output = destination / split_report_name(mode, revision, board)
                        with output.open("x") as f:
                            f.write(report.text)
            except (OSError, ValueError) as err:
                record_problem(
                    name,
                    f"Could not prepare {storage.name} report ({err})",
                )


def record_problem(name, reason):
    # The aggregate reads one artifact|message record per line.
    reason = " ".join(reason.replace("|", "/").splitlines())[:500]
    with Path("current_logs/failed_testsuite.txt").open("a") as problems:
        problems.write(f"{name}|{reason}\n")
    print(f"{name}: {reason}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["collect", "filter"])
    parser.add_argument("role", choices=["baseline", "candidate"])
    parser.add_argument("revision")
    parser.add_argument("--repo")
    parser.add_argument("--run-id", type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("expected a full GCC SHA")
    if args.operation == "collect" and (
        not args.repo or (args.role == "candidate") != bool(args.run_id)
    ):
        parser.error(
            "specify a repository for each collection and a workflow run ID for the candidate"
        )
    groups = artifact_groups(json.loads(MATRIX.read_text()), args.role, args.revision)
    storage = Path(f"{args.role}-sums")
    storage.mkdir(exist_ok=True)
    Path("current_logs").mkdir(exist_ok=True)
    if args.operation == "filter":
        destination = Path(
            "previous_logs" if args.role == "baseline" else "current_logs"
        )
        destination.mkdir(exist_ok=True)
        prepare_reports(groups, storage, destination, args.revision, Path.cwd())
        return
    if any(storage.iterdir()):
        parser.error(f"{storage} contains results from an earlier collection")
    for name in groups:
        try:
            collect(name, args.repo, args.run_id, storage)
        except (
            ValueError,
            OSError,
            KeyError,
            TypeError,
            BadZipFile,
            RuntimeError,
            EOFError,
            zlib.error,
        ) as err:
            reason = str(err) if type(err) is ValueError else type(err).__name__
            record_problem(name, f"{args.role} artifact unavailable ({reason})")


if __name__ == "__main__":
    main()
