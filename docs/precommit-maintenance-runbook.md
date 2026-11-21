# Pre-Commit Maintenance Runbook

The RISE pre-commit service polls the GCC Patchwork project, applies relevant
RISC-V patch series to a known post-commit baseline and trunk, runs lint/build/
testsuite checks, records details in RISE GitHub issues, and optionally posts
the result back to Patchwork.

## Routine Queue Check

Review the most recent `Patchworks` workflow on every on-call pass:

1. Confirm every relevant patch in the polling interval produced a patch
   artifact and a RISE issue. The nightly `Downtime-Runner` is a recovery path,
   not a substitute for checking the queue.
2. For a patch that was not tested, dispatch `Patchworks` manually with its
   Patchwork patch ID. Record the manual run URL on any incident issue.
3. Review failed jobs for runner loss, source/Patchwork timeouts, artifact
   expiry, disk pressure, or rate limiting before treating them as patch
   failures.
4. Confirm the reported baseline issue is a RISE post-commit issue carrying
   `valid-baseline`, and that all result links point to
   `riseproject-dev/gcc-precommit-ci`.

Queue a GCC Patchwork patch when its own subject identifies RISC-V or its diff
changes a RISC-V target path. Polling, staging, manual patch IDs and downtime
recovery apply the same rule. A generic patch earlier in the same series may
be applied as a prerequisite. Only qualifying patches receive their own build
and Patchwork checks. Out-of-scope IDs produce an empty build queue.

## Shadow And Production Modes

Keep `PATCHWORK_REPORTING_ENABLED` unset or set to `false` during shadow
testing. GitHub issues, artifacts, builds, and comparisons still run, but no
Patchwork check may be written. Logs must explicitly say that reporting was
skipped.

Before enabling writes:

- set `PATCHWORK_API` to a RISE service-account token;
- set `PATCHWORK_CHECK_USERNAME` to that account's Patchwork username;
- set `PATCHWORK_OVERRIDE_USERS` to an exact comma-separated list of designated
  GitHub maintainers (no spaces); organization membership alone is not enough;
- verify emitted contexts use the `toolchain-ci-rise-` prefix;
- run one real patch in shadow mode and compare it with the legacy result;
- restrict manual result overrides to designated repository maintainers.

To validate publication before enabling all schedules, set the repository variable
`PATCHWORK_REPORTING_ENABLED` to the full 40-character SHA of the reviewed
workflow revision, then dispatch `Patchworks` at that revision. Only a manual
`Patchworks` run with that exact SHA may publish in this mode. Verify the RISE
check and its target URL on the selected patch before switching the variable to
`true` for normal production reporting or `false` to stop writes.

Set `PATCHWORK_REPORTING_ENABLED=true` only after those checks. A missing token,
non-2xx Patchwork response, unexpected username, or legacy `toolchain-ci-rivos-`
context is a production failure, not a warning to ignore.

Patchwork publishing steps fail their job when the shared helper rejects the
request. In shadow mode the helper exits successfully without sending it.
Missing target reports and invalid aggregate summaries must never be reported
as passing tests.

## Failure Triage

Read the testsuite comment and its linked comparison evidence before assigning
a failure to the patch. The Patchwork `test` check uses the following outcomes.

| State | Meaning |
| --- | --- |
| `fail` | The raw DejaGnu results show at least one matching subtest changing from PASS to FAIL. The comment identifies any incomplete targets separately. |
| `warning` | Results need review. A test may be newly observed, a build may have failed, or a comparable baseline or testsuite result may be missing. |
| `success` | The expected matrix was compared and has no verified regression or new result awaiting review. Known failures may still be present in both runs. |

Use the build check for the affected target to diagnose a build failure. Track
reproducible GCC defects in the status issue or upstream bug tracker.

- Apply failures: check series order and prerequisites. Distinguish a patch that
  no longer applies to trunk from an artifact/download failure.
- Build or testsuite failures: compare with the exact post-commit baseline and
  inspect full logs. Do not attribute an existing baseline failure to the patch.
- Flaky tests: update the narrowest applicable pre-commit allowlist and the
  matching post-commit allowlist in the same maintenance change. Include an
  upstream or RISE tracking issue.
- Random infrastructure failures: rerun only after capturing the failure class,
  runner label, and run URL. Repeated cases need an owned incident issue.

For a failed summary job, use **Re-run failed jobs** on the original Actions run
while its artifacts are retained. This preserves the original workflow inputs,
issue/comment IDs, and run artifacts. If those artifacts have expired, dispatch
`Patchworks` again for the patch and inspect the new run's complete results.
`Generate-Summary` is available only through `workflow_call`: its former
standalone dispatch did not supply the issue/comment IDs or source-run context
required to publish a safe report.

The nightly recovery checkpoint advances only after a successful scan with no
patches, or after every recovered patch finishes successfully. Failed or
cancelled processing retains the previous checkpoint for the next scan. A red
recovery run therefore needs attention even if some patch issues look complete.

## Runner Safety

Patch polling and staging metadata lookup run on `ubuntu-24.04`; they do not
wait behind self-hosted builds or require a `ping` runner. Long builds require
`self-hosted`, `linux`, `x64`, and `rise-precommit-regression-07`, with a registration available to
this repository. Each build/test job uses 32 parallel workers.

Provision dependencies using the maintenance account before starting the
runner. Self-hosted jobs only verify prerequisites and must not have sudo.
They erase their own Actions workspace and execute incoming patch code, so use
an isolated, disposable execution environment with a separate workspace from
post-commit. A runner group controls routing; it does not provide execution
isolation. Keep the polling/reporting credentials out of the patch execution
environment when deploying the runner. The reusable `regression-worker.yaml`
workflow receives read-only Actions and repository access. GitHub-hosted jobs
in `test-regression.yaml` publish queued and completed checks. A failed build
links its Actions run even when its optional log preview is unavailable.

## Dependency Updates

Merge shared toolchain changes first. For changes confined to pre-commit patch
selection, update the pre-commit gitlink to the published toolchain revision.
For changes that affect baseline comparability, update post-commit and wait for
a new valid baseline before updating pre-commit. When source submodules change,
bump the `precommit-hosted-sources-` restore/save key in the hosted source job.
Patch workers receive the prepared `gcc-sources-<baseline>` artifact for their run.

## Incident Stop Controls

To stop external writes immediately, set `PATCHWORK_REPORTING_ENABLED=false`.
Disable the pre-commit schedules if issue or artifact creation is also unsafe.
Do not delete historical issues, Actions runs, artifacts, or Patchwork checks;
they are required for audit and debugging. Revoke a suspected token and rotate
it before resuming.
