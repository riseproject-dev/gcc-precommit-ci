"""Select the historical patch for a staging pull request."""

from datetime import datetime
import os
from pathlib import Path
import re
import sys

import requests


def github_list(repo, token, path):
    response = requests.get(
        f"https://api.github.com/repos/{repo}/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
    )
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list):
        raise ValueError("Expected a list in the GitHub response")
    return items


def replay_outputs(repo, token, staging, patch_detail):
    # Match the shared staging selector's window and issue eligibility.
    issues = github_list(repo, token, "issues?state=closed&per_page=100")
    issue = next((issue for issue in issues if staging.filter_issue(issue)), None)
    if issue is None:
        print("Staging replay skipped: no eligible closed issue")
        return {"found": "false"}

    number = issue["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("Invalid historical issue number")
    comments = github_list(repo, token, f"issues/{number}/comments")
    if len(comments) < 3:
        raise ValueError(f"Missing staging metadata in issue #{number}")
    patch_id, baseline, trunk = staging.parse_info(issue, comments[1])
    if not re.fullmatch(r"[1-9][0-9]*", patch_id) or not all(
        re.fullmatch(r"[0-9a-fA-F]{4,40}", sha) for sha in (baseline, trunk)
    ):
        raise ValueError(f"Invalid staging metadata in issue #{number}")
    timestamp = datetime.fromisoformat(patch_detail(patch_id)["date"]).isoformat()
    return {
        "found": "true",
        "timestamp": timestamp,
        "baseline_hash": baseline,
        "tot_hash": trunk,
        "patch_id": patch_id,
        "issue_num": str(number),
    }


def main():
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "riscv-gnu-toolchain/scripts")
    )
    import get_staging_patch
    from riscv_patch_scope import patch_detail

    fields = replay_outputs(
        os.environ["PRECOMMIT_REPOSITORY"],
        os.environ["GH_TOKEN"],
        get_staging_patch,
        patch_detail,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in fields.items():
            print(f"{key}={value}", file=output)
    if fields["found"] == "false":
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            print("Staging replay skipped: no eligible closed issue", file=summary)


if __name__ == "__main__":
    main()
