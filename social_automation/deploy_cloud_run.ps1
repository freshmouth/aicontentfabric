param(
    [string]$ProjectId = "ai-content-factory-501821",
    [string]$Region = "us-central1",
    [string]$Service = "meta-comment-automation",
    [string]$InstagramUserId = "28067143496203803",
    [string]$FacebookPageId = "1164431006757522"
)

$ErrorActionPreference = "Stop"
$env:CLOUDSDK_AUTH_DISABLE_SSL_VALIDATION = "True"
$repository = "ai-content-factory"
$image = "$Region-docker.pkg.dev/$ProjectId/$repository/$Service`:latest"
$serviceAccountName = "meta-comment-automation"
$serviceAccount = "$serviceAccountName@$ProjectId.iam.gserviceaccount.com"

gcloud.cmd config set project $ProjectId | Out-Null

$repoExists = gcloud.cmd artifacts repositories describe $repository --location $Region --format "value(name)" 2>$null
if (-not $repoExists) {
    gcloud.cmd artifacts repositories create $repository --repository-format docker --location $Region
}

$accountExists = gcloud.cmd iam service-accounts describe $serviceAccount --format "value(email)" 2>$null
if (-not $accountExists) {
    gcloud.cmd iam service-accounts create $serviceAccountName --display-name "Meta comment automation"
}

gcloud.cmd projects add-iam-policy-binding $ProjectId --member "serviceAccount:$serviceAccount" --role "roles/datastore.user" --quiet | Out-Null
gcloud.cmd projects add-iam-policy-binding $ProjectId --member "serviceAccount:$serviceAccount" --role "roles/secretmanager.secretAccessor" --quiet | Out-Null

gcloud.cmd builds submit social_automation --config social_automation/cloudbuild.yaml --substitutions "_REGION=$Region,_SERVICE=$Service"

gcloud.cmd run deploy $Service `
    --image $image `
    --region $Region `
    --platform managed `
    --service-account $serviceAccount `
    --allow-unauthenticated `
    --min-instances 0 `
    --max-instances 5 `
    --concurrency 20 `
    --timeout 120 `
    --set-env-vars "META_GRAPH_VERSION=v23.0,INSTAGRAM_USER_ID=$InstagramUserId,FACEBOOK_PAGE_ID=$FacebookPageId,DELIVERY_STORE=firestore,CAMPAIGNS_FILE=social_automation/campaigns.json" `
    --set-secrets "META_APP_SECRET=meta-app-secret:latest,META_WEBHOOK_VERIFY_TOKEN=meta-webhook-verify-token:latest,INSTAGRAM_ACCESS_TOKEN=instagram-access-token:latest,FACEBOOK_PAGE_ACCESS_TOKEN=facebook-page-access-token:latest"

$url = gcloud.cmd run services describe $Service --region $Region --format "value(status.url)"
Write-Host "Health: $url/health"
Write-Host "Meta callback URL: $url/webhook"
