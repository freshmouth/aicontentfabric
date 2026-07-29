# Sal Celtica V3 Autopilot

This account is isolated from Claire, Beyond The Label, HyperDash, and every other account.

The cloud workflow runs from GitHub Actions:

- wakes once per day
- checks the Mexico City date
- runs only every 4 days starting `2026-08-03`
- generates one Sal Celtica V3 video
- postprocesses it to publish-ready vertical MP4
- schedules it to Facebook and Instagram Reels through Metricool at `12:00`

## Required GitHub Actions Secrets

Add these as repository secrets before relying on cloud execution:

- `OPENAI_API_KEY`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `SAL_CELTICA_METRICOOL_API_TOKEN`
- `SAL_CELTICA_METRICOOL_USER_ID`
- `SAL_CELTICA_METRICOOL_BLOG_ID`

`GOOGLE_SERVICE_ACCOUNT_JSON` should be the full Google service account JSON with Vertex AI and Storage permissions for the configured bucket.

## Local Test

Use plan-only mode to validate cadence and config without calling paid providers:

```bash
python tools/sal_celtica_v3_autopilot.py --plan-only --force
```

Use dry-run publish mode to generate the video but avoid creating a Metricool post:

```bash
python tools/sal_celtica_v3_autopilot.py --force --dry-run
```

## Caption Rule

Captions must be contextual and open-looped. They must not copy the video script exactly, and they must keep the same CTA keyword used in the video: `Comenta SAL`.
