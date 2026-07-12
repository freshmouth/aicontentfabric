# Meta Comment-to-DM Automation

This isolated service watches Instagram and Facebook comments, matches a campaign keyword, sends one private reply containing the campaign asset, and optionally posts a public acknowledgement. It runs on Google Cloud Run, so the local computer can be off.

The checked-in `campaigns.json` is deliberately disabled. The service must never go live with a placeholder asset URL.

## Runtime flow

1. Meta sends a signed comment webhook to `/webhook`.
2. The service verifies `X-Hub-Signature-256` with `META_APP_SECRET`.
3. The comment is matched by platform, Reel/Post ID, and keyword.
4. Firestore claims the comment ID so duplicate webhook deliveries cannot duplicate DMs.
5. Instagram uses the Private Replies Send API; Facebook uses the comment `private_replies` edge.
6. The private message is sent first. A public acknowledgement is attempted afterward.
7. The delivery result is stored in the `meta_comment_deliveries` Firestore collection.

## Required Meta permissions

For Instagram with Instagram Login, request `instagram_business_basic` and `instagram_business_manage_comments`. With Facebook Login, use `instagram_basic`, `instagram_manage_comments`, and `pages_read_engagement`. Facebook Page private replies require Page access plus `pages_messaging`; Page comment events typically also require `pages_manage_metadata`, `pages_read_engagement`, and `pages_manage_engagement`.

Standard Access can serve professional accounts owned or managed by the app owner and added in the App Dashboard. Advanced Access and App Review are required when serving accounts outside that set.

## Configure the campaign

Copy the structure in `campaigns.example.json` into `campaigns.json` and enable it only after replacing:

- Instagram Reel media ID
- Facebook post/Reel ID
- trigger keywords
- public acknowledgement
- private message
- public HTTPS PDF or image URL

Use `["*"]` only when a keyword should trigger on every post for that account. A specific media ID is safer.

Validate before deploying:

```powershell
python -m social_automation.manage validate
python -m social_automation.manage simulate --platform instagram --media-id YOUR_MEDIA_ID --comment "LABEL"
```

## Local test

Install dependencies and run the tests:

```powershell
python -m pip install --user -r social_automation\requirements.txt
python -m unittest discover -s social_automation\tests -v
```

Generate the example CTA PDF with:

```powershell
python -m pip install --user -r social_automation\assets\requirements.txt
python social_automation\assets\generate_label_guide.py
```

For a local server, use JSON deduplication and unsigned webhooks only for local testing:

```powershell
$env:DELIVERY_STORE="json"
$env:ALLOW_UNSIGNED_WEBHOOKS="1"
$env:CAMPAIGNS_FILE="social_automation/campaigns.json"
python -m flask --app social_automation.service run --port 8080
```

Never set `ALLOW_UNSIGNED_WEBHOOKS=1` in Cloud Run.

## Google Cloud secrets

Create these Secret Manager secrets. Do not commit their values:

- `meta-app-secret`: Meta App Dashboard > App settings > Basic > App secret
- `meta-webhook-verify-token`: a long random value you choose
- `instagram-access-token`: Instagram professional-account token
- `facebook-page-access-token`: Facebook Page token

Example PowerShell commands, entered from the repository root:

```powershell
$value = Read-Host "Secret value"
$value | gcloud secrets create meta-app-secret --data-file=-
```

For an existing secret, add a new version:

```powershell
$value = Read-Host "Secret value"
$value | gcloud secrets versions add meta-app-secret --data-file=-
```

Repeat for all four secret names. Avoid putting token values directly in command history.

Create a Firestore Native database once in the `us-central1` region from Google Cloud Console, then deploy:

```powershell
powershell -ExecutionPolicy Bypass -File social_automation\deploy_cloud_run.ps1
```

The script submits only the small `social_automation` directory, builds the container, creates a dedicated service account, grants only Firestore and Secret Manager access, deploys with zero minimum instances, and prints the callback URL.

## Meta webhook configuration

In the Meta App Dashboard:

1. Add the Webhooks and Instagram products used by the app.
2. Set callback URL to `https://YOUR_CLOUD_RUN_URL/webhook`.
3. Enter the exact value stored as `meta-webhook-verify-token`.
4. Subscribe Instagram to `comments` and, if needed, `live_comments`.
5. Subscribe the Facebook Page webhook to `feed`.
6. Subscribe the connected professional account/Page to the app.
7. Put the app in Live mode once the required permissions are approved.

Meta must receive HTTP 200 from webhook verification. Invalid signatures receive 401. Failed private deliveries receive 503 so Meta can retry; successful or irrelevant comments receive 200.

## Safety defaults

- One campaign delivery per comment ID.
- No DM when the keyword or media ID does not match.
- No reply to comments authored by the connected Page/account.
- Private delivery happens before the public “sent” reply.
- Tokens and App Secret are never written to logs.
- A failed public acknowledgement does not resend an already-delivered private reply.
- Keep PDF/image URLs public and stable, or use a landing URL that does not expire.
