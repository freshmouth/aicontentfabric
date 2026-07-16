# Mix Video Lane

Experimental folder for non-character mixed-format videos.

This lane is separate from Claire/Sarah character automation. It uses the existing Google Omni stack as a clip executor:

```text
storyboard -> separate 3-10s leaf clips -> hard-cut FFmpeg assembly -> final video
```

Use this folder when the video is driven by a storyboard, presenter+B-roll mix, or a cinematic explainer format instead of a persistent avatar.

## Current Test

- Concept: bottled water/minerals controversy
- Language: Spanish
- Target length: 45 seconds
- Provider: Google Gemini Omni Flash through `pipeline_google_omni_stack`
- Output path: `Mix/runs/agua_minerales_es_YYYYMMDD_HHMMSS/variants/variant_001/final_video.mp4`
