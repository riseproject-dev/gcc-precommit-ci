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

Use `PATCHWORK_FILTER_EMAILS` only for intentional additional mailbox matching;
normal RISC-V matching is based on `riscv`/`risc-v` text in the mbox.

## Shadow And Production Modes

Keep `PATCHWORK_REPORTING_ENABLED` unset or different from `true` during shadow
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

Set `PATCHWORK_REPORTING_ENABLED=true` only after those checks. A missing token,
non-2xx Patchwork response, unexpected username, or legacy `toolchain-ci-rivos-`
context is a production failure, not a warning to ignore.

Patchwork publishing steps fail their job when the shared helper rejects the
request. In shadow mode the helper exits successfully without sending it.
Missing target reports and invalid aggregate summaries must never be reported
as passing tests.

## Failure Triage

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
`self-hosted`, `linux`, `x64`, and `rise-gcc-ci`, with a registration available to
this repository. Each build/test job uses 20 parallel workers.

Provision dependencies using the maintenance account before starting the
runner. Self-hosted jobs only verify prerequisites and must not have sudo.
They erase their own Actions workspace and execute incoming patch code, so use
an isolated, disposable execution environment with a separate workspace from
post-commit. A runner group controls routing; it does not provide execution
isolation. Keep the polling/reporting credentials out of the patch execution
environment when deploying the runner.

## Dependency Updates

Publish toolchain changes first, then post-commit, wait for a new valid
post-commit baseline, and only then update the pre-commit gitlink. When source
submodules change, bump every `submodules-archive-` restore/save key in both CI
repositories as applicable.

## Incident Stop Controls

To stop external writes immediately, set `PATCHWORK_REPORTING_ENABLED=false`.
Disable the pre-commit schedules if issue or artifact creation is also unsafe.
Do not delete historical issues, Actions runs, artifacts, or Patchwork checks;
they are required for audit and debugging. Revoke a suspected token and rotate
it before resuming.
