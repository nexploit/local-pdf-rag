param(
    [string]$Registry = "docker.example.internal",
    [string]$Repository = "local-pdf-rag",
    [string]$Version = "1.1.0"
)
$ErrorActionPreference = "Stop"
$LocalImage = "local-pdf-rag:1.1.0"
$VersionTag = "$Registry/${Repository}:$Version"
$LatestTag = "$Registry/${Repository}:latest"

Write-Host "Prüfe lokales Image $LocalImage ..."
& docker image inspect $LocalImage *> $null
if ($LASTEXITCODE -ne 0) { throw "Image $LocalImage wurde nicht gefunden. Zuerst install.ps1 oder 'docker compose build' ausführen." }

Write-Host "Tagge $VersionTag und $LatestTag ..."
& docker tag $LocalImage $VersionTag
& docker tag $LocalImage $LatestTag

Write-Host "Push Version ..."
& docker push $VersionTag
if ($LASTEXITCODE -ne 0) { throw "Push von $VersionTag fehlgeschlagen." }

Write-Host "Push latest ..."
& docker push $LatestTag
if ($LASTEXITCODE -ne 0) { throw "Push von $LatestTag fehlgeschlagen." }

Write-Host "Fertig:"
Write-Host "  $VersionTag"
Write-Host "  $LatestTag"
