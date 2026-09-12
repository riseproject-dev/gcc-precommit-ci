# RISE Pre-Commit CI Cutover

## Architecture

`riseproject-dev/gcc-precommit-ci` consumes `riseproject-dev/riscv-gnu-toolchain-ci`
as the shared CI script submodule on the `build-frequent` branch. Patch
discovery continues to read the upstream GCC Patchwork project. Baseline
selection and comparison artifacts come from `riseproject-dev/gcc-postcommit-ci`.
Patchwork check detail links point back to issues and comments in the RISE
pre-commit repository.

The normal data flow is:

1. `patchworks.yaml` discovers new GCC Patchwork patches and stores patch URL
   artifacts in this repository.
2. `init-submodules.yaml` restores or initializes GCC source submodules.
3. `run-checks.yaml`, `lint.yaml`, and `test-regression.yaml` create RISE
   status issues and comments.
4. `generate-precommit-summary.yaml` reads RISE post-commit baseline artifacts
   and produces comparison summaries.
5. `post_check_to_patchworks.py` posts Patchwork checks only when
   `PATCHWORK_REPORTING_ENABLED` is exactly `true`.

## Repository Settings

- Actions must be enabled for all workflows.
- The default branch should be `main`.
- Issues must be enabled because CI status is tracked through repository issues.
- The `production` environment must exist for jobs that post status, fetch
  baselines, or update Patchwork.
- Workflow token permissions should default to read-only. Jobs that create or
  update issues need `issues: write`; jobs that download artifacts from the
  current repository need Actions artifact read access.
- Branch protection should require the pre-pull-request workflow, including the
  migration ownership guard.

## Secrets And Variables

| Name | Scope | Minimum permissions | Purpose | Required before |
| --- | --- | --- | --- | --- |
| `PATCHWORK_API` | Repository or organization secret | GCC Patchwork check write access for a RISE-managed service account | Post Patchwork check results | Production cutover |
| `PATCHWORK_CHECK_USERNAME` | Repository variable | Patchwork username, not a token | Identifies checks written by the RISE account during recovery scans | Shadow validation |
| `PATCHWORK_REPORTING_ENABLED` | Repository variable | Set to `true` only after shadow results are verified | Enables Patchwork writes; any other value skips writes | Shadow testing can run while unset |
| `PATCHWORK_FILTER_EMAILS` | Repository variable | Comma-separated mailbox list | Optional additional Patchwork mbox matching | Only if mailbox filtering is required |
| `PATCHWORK_OVERRIDE_USERS` | Repository variable | Comma-separated GitHub logins, without spaces | Allows only designated maintainers to issue manual Patchwork result commands | Production cutover |
| `RISE_CI_READ_TOKEN` | Repository or organization secret | Read Actions artifacts and metadata from `riseproject-dev/gcc-postcommit-ci` | Download baseline sum files | First pre-commit comparison |
| `GIST_TOKEN` | Repository or organization secret | Create gists from a RISE-managed account | Store oversized build or summary excerpts | Only needed for large report previews |
| `GITHUB_TOKEN` | Built-in | `contents: read`, `actions: read`, `issues: write` on this repository where jobs require it | Checkout, artifact access, and issue updates | Shadow testing |

Do not use personal GitHub tokens for any of these secrets.

## Runner Inventory

| Workflow/job | Labels | Purpose | Required software | Scope |
| --- | --- | --- | --- | --- |
| `patchworks.yaml/fetch_patches` | `self-hosted`, `linux`, `x64`, `rise-gcc-ci`, `ping` | Dedicated Patchwork polling and patch artifact creation | Python, zip, network access to Patchwork and GitHub | Dedicated RISE runner group |
| `staging.yaml/get-patch-info` | `self-hosted`, `linux`, `x64`, `rise-gcc-ci`, `ping` | Staging patch lookup for pull requests | Python, zip, GitHub API access | Dedicated RISE runner group |
| `test-regression.yaml/rerun-timeouts` | `self-hosted`, `linux`, `x64`, `rise-gcc-ci` | Long retry builds and tests | GCC build prerequisites, Python, enough disk for toolchain builds | Dedicated RISE runner group |
| `test-regression.yaml/run-on-self-hosted` | `self-hosted`, `linux`, `x64`, `rise-gcc-ci` | Long production build and tests | GCC build prerequisites, Python, enough disk for toolchain builds | Dedicated RISE runner group |

The code cannot register runners. RISE must register the runner labels above and
complete a workflow before production ownership can be claimed.

## Bootstrap

1. Merge and publish the RISE toolchain submodule commit first.
2. Merge post-commit migration changes and wait for a valid RISE baseline issue
   and baseline artifacts.
3. Configure `RISE_CI_READ_TOKEN` in this repository.
4. Configure `PATCHWORK_CHECK_USERNAME` for the RISE Patchwork account and
   leave `PATCHWORK_REPORTING_ENABLED` unset or set to a non-`true` value.
5. Run a workflow_dispatch or staging pre-commit job against a real Patchwork
   patch and inspect the generated RISE issue, artifact downloads, and skipped
   Patchwork log messages.

## Cutover

1. Confirm the dedicated `ping` runner and build runners are online under RISE.
2. Confirm `PATCHWORK_API`, `RISE_CI_READ_TOKEN`, and `GIST_TOKEN` are RISE
   service-account credentials.
3. Confirm generated issues link to `riseproject-dev/gcc-precommit-ci` and
   baseline links point to `riseproject-dev/gcc-postcommit-ci`.
4. Set `PATCHWORK_REPORTING_ENABLED=true`.
5. Process one real patch and verify the Patchwork check target URL points to a
   RISE pre-commit issue or Actions run.
6. Disable legacy schedules only after the RISE shadow and posting runs are
   verified.

Before enabling schedules, update the repository description, make the RISE
repository the local `origin`, verify the maintainer team and rulesets, and
export/recreate labels, environments, Actions settings, webhooks, Apps, deploy
keys, and runner registrations. Git history alone does not transfer this state.

## Governance Handoff

This repository does not currently contain a top-level license file. Before the
RISE repository is presented as the authoritative project, confirm the code's
provenance and redistribution terms with the former maintainers and add the
approved license; changing repository URLs does not itself transfer copyright.

Add `CODEOWNERS` only after the exact RISE maintainer team slug is confirmed,
and grant ownership to that team rather than to an individual account. Record
the primary and backup service owners, incident contact, and token-rotation
owner in the RISE operations system.

## Rollback

Set `PATCHWORK_REPORTING_ENABLED` to any value other than `true` to stop
Patchwork writes while keeping patch fetch, build, and test artifacts running.
If baseline reads fail, restore service by fixing `RISE_CI_READ_TOKEN` or by
pausing pre-commit schedules until a valid RISE post-commit baseline is
available. Do not re-enable personal repository fallbacks.

## GCC Release Policy

Pre-commit compares patches against the RISE post-commit baseline and does not
schedule release-branch testing directly. The older `14` release line is retired
from active CI configuration, while the post-commit baseline service now carries
the maintained `15` and `16` release conventions.
