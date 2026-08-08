# Presell Account

Presell is an isolated V3 content-factory account. Its character references,
creative configuration, generated runs, cloud routes, publishing credentials,
and history must never be reused by another account.

## Current state

- Manual creative generation: available
- Recurring autopilot: disabled
- Publishing: disabled
- Pipeline: V3

The account can be selected in the Factory Dashboard for creative drafting and
manual generation. Enable scheduling or publishing only after Presell-specific
cloud storage and publisher credentials have been configured and validated.

Generated files belong under `accounts/presell/runs/` locally and under an
account-scoped `/accounts/presell/` prefix in cloud staging and master storage.
