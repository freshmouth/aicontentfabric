# AI Content Factory Dashboard

The dashboard is a separate cloud control plane for the existing account-isolated V3 production system. It does not replace image, video, FFmpeg, or publishing adapters.

## What It Controls

- Account registry and readiness
- Per-account cadence overrides
- ChatGPT-generated V3 creative drafts
- Account-scoped photo references for new drafts and revisions
- Manual drafts, direct cloud generation, and Metricool publishing without an autopilot manifest
- Versioned revisions and approvals
- Test or live cloud generation
- GitHub Actions execution status
- Durable video history with authenticated inline previews
- Per-account Google Cloud generation routing with temporary-output cleanup
- Metricool scheduling through the existing pipeline
- Cloud scheduler ticks with duplicate-run protection

Every draft and job contains one `account_id`. The API rejects a request when the selected account and V3 source config do not match. Social credentials remain in GitHub/Cloud secrets and are never stored in Firestore drafts.

## Manual Mode Versus Autopilot

Every registered account with an `account.json` is **manual ready**. It can use Creative Lab, generate a one-off V3 video, and publish or schedule that result through its own `publish_config.json`. It does not need `autopilot_v3.json`.

`autopilot_v3.json` is required only for recurring unattended concept rotation. The cloud scheduler ignores manual-only accounts. Enabling recurring scheduling from the dashboard is rejected until the selected account has a resolvable autopilot manifest.

When a manual live-publish job has no explicit publish time, the worker schedules it through Metricool ten minutes after media generation completes. Attached dashboard photos are downloaded from the selected account's private Cloud Storage prefix and passed into first-frame generation as visual references.

Metricool API tokens, user IDs, and blog IDs remain GitHub secrets. The committed account `publish_config.json` contains environment-variable names and destination routing only, never credentials.

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

The browser exchanges the token for a signed, `HttpOnly`, `SameSite=Strict` session cookie. With **Keep this computer signed in** enabled, the session lasts 90 days by default (`DASHBOARD_SESSION_DAYS`) without storing the raw dashboard token in browser storage.

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

Use the paperclip or drag a batch from Windows into Creative Lab to attach up to 20 JPEG, PNG, or WebP photos, each no larger than 8 MB. Press `Enter` to send and `Shift+Enter` for a new line. The original files are stored under `factory-dashboard/uploads/<account_id>/` and are sent to the OpenAI creative request as image inputs. Draft metadata stores only the account-scoped attachment record; references from one account are rejected if used by another account.

For local development, leave `DASHBOARD_UPLOAD_BUCKET` empty and uploads are written to the gitignored `factory_dashboard/.data/uploads/` directory. Cloud Run uses `ai-content-factory-501821-omni-outputs`.

## Multi-Project Google Cloud Routing

Each account can save an independent generation route from the dashboard or with:

```http
PATCH /api/accounts/<account_id>/cloud-route
Content-Type: application/json

{
  "generation_project_id": "client-generation-project",
  "generation_service_account": "video-worker@client-generation-project.iam.gserviceaccount.com",
  "generation_location": "global",
  "staging_gcs_uri_prefix": "gs://client-staging/accounts/<account_id>",
  "master_gcs_uri_prefix": "gs://factory-master/accounts/<account_id>",
  "cleanup_staging": true
}
```

No service-account keys are stored. The central GitHub Workload Identity principal impersonates the configured account service account using short-lived credentials. Grant the central worker permission to impersonate that service account, and grant the account service account only the generation and staging permissions it needs.

For every job, the worker:

1. Generates into `staging_gcs_uri_prefix/jobs/<job_id>/` with the account's short-lived identity.
2. Uploads the finished video to `master_gcs_uri_prefix/generation-history/<job_id>/final_video.mp4` using the master identity.
3. Reloads the master object and verifies its byte size and MD5 checksum.
4. Writes `result.json` beside the final video.
5. Deletes only the generation-matched objects inside that job's staging prefix.

If archive verification fails, staging is retained and the job fails clearly. The dashboard proxies byte-range requests from the private master bucket, so completed videos appear as playable previews while remaining private.

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
