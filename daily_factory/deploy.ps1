param(
    [string]$Project = "ai-content-factory-501821",
    [string]$Region = "us-central1",
    [string]$ServiceAccount = "daily-factory@$Project.iam.gserviceaccount.com",
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmmss")
)

$ErrorActionPreference = "Stop"
$env:CLOUDSDK_AUTH_DISABLE_SSL_VALIDATION = "True"
$Image = "$Region-docker.pkg.dev/$Project/ai-content-factory/daily-factory:$Tag"

gcloud.cmd builds submit . --project $Project --config daily_factory/cloudbuild.yaml --ignore-file daily_factory/.gcloudignore --substitutions "_REGION=$Region,_TAG=$Tag"
gcloud.cmd run jobs deploy ai-content-daily-factory --project $Project --region $Region --image $Image --service-account $ServiceAccount --task-timeout 7200s --max-retries 1 --cpu 2 --memory 4Gi --set-env-vars "DAILY_FACTORY_RELEASE=$Tag,GOOGLE_CLOUD_PROJECT=$Project" --set-secrets "INSTAGRAM_ACCESS_TOKEN=instagram-access-token:latest,FACEBOOK_PAGE_ACCESS_TOKEN=facebook-page-access-token:latest,OPENAI_API_KEY=openai-api-key:latest"

gcloud.cmd run jobs update ai-content-daily-factory --project $Project --region $Region --update-env-vars "DAILY_FACTORY_DRY_RUN=true"
gcloud.cmd run jobs execute ai-content-daily-factory --project $Project --region $Region --wait
gcloud.cmd run jobs update ai-content-daily-factory --project $Project --region $Region --update-env-vars "DAILY_FACTORY_DRY_RUN=false"

$JobUri = "https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$Project/jobs/ai-content-daily-factory:run"
gcloud.cmd scheduler jobs describe ai-content-daily-0913 --project $Project --location $Region 2>$null
if ($LASTEXITCODE -eq 0) {
    gcloud.cmd scheduler jobs update http ai-content-daily-0913 --project $Project --location $Region --schedule "13 9 * * *" --time-zone "America/Mexico_City" --uri $JobUri --http-method POST --oauth-service-account-email $ServiceAccount --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" --headers "Content-Type=application/json" --message-body "{}" --attempt-deadline 5m
} else {
    gcloud.cmd scheduler jobs create http ai-content-daily-0913 --project $Project --location $Region --schedule "13 9 * * *" --time-zone "America/Mexico_City" --uri $JobUri --http-method POST --oauth-service-account-email $ServiceAccount --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" --headers "Content-Type=application/json" --message-body "{}" --attempt-deadline 5m
}

Write-Output "Deployed $Image"
