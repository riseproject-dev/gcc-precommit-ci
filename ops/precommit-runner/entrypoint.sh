#!/usr/bin/env bash
set -euo pipefail

# The operator provides a repository-scoped, single-job JIT configuration.
# Do not give the container a PAT, registration API token, or host engine socket.
config_file=${RUNNER_JIT_CONFIG_FILE:-/run/secrets/runner-jit-config}
if test ! -s "$config_file"; then
    echo "A single-job runner JIT configuration is required." >&2
    exit 1
fi

check-precommit-dependencies
cd /opt/actions-runner
exec ./run.sh --jitconfig "$(cat "$config_file")"
