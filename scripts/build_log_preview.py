"""Read small text previews from a build job's log artifact without extracting it."""

import argparse
import os
import re
import tempfile
import zipfile
from pathlib import Path

import requests


PREVIEW_BYTES = 1_048_000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024 * 1024


def write_previews(archive_file, log_name, output):
    if not re.fullmatch(r"[0-9]+-[a-zA-Z0-9_.-]+-build-log", log_name):
        raise ValueError("Invalid build log name")
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_file) as archive:
        for member, dest in [
            (log_name + ".log", "stdout.txt"),
            (log_name + "-stderr.log", "stderr.txt"),
        ]:
            info = archive.getinfo(member)
            if info.file_size > MAX_LOG_BYTES:
                raise ValueError("Log exceeds preview size limit")
            with archive.open(info) as stream:
                stream.seek(max(0, info.file_size - PREVIEW_BYTES))
                tail = stream.read(PREVIEW_BYTES)
            # The two fixed files are text inputs to the gist API. Archive paths,
            # modes, links and other entries never materialize on the reporter.
            (output / dest).write_text(tail.decode("utf-8", errors="replace"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_name")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    api = (
        os.environ["GITHUB_API_URL"]
        + "/repos/"
        + os.environ["GITHUB_REPOSITORY"]
        + "/actions"
    )
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"]}
    response = requests.get(
        f"{api}/runs/{os.environ['GITHUB_RUN_ID']}/artifacts",
        params={"name": args.log_name},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    matching = [
        a
        for a in response.json()["artifacts"]
        if a["name"] == args.log_name and not a["expired"]
    ]
    if len(matching) != 1 or matching[0]["size_in_bytes"] > MAX_ARCHIVE_BYTES:
        raise ValueError(
            "No single current-run log artifact within the preview size limit"
        )
    with requests.get(
        f"{api}/artifacts/{int(matching[0]['id'])}/zip",
        headers=headers,
        stream=True,
        timeout=60,
    ) as download, tempfile.TemporaryFile() as tmp:
        download.raise_for_status()
        for block in download.iter_content(256 * 1024):
            if tmp.tell() + len(block) > MAX_ARCHIVE_BYTES:
                raise ValueError("Downloaded archive exceeds preview size limit")
            tmp.write(block)
        tmp.seek(0)
        write_previews(tmp, args.log_name, args.output)


if __name__ == "__main__":
    main()
