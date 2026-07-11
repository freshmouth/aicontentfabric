# Meta Graph Reels Scheduler

This module publishes finished videos to Instagram and Facebook through Meta Graph API without changing the video generation pipeline.

## Required Accounts

- Instagram account must be Professional, Business or Creator.
- Instagram must be connected to a Facebook Page.
- The app/token must have the required Meta permissions for Instagram content publishing and Page publishing.

## Environment Variables

Add these to `.env.local`:

```text
INSTAGRAM_ACCESS_TOKEN=...
FACEBOOK_PAGE_ACCESS_TOKEN=...
META_GRAPH_SSL_NO_VERIFY=1
```

Do not put tokens in JSON config.

## Config

Copy `social_scheduler/config.meta.example.json` to a private config, then set:

```json
{
  "meta_graph": {
    "graph_version": "v23.0",
    "instagram_user_id": "17841400000000000",
    "facebook_page_id": "1234567890",
    "facebook_mode": "reels"
  }
}
```

## Important Instagram Constraint

Instagram Reels publishing requires a public `https://` MP4 URL. A local file path is not enough.

Facebook Reels publishing can upload the local MP4 directly.

## Google Cloud Storage Hosting

The scheduler can upload the finished MP4 to Google Cloud Storage before publishing to Instagram.

Config:

```json
"google_cloud_storage": {
  "enabled": true,
  "bucket": "ai-content-factory-501821-omni-outputs",
  "prefix": "meta-reels",
  "url_mode": "public",
  "make_public": true,
  "signed_url_duration": "2h"
}
```

Options:

- `url_mode: "public"` returns `https://storage.googleapis.com/<bucket>/<object>`. The object or bucket must be publicly readable.
- `url_mode: "signed"` returns a signed temporary URL, if your local `gcloud` credentials support signing URLs.
- `make_public: true` attempts to grant public read access to the uploaded object. This may fail if the bucket uses uniform bucket-level access.

If `public_video_url` is omitted and GCS hosting is enabled, the scheduler uploads automatically and fills `public_video_url` before Instagram publishing.

Discover visible account IDs from configured tokens:

```powershell
$env:META_GRAPH_SSL_NO_VERIFY='1'
python -m social_scheduler.scheduler `
  --config social_scheduler/config.meta.local.json `
  discover-accounts
```

## Queue A Finished Video

```powershell
python -m social_scheduler.scheduler `
  --config social_scheduler/config.meta.local.json `
  queue `
  --video "pipeline_google_omni_stack/runs/v2_claire_salad_leaf_strict_002/variants/variant_001/final_video_publish_ready.mp4" `
  --caption "Your salad dressing might be dessert oil. Comment LABEL and I’ll send the 3 things I check before a dressing goes in my cart." `
  --platforms instagram,facebook `
  --publish-at "2026-07-12T15:00:00Z"
```

## Publish Due Posts

Run this from Task Scheduler, cron, or manually:

```powershell
python -m social_scheduler.scheduler `
  --config social_scheduler/config.meta.local.json `
  run-due
```

## Publish Immediately

```powershell
python -m social_scheduler.scheduler `
  --config social_scheduler/config.meta.local.json `
  publish-now `
  --video "pipeline_google_omni_stack/runs/v2_claire_salad_leaf_strict_002/variants/variant_001/final_video_publish_ready.mp4" `
  --caption "Your salad dressing might be dessert oil. Comment LABEL and I’ll send the 3 things I check before a dressing goes in my cart." `
  --platforms instagram,facebook
```

## Dry Run

Use this before spending a real publish attempt:

```powershell
python -m social_scheduler.scheduler `
  --config social_scheduler/config.meta.local.json `
  publish-now `
  --video "pipeline_google_omni_stack/runs/v2_claire_salad_leaf_strict_002/variants/variant_001/final_video_publish_ready.mp4" `
  --public-video-url "https://your-public-host/final_video_publish_ready.mp4" `
  --caption "Test caption" `
  --platforms instagram,facebook `
  --dry-run
```

## Notes

- The scheduler is local. Meta does not need to know your desired future time; `run-due` publishes when the queue item is due.
- For Instagram, host the MP4 somewhere Meta can fetch it.
- For Facebook, `facebook_mode: "reels"` uses the Page Reels upload flow. Use `"page_video"` only as a fallback.
