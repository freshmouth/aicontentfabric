# Google Omni Flash Clip Stack

Experimental parallel module for building reusable native-audio short-form clips with Google Gemini Omni Flash Preview.

It does not modify the current OpenAI Images, Kling, ElevenLabs, or modular reel pipelines.

## Purpose

The goal is to create an asset stack:

- hooks: short scroll-stopper clips
- mains/meals: reusable educational body blocks made from 3-10 second Omni segments
- ctas/desserts: closing clips with a comment CTA

Then the module assembles variants:

```text
hook + meal/main + CTA = one 40-60s reel
```

Because Gemini Omni Flash can generate video with native speech/audio, this module avoids ElevenLabs voiceover, Whisper timing, and lip-sync in the test path. Final variants are hard-cut FFmpeg concatenations of already generated clips that include their own audio.

## Product Authenticity Rule

Claire Natural is the on-camera character only. Never use `Claire Natural` as product branding, a package label, an ebook cover, a receipt title, a sticker, or a CTA asset.

Food scenes should use real supermarket products and ordinary retail packaging cues. Product packaging must feel like something from an actual grocery aisle, not creator-branded wellness merch.

## Official API Shape

This module targets Google Cloud Gemini Enterprise Agent Platform:

- model: `gemini-omni-flash-preview`
- endpoint: `https://aiplatform.googleapis.com/v1beta1/projects/PROJECT_ID/locations/global/interactions`
- duration per generated clip: `3s` to `10s`
- aspect ratio: `9:16` for Reels/TikTok/Shorts
- supported task shape: text-to-video or reference-to-video

Omni Flash is Preview. Treat it as experimental and keep the existing Kling/OpenAI/ElevenLabs pipeline intact until this path proves reliable.

## Google Cloud Setup

1. Create or select a Google Cloud project with billing enabled.
2. Enable the Agent Platform API.
3. Install the Google Cloud CLI.
4. Authenticate locally:

```powershell
gcloud auth login
gcloud auth application-default login
```

5. Add your project to `.env.local`:

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
```

Optional, if you want to bypass `gcloud` token lookup:

```env
GOOGLE_OAUTH_ACCESS_TOKEN=ya29...
```

For production-scale output storage, create a Cloud Storage bucket and set:

```json
"output_gcs_uri": "gs://your-bucket/omni_outputs/"
```

If `output_gcs_uri` is omitted, the adapter attempts to save inline video bytes returned by the API.

## Files

```text
pipeline_google_omni_stack/
  adapters/google_omni_flash.py
  omni_stack_runner.py
  config.google_omni_flash.example.json
  README_GOOGLE_OMNI_STACK.md
```

## Dry Run

Validate prompts, segment durations, and variant math without Google calls:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.example.json `
  --mode dry-run
```

Outputs:

```text
pipeline_google_omni_stack/runs/omni_stack_YYYYMMDD_HHMMSS/
  dry_requests/
  dry_run_manifest.json
  run_log.json
```

## Generate Clip Library

This creates reusable native-audio clips:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.example.json `
  --mode generate-library
```

Outputs:

```text
runs/omni_stack_.../
  library/
    hooks/<hook_id>/clip.mp4
    mains/<meal_id>/segment_*.mp4
    mains/<meal_id>/clip.mp4
    ctas/<cta_id>/clip.mp4
  library_manifest.json
  google_omni_provider_log.jsonl
```

## Build 50 Variants

After clips exist:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.example.json `
  --mode build-variants `
  --out pipeline_google_omni_stack\runs\YOUR_EXISTING_RUN
```

Outputs:

```text
variants/
  variant_001/final_video.mp4
  variant_001/variant.json
  ...
variant_manifest.json
```

## Final Filter Presets

The Google stack includes a reusable FFmpeg postprocess step for final polish and optional 60fps optical-flow interpolation.

Slider mapping:

```text
Exposure      -> eq=brightness
Contrast      -> eq=contrast
Saturation    -> eq=saturation
Highlights    -> curves custom points or curves preset
Shadows       -> curves custom points or curves preset
Temp          -> colortemperature
Tint          -> colorbalance
Sharpen       -> unsharp
Clarity       -> unsharp, kept subtle for faces
Vignette      -> vignette
Fade          -> eq=gamma or curves lifted blacks
Fluidity      -> minterpolate
```

Legacy soft-dark preset:

```text
eq=contrast=0.92:brightness=-0.04:saturation=1.0:gamma=1.0,
curves=preset=darker,
colortemperature=temperature=5200,
unsharp=5:5:0.12:3:3:0.0,
vignette=PI/5,
minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=fdiff:scd_threshold=8.0,
format=yuv420p
```

`scd=fdiff` and `scd_threshold=8.0` are included so FFmpeg does not try to optical-flow morph across hard cuts.

CapCut-style UGC adjustment presets are available from the attached slider stack:

```text
temp -3
tint +2
saturation -6
exposure -3
contrast +12
highlight -35
shadow +18
fade +6
```

Use `capcut_ugc_adjustments_30fps` for fast visual review. It maps the sliders to:

- exposure, contrast, saturation -> `eq`
- highlights, shadows, fade -> custom `curves`
- temp, tint -> `colortemperature` + `colorbalance`
- clarity/sharpen -> subtle `unsharp`

Use `capcut_ugc_adjustments_60fps` only for final winner exports. It applies the same grade plus:

```text
minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=fdiff:scd_threshold=8.0
```

Postprocess one finished video:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.batch5.live.json `
  --mode postprocess-video `
  --postprocess-input pipeline_google_omni_stack\runs\batch5_live_20260709_001\final_videos\video_01.mp4 `
  --postprocess-output pipeline_google_omni_stack\runs\batch5_live_20260709_001\final_videos\video_01_capcut_ugc_adjustments_30fps.mp4 `
  --postprocess-preset capcut_ugc_adjustments_30fps
```

Postprocess all variants in a run:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.batch5.live.json `
  --mode postprocess-variants `
  --out pipeline_google_omni_stack\runs\batch5_live_20260709_001 `
  --postprocess-preset capcut_ugc_adjustments_30fps
```

Full optical-flow interpolation is CPU-heavy. Use `capcut_ugc_adjustments_30fps` for fast review drafts and `capcut_ugc_adjustments_60fps` for final winners.

## One Command

Generate the library and variants in one run:

```powershell
python pipeline_google_omni_stack\omni_stack_runner.py `
  --config pipeline_google_omni_stack\config.google_omni_flash.example.json `
  --mode all
```

## Scaling Strategy

Start small:

- 2 hooks
- 1 meal block
- 1 CTA
- 2 variants

Then scale to:

- 10 hooks
- 3-5 meals
- 5-7 CTAs
- 50 variants

Each final reel stores its component IDs in `variant.json`, so later Meta/Instagram metrics can map performance back to the exact hook, meal, and CTA used.

## Success Criteria

- Every generated clip includes native audio.
- Hook, meal, and CTA clips are reusable.
- Meal blocks are assembled from valid 3-10 second Omni segments.
- Final variants are 40-60 seconds.
- Final variants use hard cuts only.
- No ElevenLabs, Whisper, lip-sync, or Kling steps are required in this module.
