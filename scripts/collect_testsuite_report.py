"""Read the expected report as data without extracting the build worker's archive."""

import re
import sys
from pathlib import Path
from zipfile import ZipFile


MAX_REPORT_BYTES = 256 * 1024 * 1024


def collect(archive_path, report_name, output_dir):
    if not re.fullmatch(
        r"gcc-(?:linux|newlib)-[a-z0-9_]+-[a-z0-9]+-[0-9a-f]{40}-multilib-report\.log",
        report_name,
    ):
        raise ValueError("Invalid report name")
    report_path = output_dir / report_name
    temporary = report_path.with_suffix(".partial")
    try:
        with ZipFile(archive_path) as archive:
            if archive.namelist().count(report_name) != 1:
                raise ValueError("Expected exactly one matching report")
            if archive.getinfo(report_name).file_size > MAX_REPORT_BYTES:
                raise ValueError("Report exceeds the comparison size limit")
            header = True
            with archive.open(report_name) as source, temporary.open("wb") as dest:
                for line in source:
                    if header:
                        # The multilib splitter uses these target names in filenames.
                        if (
                            b"===" in line
                            and b"Unexpected fails for" in line
                            and b"/" in line
                        ):
                            raise ValueError("Invalid multilib target in report")
                        if line in (b"\n", b"\r\n"):
                            header = False
                    dest.write(line)
                if header:
                    raise ValueError("Incomplete multilib report header")
        temporary.replace(report_path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    collect(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
