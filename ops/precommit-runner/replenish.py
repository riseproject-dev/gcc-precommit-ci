"""Keep one single-use listener ready for the RISE GCC pre-commit repository."""

import argparse
import base64
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

import jwt
import requests


REPO = "/repos/riseproject-dev/gcc-precommit-ci"
RUNNERS = REPO + "/actions/runners"
NAME = "rise-precommit-regression-07"
SERVICE = "rise-precommit-pilot.service"
CONTAINER = "rise-precommit-07-pilot"
SECRET = "runner-jit-config"


def api(method, path, token, body=None, *, missing_ok=False):
    response = requests.request(
        method,
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=(10, 30),
        allow_redirects=False,
    )
    if missing_ok and response.status_code == 404:
        return None
    if response.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"GitHub {method} {path} returned HTTP {response.status_code}"
        )
    return response.json() if response.content else None


def installation_token():
    key = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "app-key.pem"
    now = int(time.time())
    assertion = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]},
        key.read_bytes(),
        algorithm="RS256",
    )
    installation = api("GET", REPO + "/installation", assertion)
    result = api(
        "POST",
        f"/app/installations/{installation['id']}/access_tokens",
        assertion,
        {
            "repositories": ["gcc-precommit-ci"],
            "permissions": {"administration": "write"},
        },
    )
    return result["token"]


def command(*args, stdin=None):
    return subprocess.run(
        args, input=stdin, text=True, stdout=subprocess.PIPE, check=True, timeout=60
    ).stdout.strip()


def worker_idle():
    info = command(
        "systemctl", "--user", "show", SERVICE, "-p", "LoadState", "-p", "ActiveState"
    )
    state = dict(line.split("=", 1) for line in info.splitlines())
    if state["LoadState"] != "loaded":
        raise RuntimeError(f"{SERVICE} is not installed and loaded")
    if state["ActiveState"] not in ("inactive", "failed"):
        return False
    containers = command("podman", "ps", "-a", "--format", "{{.Names}} {{.State}}")
    for entry in containers.splitlines():
        name, status = entry.split()
        if name == CONTAINER:
            if status not in ("exited", "stopped", "created", "configured"):
                return False
            # Clean up an exited container left by a host reboot. Without
            # --force Podman will refuse to remove a running container.
            command("podman", "rm", CONTAINER)
    return True


def next_config():
    token = installation_token()
    registrations = api("GET", f"{RUNNERS}?name={NAME}", token)["runners"]
    matches = [item for item in registrations if item["name"] == NAME]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple registrations are named {NAME}")
    if matches:
        runner = matches[0]
        if NAME not in {label["name"] for label in runner["labels"]}:
            raise RuntimeError(f"The name {NAME} is in use by another registration")
        if runner["status"] != "offline" or runner["busy"] is not False:
            return None
        # Our fixed name also recovers a previous failed start or lost JIT
        # response. A missing registration means GitHub already retired it.
        api("DELETE", f"{RUNNERS}/{int(runner['id'])}", token, missing_ok=True)
    registration = api(
        "POST",
        RUNNERS + "/generate-jitconfig",
        token,
        {
            "name": NAME,
            "runner_group_id": 1,
            "labels": ["self-hosted", "Linux", "X64", NAME],
            "work_folder": "/srv/work",
        },
    )
    return registration["encoded_jit_config"]


def replenish(from_stdin):
    if not worker_idle():
        if from_stdin:
            raise RuntimeError("The pre-commit worker is already running")
        return
    config = sys.stdin.read().strip() if from_stdin else next_config()
    if config is None:
        return
    if not config or not base64.b64decode(config, validate=True):
        raise ValueError("Invalid JIT configuration")
    command("podman", "secret", "rm", "--ignore", SECRET)
    command("podman", "secret", "create", SECRET, "-", stdin=config)
    command("systemctl", "--user", "start", SERVICE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdin", action="store_true", help="Start once with an externally issued JIT"
    )
    args = parser.parse_args()
    runtime = Path("/run/user") / str(os.getuid())
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    with (runtime / "rise-precommit-jit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        replenish(args.stdin)


if __name__ == "__main__":
    main()
