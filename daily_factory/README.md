# Daily Factory V1

This release runs the existing Google Omni Flash creative stack as a Cloud Run Job, finishes the Reel with silence removal and final subtitles, stores the result in GCS, and publishes it to Instagram and Facebook through Meta Graph.

## Reliability model

- One immutable container tag per release.
- A deterministic daily concept queue, versioned separately from providers.
- A GCS lock prevents duplicate generation for the same local date.
- A daily manifest records the generated object and per-platform post IDs.
- Meta platform failures are explicit; there is no silent success or image/video fallback.
- New providers belong in separate adapters and release tags. Rollback means pointing the Job at the previous image tag.

## Local dry run

```powershell
python -m daily_factory.worker --dry-run --out daily_factory/dry_run
```

## Characters

The daily factory resolves the on-camera person from a character registry. Existing concepts use `claire_natural` by default. A new character remains draft-only until all three readiness gates are explicitly enabled:

- `identity_ready`: the complete identity, voice, visual world, and prompt rules are approved.
- `content_ready`: that character's own daily prompt list is installed.
- `publishing_ready`: a dedicated `meta_config` points to that character's Facebook/Instagram credential environment variables.

Draft characters fail before paid image or video generation. They cannot inherit Claire's Meta account. After all gates are ready, select the character on its own concept:

```json
{
  "id": "example_sarah_concept",
  "character_id": "sarah_cole",
  "hook_overlay": "YOUR BREAKFAST LABEL IS LYING",
  "hook": "That wellness word on the front is not the decision. The back is.",
  "segments": ["One complete spoken segment."]
}
```

Each character owns its own folder under `characters/<character_id>/` with `character.json` and `master_reference.png`. Character identity, outfit, room, and voice metadata do not carry across characters; only the generation, finishing, scheduling, and publishing automation is shared.

Character status:

- `claire_natural`: active default identity with its existing publishing profile.
- `sarah_cole`: draft registration only. No generation, schedule selection, or publishing is allowed until her identity prompts, prompt list, and dedicated Meta credentials are supplied and approved.

## Cloud resources

- Cloud Run Job: `ai-content-daily-factory`
- Cloud Scheduler: `ai-content-daily-0913`
- Region: `us-central1`
- Time zone: `America/Mexico_City`
- Secrets: `instagram-access-token`, `facebook-page-access-token`
- Output bucket: `ai-content-factory-501821-omni-outputs`

Deployments are performed by `deploy.ps1`. It builds an immutable Artifact Registry image, updates the Job, runs a cloud dry run, then configures the daily Scheduler only after that execution succeeds.
