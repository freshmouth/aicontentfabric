param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Project = "ai-content-factory-501821",
    [string]$Region = "us-central1"
)

$ErrorActionPreference = "Stop"
$Image = "$Region-docker.pkg.dev/$Project/ai-content-factory/daily-factory:$Tag"
gcloud.cmd run jobs update ai-content-daily-factory --project $Project --region $Region --image $Image --set-env-vars "DAILY_FACTORY_RELEASE=$Tag,DAILY_FACTORY_DRY_RUN=false"
Write-Output "Rolled back daily factory to $Image"
