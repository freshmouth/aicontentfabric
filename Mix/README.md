# Mix Video Lane

Experimental folder for non-character mixed-format videos.

This lane is separate from Claire/Sarah character automation. It uses the existing Google Omni stack as a clip executor:

```text
storyboard -> separate 3-10s leaf clips -> hard-cut FFmpeg assembly -> final video
```

Use this folder when the video is driven by a storyboard, presenter+B-roll mix, or a cinematic explainer format instead of a persistent avatar.

## V3 Composed Module

`Mix/v3` adds a composed execution layer for dynamic physical subjects. It does not replace the working Omni stack. It prepares subject-aware first-frame image prompts and image-QA prompts, then can optionally hand off to the current Omni video executor.

Optional fields:

- `subject_label`: short noun phrase, defaults to `product`
- `subject_placement_hint`: natural placement clause, defaults to `naturally held in hand or placed on surface`
- `cloudinary_reference_folder`: reference image folder, defaults to `catalog/sal-celtica/ugc-refs`
- `cloudinary_max_reference_images`: number of local references to attach to first-image generation

Cloudinary references require:

```env
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
```

If you want to reuse a separate local env file for testing, add `cloudinary_env_file` to the V3 config. Keep that as a local override because it is machine-specific.

Dry-run the V3 composition:

```powershell
python Mix\v3\pipeline_v3.py --config Mix\config.v3.dynamic_subject.example.json --out Mix\runs\v3_dynamic_subject_dry_run
```

Generate only the first image with Cloudinary UGC references:

```powershell
python Mix\v3\pipeline_v3.py --config Mix\config.v3.dynamic_subject.example.json --out Mix\runs\v3_first_image_refs --generate-first-image --with-cloudinary-refs
```

Run the existing Omni video executor after V3 composition:

```powershell
python Mix\v3\pipeline_v3.py --config Mix\config.v3.dynamic_subject.example.json --out Mix\runs\v3_dynamic_subject_live --execute-video --env-ssl-no-verify
```

## Current Test

- Concept: bottled water/minerals controversy
- Language: Spanish
- Target length: 45 seconds
- Provider: Google Gemini Omni Flash through `pipeline_google_omni_stack`
- Output path: `Mix/runs/agua_minerales_es_YYYYMMDD_HHMMSS/variants/variant_001/final_video.mp4`
