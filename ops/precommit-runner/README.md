# Pre-commit runner on 07

The host runs one rootless Podman pre-commit worker alongside the post-commit
service. Each listener accepts one job. Its fresh container and workspace are
removed on exit. The production runner name and routing label are both
`rise-precommit-regression-07`. The pre-commit build workflow must request
that label. The worker uses the pilot image, systemd user service and resource
limits already installed on 07. Build jobs must use read-only repository
permissions and publish their results through a separate trusted job.

## Install the worker launcher

Run these commands on 07 from the reviewed pre-commit repository checkout.
The pilot account must already have its runner image, resource limits and
diagnostics directory. Install the Python host dependencies and enable its user
manager at boot.

```sh
sudo apt-get install python3-requests python3-jwt python3-cryptography
sudo loginctl enable-linger rise-precommit-ci
sudo install -D -m 0755 ops/precommit-runner/replenish.py /usr/local/lib/rise-precommit-jit/replenish.py
worker_home=$(getent passwd rise-precommit-ci | cut -d: -f6)
worker_uid=$(id -u rise-precommit-ci)
sudo -u rise-precommit-ci mkdir -p "$worker_home/.config/systemd/user"
sudo install -o rise-precommit-ci -g rise-precommit-ci -m 0644 ops/precommit-runner/rise-precommit-pilot.service "$worker_home/.config/systemd/user/rise-precommit-pilot.service"
sudo -Hu rise-precommit-ci env XDG_RUNTIME_DIR="/run/user/$worker_uid" systemctl --user daemon-reload
```

## Start one worker from the operator machine

This works before a GitHub App is installed. For a controlled rollout, use
the operator's existing GitHub login to issue a JIT for this repository and
stream it to the host. Each additional build job needs a new listener. Run
with a verified SSH host key for 07 and a drained pre-commit worker slot.
If switching back from automatic replenishment, first stop the timer and wait
for any active replenishment invocation and worker job to finish. This keeps
the operator and the timer from requesting the same registration.

```bash
set -o pipefail
printf '%s\n' '{"name":"rise-precommit-regression-07","runner_group_id":1,"labels":["self-hosted","Linux","X64","rise-precommit-regression-07"],"work_folder":"/srv/work"}' |
  gh api --method POST repos/riseproject-dev/gcc-precommit-ci/actions/runners/generate-jitconfig --input - --jq .encoded_jit_config |
  ssh -o ForwardAgent=no -o StrictHostKeyChecking=yes jiawei@rise-gcc-ci-07.di.riseproject.dev \
    'cd /tmp && sudo -nHu rise-precommit-ci /usr/bin/python3 /usr/local/lib/rise-precommit-jit/replenish.py --stdin'
```

## Keep the worker available automatically

Use a dedicated RISE GitHub App installed only on `gcc-precommit-ci` with
repository Administration write access, the scope required by GitHub's
repository runner registration API. A system timer checks the local worker
once a minute. When the worker is stopped, the controller authenticates with
the App, requests a short-lived installation token scoped to this repository,
and creates a new JIT registration. Only the JIT enters the worker container.

Store the App's private key in `/etc/rise-precommit-jit/app-key.pem`, owned by
root with mode 0600. Store `GITHUB_APP_ID=<app ID>` in
`/etc/rise-precommit-jit/app.conf`. Systemd makes the key available to the
controller through `LoadCredential`. The next invocation reads any rotated key.
The installation ID is looked up using the repository URL.

```sh
sudo install -m 0644 ops/precommit-runner/rise-precommit-jit.service ops/precommit-runner/rise-precommit-jit.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rise-precommit-jit.timer
sudo systemctl start rise-precommit-jit.service
journalctl -u rise-precommit-jit.service -n 30
```

To drain the slot, disable the replenishment timer and allow a running build
to finish. If the listener is online and idle, stop the worker user unit during
the maintenance window. Each timer invocation checks the worker service and
container before renewing. It removes an offline, idle registration only when
both its name and routing label match this controller. Busy or online
registrations are left to finish. GitHub API and host errors appear in the
service journal and the timer retries. Check repository runner status with
`gh api repos/riseproject-dev/gcc-precommit-ci/actions/runners`.
