param(
    [string]$Image = $env:APP_IMAGE
)

if (-not $Image) {
    Write-Error "Set APP_IMAGE or pass -Image, for example ghcr.io/owner/repo:20260522"
    exit 2
}

$localId = docker image inspect $Image --format '{{.Id}}' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "local: missing"
} else {
    Write-Host "local: $localId"
}

docker pull $Image | Out-Host
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$remoteId = docker image inspect $Image --format '{{.Id}}'
Write-Host "after-pull: $remoteId"

if ($localId -and $localId -ne $remoteId) {
    Write-Host "update: available and pulled"
    exit 10
}

Write-Host "update: none"
