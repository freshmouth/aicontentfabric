# Live deployment status

Updated: 2026-07-11 (America/Mexico_City)

## Cloud service

- Project: `ai-content-factory-501821`
- Region: `us-central1`
- Service: `meta-comment-automation`
- Revision: `meta-comment-automation-00003-xpw`
- URL: `https://meta-comment-automation-529307215825.us-central1.run.app`
- Callback: `https://meta-comment-automation-529307215825.us-central1.run.app/webhook`
- Firestore: Native `(default)`, free tier, `us-central1`
- Minimum instances: `0`

## Meta app

- App name: `AIFactory`
- App ID: `1379023027504762`
- Instagram object callback: active, `comments`
- Page object callback: active, `feed`
- Instagram account `28067143496203803`: subscribed to `comments`
- Facebook Page `1164431006757522`: pending account subscription

The current Facebook Page token lacks:

- `pages_manage_metadata`, required to subscribe the Page to the `feed` webhook
- `pages_messaging`, required for Facebook private Messenger replies

Generate a replacement Page token with those scopes plus the existing engagement scopes, update `FACEBOOK_PAGE_ACCESS_TOKEN` in `.env.local`, add a new `facebook-page-access-token` Secret Manager version, and rerun the Page `/subscribed_apps` request.

## Active campaign

- Campaign: `salad_dressing_label_guide_v1`
- Platforms: Instagram and Facebook
- Keyword: `LABEL`
- Media selection: wildcard until the first published Reel IDs are available
- Asset: `https://storage.googleapis.com/ai-content-factory-501821-omni-outputs/cta-assets/salad-dressing-label-guide-v1.pdf`

The asset is publicly readable and returns `Content-Type: application/pdf`.

## Verified behavior

- Cloud Run health: HTTP 200
- Correct webhook verification token: HTTP 200
- Incorrect verification token: HTTP 403
- Correctly signed nonmatching Instagram event: HTTP 200, `ignored_no_match`
- Unit tests: both Instagram payload shapes, Facebook feed comments, signatures, keyword routing, and duplicate suppression

The first true private-reply test requires a comment from a separate Instagram/Facebook user. Do not test from the connected professional account because self-comments are intentionally ignored.
