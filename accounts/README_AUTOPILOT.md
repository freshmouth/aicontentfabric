# Multi-Account V3 Autopilot

The system scales by adding account folders, not by changing pipeline code.

Each account is a separate tenant:

- own `account.json`
- own `autopilot_v3.json`
- own V3 source/wrapper configs
- own `publish_config.json`
- own `generation_rules.md`
- own references
- own `runs/`
- own Metricool secrets

The shared cloud entrypoint is:

```bash
python tools/account_autopilot.py --all-due
```

For one account:

```bash
python tools/account_autopilot.py --account sal_celtica
```

For no-cost validation:

```bash
python tools/account_autopilot.py --all-due --plan-only --today 2026-08-03
python tools/account_autopilot.py --account sal_celtica --plan-only --force
```

## Registry

The master registry is `accounts/registry.json`.

An account only runs when:

- it is listed in the registry
- `enabled` is `true`
- the account has an `autopilot_v3.json`
- the date matches its cadence, unless `--force` is used

## Isolation Rules

The generic runner refuses cross-account contamination:

- account config must declare the same `account_id`
- V3 wrapper config must live inside that account folder
- V3 source config must live inside that account folder
- cached references must live inside that account folder
- publish config must declare the same account ID
- video manifest is written with that account ID

## GitHub Secrets Pattern

Shared provider secrets:

- `OPENAI_API_KEY`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`

Preferred account-scoped secret bundle:

- `ACCOUNT_PUBLISH_SECRETS_JSON`

Example:

```json
{
  "sal_celtica": {
    "METRICOOL_API_TOKEN": "...",
    "METRICOOL_USER_ID": "...",
    "METRICOOL_BLOG_ID": "..."
  },
  "account_101": {
    "METRICOOL_API_TOKEN": "...",
    "METRICOOL_USER_ID": "...",
    "METRICOOL_BLOG_ID": "..."
  }
}
```

The runner expands each entry to the account-prefixed env vars expected by that account's `publish_config.json`.

Legacy direct GitHub secrets are still supported:

- `<ACCOUNT_PREFIX>_METRICOOL_API_TOKEN`
- `<ACCOUNT_PREFIX>_METRICOOL_USER_ID`
- `<ACCOUNT_PREFIX>_METRICOOL_BLOG_ID`

Example for `sal_celtica`:

- `SAL_CELTICA_METRICOOL_API_TOKEN`
- `SAL_CELTICA_METRICOOL_USER_ID`
- `SAL_CELTICA_METRICOOL_BLOG_ID`

## Adding Account 101

Create a new folder from a working account template, add it to `accounts/registry.json`, set its own secrets in GitHub Actions, and enable it.

No shared pipeline code should be modified for normal account onboarding.

Shortcut:

```bash
python tools/create_account.py --account account_101 --display-name "Account 101" --template v3_ugc
```
