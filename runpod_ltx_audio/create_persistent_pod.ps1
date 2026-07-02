param(
    [string]$Name = "ltx23-audio-persistent",
    [string]$ImageName = "ghcr.io/freshmouth/ltx2-audio-worker:ae52b490649cf1d6fd8e911c5bd212e8f84ec332",
    [string[]]$GpuTypeIds = @("NVIDIA H100 80GB HBM3", "NVIDIA H100 PCIe", "NVIDIA A100 80GB PCIe", "NVIDIA A100-SXM4-80GB", "NVIDIA L40S"),
    [string[]]$DataCenterIds = @(),
    [string]$NetworkVolumeId = $env:RUNPOD_LTX_NETWORK_VOLUME_ID,
    [int]$VolumeGb = 250,
    [int]$ContainerDiskGb = 40,
    [int]$MinRamPerGpu = 64,
    [int]$MinVcpuPerGpu = 8,
    [switch]$Interruptible,
    [switch]$Create
)

$ErrorActionPreference = "Stop"

function Get-RunpodApiKey {
    if ($env:RUNPOD_API_KEY -and $env:RUNPOD_API_KEY.Trim()) {
        return $env:RUNPOD_API_KEY.Trim()
    }

    $configPath = Join-Path $HOME ".runpod\config.toml"
    if (Test-Path -LiteralPath $configPath) {
        $raw = Get-Content -LiteralPath $configPath -Raw
        $match = [regex]::Match($raw, 'api_key\s*=\s*"([^"]+)"')
        if ($match.Success) {
            return $match.Groups[1].Value.Trim()
        }
    }

    throw "RUNPOD_API_KEY is missing. Set it in the environment or log in with RunPod tooling first."
}

function Get-HfToken {
    if ($env:HF_TOKEN -and $env:HF_TOKEN.Trim().StartsWith("hf_")) {
        return $env:HF_TOKEN.Trim()
    }
    throw "HF_TOKEN is missing or invalid. Set HF_TOKEN before creating the pod."
}

function New-PodBody {
    param([string]$HfToken)

    $envVars = @{
        RUNPOD_SERVERLESS = "0"
        HF_TOKEN = $HfToken
        MODEL_ROOT = "/workspace/models"
        HF_HOME = "/workspace/models/huggingface"
        LTX2_MODEL_PATH = "/workspace/models/ltx2"
        OUTPUT_ROOT = "/workspace/outputs"
        MAX_INLINE_OUTPUT_BYTES = "160000000"
        LTX2_GENERATION_RESOLUTION = "1088x1920"
        LTX2_FRAME_RATE = "25"
        LTX2_NUM_INFERENCE_STEPS = "30"
        LTX2_A2V_GUIDANCE_SCALE = "2.0"
        LTX2_VIDEO_CFG_GUIDANCE_SCALE = "3.0"
        LTX2_QUANTIZATION = "fp8-cast"
        LTX2_OFFLOAD = "none"
    }

    $body = @{
        name = $Name
        computeType = "GPU"
        cloudType = "SECURE"
        gpuTypeIds = $GpuTypeIds
        gpuTypePriority = "availability"
        gpuCount = 1
        imageName = $ImageName
        containerDiskInGb = $ContainerDiskGb
        volumeMountPath = "/workspace"
        ports = @("8000/http", "22/tcp")
        env = $envVars
        minRAMPerGPU = $MinRamPerGpu
        minVCPUPerGPU = $MinVcpuPerGpu
        allowedCudaVersions = @("12.8", "12.7", "12.6", "12.5", "12.4")
        globalNetworking = $true
        interruptible = [bool]$Interruptible
    }

    if ($NetworkVolumeId) {
        $body.networkVolumeId = $NetworkVolumeId
    } else {
        $body.volumeInGb = $VolumeGb
    }
    if ($DataCenterIds.Count -gt 0) {
        $body.dataCenterIds = $DataCenterIds
        $body.dataCenterPriority = "availability"
    }

    return $body
}

function Copy-RedactedBody {
    param([hashtable]$Body)

    $json = $Body | ConvertTo-Json -Depth 20
    $copy = $json | ConvertFrom-Json
    if ($copy.env.HF_TOKEN) {
        $copy.env.HF_TOKEN = "hf_***redacted***"
    }
    return $copy
}

$apiKey = Get-RunpodApiKey
$hfToken = Get-HfToken
$body = New-PodBody -HfToken $hfToken
$redacted = Copy-RedactedBody -Body $body

if (-not $Create) {
    Write-Host "Dry run only. Add -Create to launch the paid RunPod pod."
    $redacted | ConvertTo-Json -Depth 20
    exit 0
}

Write-Host "Creating paid RunPod GPU pod '$Name'..."
$headers = @{
    Authorization = "Bearer $apiKey"
    "Content-Type" = "application/json"
    Accept = "application/json"
}
$response = Invoke-RestMethod `
    -Method Post `
    -Uri "https://rest.runpod.io/v1/pods" `
    -Headers $headers `
    -Body ($body | ConvertTo-Json -Depth 20) `
    -TimeoutSec 120

[pscustomobject]@{
    id = $response.id
    name = $response.name
    desiredStatus = $response.desiredStatus
    imageName = $response.imageName
    gpuCount = $response.gpuCount
    gpuTypeId = $response.gpuTypeId
    machineId = $response.machineId
} | ConvertTo-Json -Depth 5
