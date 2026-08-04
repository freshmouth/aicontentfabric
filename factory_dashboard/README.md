# AI Content Factory Dashboard

The dashboard is a separate cloud control plane for the existing account-isolated V3 production system. It does not replace image, video, FFmpeg, or publishing adapters.

## What It Controls

- Account registry and readiness
- Per-account cadence overrides
- ChatGPT-generated V3 creative drafts
- Account-scoped photo references for new drafts and revisions
- Versioned revisions and approvals
- Test or live cloud generation
- GitHub Actions execution status
- Metricool scheduling through the existing pipeline
- Cloud scheduler ticks with duplicate-run protection

Every draft and job contains one `account_id`. The API rejects a request when the selected account and V3 source config do not match. Social credentials remain in GitHub/Cloud secrets and are never stored in Firestore drafts.

## Local Run

```powershell
python -m pip install -r factory_dashboard/requirements.txt
$env:DASHBOARD_STORE="local"
$env:DASHBOARD_ADMIN_TOKEN="local-dev-token"
$env:OPENAI_API_KEY="..."
$env:GITHUB_DASHBOARD_TOKEN="..."
$env:DASHBOARD_UPLOAD_BUCKET=""
python -m uvicorn factory_dashboard.app.main:app --host 127.0.0.1 --port 8088
```

Open `http://127.0.0.1:8088` and enter `local-dev-token`.

## Cloud Runtime

The production deployment uses:

- Cloud Run with zero minimum instances
- Firestore for drafts, revisions, jobs, and schedule overrides
- Cloud Storage for private, account-scoped creative reference photos
- Secret Manager for dashboard, cron, OpenAI, and GitHub credentials
- GitHub Actions for the existing V3 media pipeline
- Metricool for account-specific publishing
- Cloud Scheduler calling `POST /api/system/tick`

Required Secret Manager secrets:

```text
factory-dashboard-admin-token
factory-dashboard-cron-token
openai-api-key
factory-dashboard-github-token
```

The GitHub token needs Actions read/write access for `freshmouth/aicontentfabric`. It does not need access to social credentials.

## Creative Photo References

Use the paperclip in Creative Lab to attach up to six JPEG, PNG, or WebP photos, each no larger than 8 MB. The original files are stored under `factory-dashboard/uploads/<account_id>/` and are sent to the OpenAI creative request as image inputs. Draft metadata stores only the account-scoped attachment record; references from one account are rejected if used by another account.

For local development, leave `DASHBOARD_UPLOAD_BUCKET` empty and uploads are written to the gitignored `factory_dashboard/.data/uploads/` directory. Cloud Run uses `ai-content-factory-501821-omni-outputs`.

## Deploy

The `Deploy Factory Dashboard` GitHub workflow runs on dashboard changes and can also be started manually. It builds on the GitHub runner, pushes to Artifact Registry, and deploys `factory-dashboard` to `us-central1`.

Provision the one-time Google Cloud resources from the repository root:

```powershell
.venv\Scripts\python.exe factory_dashboard\provision_cloud.py
```

The provisioner uses the active `gcloud` login, reads `OPENAI_API_KEY` from `.env.local`, reads the current GitHub credential from Git Credential Manager, and writes only the dashboard operator tokens to the gitignored `factory_dashboard/.data/dashboard_credentials.json`.

After deployment, configure Cloud Scheduler to call:

```text
POST https://<cloud-run-url>/api/system/tick
X-Factory-Cron: <factory-dashboard-cron-token>
```

The deployment workflow creates or updates a daily Cloud Scheduler job. The endpoint evaluates each account's timezone and interval and creates a deterministic daily job id, so repeated scheduler calls cannot enqueue the same account twice on one date. GitHub's old cron trigger is intentionally disabled to prevent duplicate production runs.
