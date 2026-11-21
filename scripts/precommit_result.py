"""Render the pre-commit issue and Patchwork verdict from the comparison job."""

import json
import os
from pathlib import Path


def read_evidence(directory):
    result = json.loads((directory / "verdict.json").read_text())
    if not isinstance(result, dict) or type(result.get("complete")) is not bool:
        raise ValueError("missing coverage decision")
    for key in ("confirmed", "unverified"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise ValueError("invalid result count")
    details = (directory / "evidence.md").read_text().strip()
    if not details:
        raise ValueError("missing comparison details")
    return result, details


def render(root, comparison_ok, aggregate_ok, evidence_ok):
    labels = set()
    details = []
    incomplete = not comparison_ok
    if not comparison_ok:
        details.append("The comparison job did not finish successfully.")

    legacy = ""
    if aggregate_ok:
        try:
            _, separator, legacy = (
                (root / "temp_comment.md").read_text().partition("\n---\n")
            )
            if not separator or not legacy.strip():
                raise ValueError("empty aggregate report")
            labels = set((root / "labels.txt").read_text().strip().split(",")) & {
                "new-regressions",
                "resolved-regressions",
                "build-failure",
                "testsuite-failure",
                "invalid",
            }
        except (OSError, ValueError):
            aggregate_ok = False
            legacy = ""
    if not aggregate_ok:
        incomplete = True
        details.append("The filtered comparison report is unavailable.")
    if labels & {"build-failure", "testsuite-failure", "invalid"}:
        incomplete = True

    confirmed = unverified = 0
    if evidence_ok:
        try:
            verdict, evidence = read_evidence(root / "evidence")
            confirmed, unverified = verdict["confirmed"], verdict["unverified"]
            incomplete |= not verdict["complete"]
            details.append(evidence)
        except (OSError, ValueError):
            evidence_ok = False
    if not evidence_ok:
        incomplete = True
        details.append("The verified comparison report is unavailable.")

    if "new-regressions" in labels:
        labels.remove("new-regressions")
        if evidence_ok and not (confirmed or unverified):
            incomplete = True
            details.append(
                "The filtered report lists new observations that could not be "
                "matched to the raw comparison."
            )
    if incomplete:
        labels.add("invalid")
    if confirmed:
        labels.add("new-regressions")
        state = "fail"
        transition = "transition" if confirmed == 1 else "transitions"
        description = f"{confirmed} verified PASS to FAIL {transition}"
        if incomplete:
            description += " with incomplete coverage"
    elif incomplete:
        state = "warning"
        description = "Testsuite comparison incomplete"
        if "build-failure" in labels:
            description += " after a build failure"
    elif unverified:
        state = "warning"
        description = "Newly observed test results need review"
    else:
        state = "success"
        description = "Complete testsuite comparison found no new regressions"

    if legacy:
        # The shared aggregate includes cases with no comparable prior outcome.
        legacy = legacy.replace("|New Failures|gcc|", "|Newly observed results|gcc|")
        legacy = legacy.replace(
            "\n## New Failures Across All Affected Targets",
            "\n## Newly observed results across affected targets",
        ).replace(
            "\n## Architecture Specific New Failures",
            "\n## Newly observed results by architecture",
        )
        details.append(legacy.strip())
    (root / "comment.md").write_text(
        "\n\n".join(["## Testsuite Status", f"**{description}.**", *details]) + "\n"
    )
    return state, description, ",".join(sorted(labels))


if __name__ == "__main__":
    state, description, labels = render(
        Path.cwd(),
        os.environ["COMPARISON_JOB"] == "success",
        os.environ["AGGREGATE_STEP"] == "success",
        os.environ["EVIDENCE_STEP"] == "success",
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print(
            f"state={state}",
            f"description={description}",
            f"labels={labels}",
            sep="\n",
            file=output,
        )
