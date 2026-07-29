# Social Reels Scheduler

This module schedules or publishes finished videos without changing the video generation pipeline.

Supported publishers:

- `metricool`: preferred production scheduler. Uploads the MP4 to Metricool and schedules Instagram/Facebook Reels in the selected Metricool brand.
- `meta`: legacy/direct Meta Graph publisher kept as a fallback.

Metricool API reference used for this integration:

- Base URL: `https://app.metricool.com/api`
- Auth header: `X-Mc-Auth`
- Required identifiers on every request: `userId` and `blogId`
- Upload endpoint: `/v2/media/s3/upload-transactions`
- Schedule endpoint: `/v2/scheduler/posts`

Metricool API access requires a Metricool plan with API access enabled.

## Account Isolation

Production publishing should use the account registry in `accounts/`, not one shared scheduler config.

Current account ids:

- `claire`
- `beyond_the_label`
- `hyperdash`
- `sal_celtica`
- `sarah_cole`
- `speliers`

Each account has its own Metricool brand ID, private secrets file, queue, prompt DNA, runs, and logs. The scheduler rejects mismatched account records/manifests and rejects generic token env names in account-scoped configs.

```powershell
python -m social_scheduler.scheduler list-accounts

python -m social_scheduler.scheduler `
  --account hyperdash `
  show-account

python -m social_scheduler.scheduler `
  --account hyperdash `
  validate-account
```

## Required Accounts

- Each Instagram/Facebook destination must be connected inside the correct Metricool brand.
- Each AI Content Factory account must map to exactly one Metricool `blogId`.
- Do not reuse one brand's `blogId` in another account folder.

## Private Account Secrets

Legacy Meta configs can still use the old generic variables:

```text
INSTAGRAM_ACCESS_TOKEN=...
FACEBOOK_PAGE_ACCESS_TOKEN=...
```

Account-scoped configs must use namespaced values in the account folder instead:

```text
accounts/<account_id>/secrets.env
```

For HyperDash with Metricool:

```text
HYPERDASH_METRICOOL_USER_ID=...
HYPERDASH_METRICOOL_BLOG_ID=...
HYPERDASH_METRICOOL_API_TOKEN=...
```

The scheduler automatically loads `accounts/hyperdash/secrets.env` when you run with `--account hyperdash`. The root `.env.local` can still hold shared provider keys such as Google Cloud or OpenAI, but Metricool IDs/tokens should stay inside the matching account folder.

Do not put tokens in JSON config. Do not commit `secrets.env`; only commit `secrets.env.example`.

## Metricool Config

Each account's `publish_config.json` should use:

```json
{
  "account_id": "hyperdash",
  "publisher": "metricool",
  "metricool": {
    "user_id": "HYPERDASH_METRICOOL_USER_ID",
    "blog_id": "HYPERDASH_METRICOOL_BLOG_ID",
    "api_token_env": "HYPERDASH_METRICOOL_API_TOKEN",
    "timezone": "America/Mexico_City",
    "networks": {
      "instagram": "instagram",
      "facebook": "facebook"
    },
    "post_types": {
      "instagram": "REEL",
      "facebook": "REEL"
    },
    "auto_publish": true,
    "draft": false
  }
}
```

## Program A Reel In Metricool

This uploads the local MP4 into Metricool and creates one scheduled post for the selected brand's Instagram and Facebook connections.

```powershell
python -m social_scheduler.scheduler `
  --account hyperdash `
  publish-now `
  --video "C:\path\to\final_video.mp4" `
  --caption "This is the new Kinect for iPhone. Comment RUN and I'll send the game link." `
  --platforms instagram,facebook `
  --publish-at "2026-07-29T09:00:00-06:00"
```

Use `--dry-run` first to verify the Metricool payload without uploading or scheduling:

```powershell
python -m social_scheduler.scheduler `
  --account hyperdash `
  publish-now `
  --video "C:\path\to\final_video.mp4" `
  --caption "Test caption" `
  --platforms instagram,facebook `
  --publish-at "2026-07-29T09:00:00-06:00" `
  --dry-run
```

If Metricool accepts the request, the returned status is `scheduled` and includes the Metricool scheduled post id/uuid.

## Legacy Meta Constraint

Instagram Reels publishing through direct Meta Graph requires a public `https://` MP4 URL. A local file path is not enough.

## Google Cloud Storage Hosting

The legacy Meta publisher can upload the finished MP4 to Google Cloud Storage before publishing to Instagram.

Config:

```json
"google_cloud_storage": {
  "enabled": true,
  "bucket": "ai-content-factory-501821-omni-outputs",
  "prefix": "accounts/hyperdash/meta-reels",
  "url_mode": "public",
  "make_public": true,
  "signed_url_duration": "2h"
}
```

Options:

- `url_mode: "public"` returns `https://storage.googleapis.com/<bucket>/<object>`. The object or bucket must be publicly readable.
- `url_mode: "signed"` returns a signed temporary URL, if your local `gcloud` credentials support signing URLs.
- `make_public: true` attempts to grant public read access to the uploaded object. This may fail if the bucket uses uniform bucket-level access.

If `public_video_url` is omitted and GCS hosting is enabled, the Meta publisher uploads automatically and fills `public_video_url` before Instagram publishing.

Discover visible account IDs from configured tokens:

```powershell
$env:META_GRAPH_SSL_NO_VERIFY='1'
python -m social_scheduler.scheduler `
  --account claire `
  discover-accounts
```

## Queue A Finished Video Locally

```powershell
python -m social_scheduler.scheduler `
  --account claire `
  queue `
  --video "pipeline_google_omni_stack/runs/v2_claire_salad_leaf_strict_002/variants/variant_001/final_video_publish_ready.mp4" `
  --caption "Your salad dressing might be dessert oil. Comment LABEL and I’ll send the 3 things I check before a dressing goes in my cart." `
  --platforms instagram,facebook `
  --publish-at "2026-07-12T15:00:00Z"
```

## Publish Due Posts

Run this from Task Scheduler, cron, or manually if you are using the local queue:

```powershell
python -m social_scheduler.scheduler `
  --account claire `
  run-due
```

## Publish Immediately Or Schedule In Metricool

```powershell
python -m social_scheduler.scheduler `
  --account hyperdash `
  publish-now `
  --video "HyperDash/runs/tv_treadmill_first_photo_ugc_ref_iphone_mount_20260723/omni_video/final_video_hyperdash_tv_treadmill_1080x1920.mp4" `
  --caption "My TV just became a treadmill. Comment RUN and I'll send the game link." `
  --platforms instagram,facebook `
  --publish-at "now"
```

## Dry Run

Use this before spending a real publish attempt:

```powershell
python -m social_scheduler.scheduler `
  --account hyperdash `
  publish-now `
  --video "HyperDash/runs/tv_treadmill_first_photo_ugc_ref_iphone_mount_20260723/omni_video/final_video_hyperdash_tv_treadmill_1080x1920.mp4" `
  --caption "Test caption" `
  --platforms instagram,facebook `
  --dry-run
```

## Notes

- With `publisher: "metricool"`, Metricool owns the cloud schedule. Your machine does not need to stay on after the scheduled post is accepted.
- With `publisher: "meta"`, the scheduler is local. `run-due` publishes only when the command runs.
- Do not schedule an account using another account's `blogId`; that is the main cross-contamination risk.
