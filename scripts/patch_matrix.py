"""Bind patch reporting IDs to matrix entries during hosted Patchwork discovery."""

import json
import re
from pathlib import Path


def patch_targets(names, metadata):
    targets = []
    for name in names:
        if not re.fullmatch(r"[0-9]+-\w*-[1-9][0-9]*", name):
            raise ValueError(f"Invalid patch artifact name: {name!r}")
        patch_id = (metadata / name).read_text().splitlines()[-1]
        if not re.fullmatch(r"[1-9][0-9]*", patch_id):
            raise ValueError(f"Invalid Patchwork ID in {name}")
        targets.append({"patch_name": name, "patch_id": patch_id})
    return targets


if __name__ == "__main__":
    names = json.loads(Path("artifact_names.txt").read_text())
    print(json.dumps(patch_targets(names, Path("patchworks_metadata"))))
