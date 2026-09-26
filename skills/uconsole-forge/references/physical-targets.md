# Live SSH target transactions

The default hardware development loop is edit/build, test in the IDE, back up
affected target state on the host, deploy over SSH, validate, and retain a
restore path. Successful changes can stay installed while development continues;
qualification must separately exercise restoration. External-card flashing is
optional provisioning, not a prerequisite for this loop.

## Agent interface

Call `target_transactions` with the registered workspace ID. It lists named
owner-approved transactions and their policy/approval digests, without exposing
backup contents or target paths. `execution_granted` reflects the connection's
`target-write` grant. An attached read-only client does not inherit the owner's
write permission.

For an approved transaction, `target_recovery_inspect` takes only `workspace`
and `transaction`. It requires `target-write` for fixed remote execution but
does not mutate the target. Poll its job and inspect file statuses/watchdog
observations. It does not create backups, publish a recovery image, reboot, or
prove fallback merely because a runtime watchdog is active. A denied grant is
not a reason to bypass MCP with an arbitrary SSH command.

When deployment or restoration is within the user's request, invoke
`target_transition` with only `workspace`, `transaction` and `direction`
(`apply` or `restore`). Do not supply a host, arbitrary unit body, command,
journal path, or replacement digest. If no appropriate transaction is approved,
ask the owner to prepare/review it; do not create a competing owner or grant to
bypass the policy. This interface does not itself prepare backups or authorize
new service behavior.

Poll the returned job with `job_status`. Target transitions are not cancellable
once running. Check the result and context: file transactions carry
`plan_sha256`; service pairs carry `kind: service` and
`authorization_sha256`; both retain `policy_sha256`. These pins bind the reviewed
operation, not proof that arbitrary application behavior is safe.

Workbench's physical-target panel can prepare file or standalone-service plans.
**Prepare service…** captures backups without granting writes. Separate approval
creates a private target journal and registers the pinned pair; it does not
deploy the service. Apply is another explicit action. If journal provisioning
loses its acknowledgement, retain its intent and inspect it rather than blindly
creating another journal. Service plans currently support a new standalone unit or an
existing local unit replacement preserving enablement; drop-ins, package
migration and general application-data recovery are not qualified by those
paths. Dependency captures are review evidence, not permission to modify every
related unit.

## Uncertain outcomes and recovery

A failed or timed-out job may have changed the device. Retain host backups,
dispatch records, and target phase journals. Do not reverse direction or retry
blindly. Inspect the original owner's jobs and journal to establish which
direction needs reconciliation. Service dispatch refuses an opposite direction
while the prior one is uncertain. Reconciliation uses the same approved pair
and target ledger; non-repeatable start/stop effects can require investigation
rather than another invocation. Do not edit an approved plan to make its digest
match new assumptions.

After a service pair is restored, another development cycle needs a newly
prepared transaction; completed target ledgers are not reusable deployment
scripts. A reboot changes the approved service boot identity and requires
reconciliation/review, not deletion of the old journal.

Report exactly what was verified: file contents/metadata and service state are
not application-database consistency, physical peripheral equivalence, or
whole-image boot evidence. Boot/kernel and whole-system changes require a
recovery path that still works without normal SSH. Never overwrite the mounted
system card or describe a live raw copy as a verified consistent backup.

Backups, images, and journals can contain credentials and user data. Keep them
private; they are not release assets by default.
