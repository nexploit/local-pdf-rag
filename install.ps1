$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== Local PDF RAG 1.1.0 Installer ==="

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI wurde nicht gefunden. Bitte Docker Desktop installieren/starten."
}

try {
    & docker info *> $null
} catch {
    throw "Docker Engine ist nicht erreichbar. Docker Desktop/WSL2 prüfen und danach install.ps1 erneut ausführen."
}
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine ist nicht erreichbar. Docker Desktop/WSL2 prüfen und danach install.ps1 erneut ausführen."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[OK] .env wurde aus Vorlage erstellt."
}

@("data\incoming","data\processed","data\failed","data\metadata","logs","qdrant") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}

Write-Host "[1/3] Docker Images bauen/holen ..."
& docker compose pull qdrant
if ($LASTEXITCODE -ne 0) { throw "Qdrant Image konnte nicht geladen werden." }

& docker compose build pdf-rag
if ($LASTEXITCODE -ne 0) { throw "pdf-rag Image konnte nicht gebaut werden." }

Write-Host "[2/3] Container starten ..."
& docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "Container konnten nicht gestartet werden." }

Write-Host "[3/3] Status ..."
& docker compose ps

Write-Host ""
Write-Host "Installation abgeschlossen."
Write-Host "Web UI       : http://localhost:8787/"
Write-Host "API / Swagger: http://localhost:8787/docs"
Write-Host "OpenAI API   : http://localhost:8787/v1"
Write-Host "Qdrant UI    : http://localhost:6333/dashboard"
Write-Host "PDF-Eingang  : $Root\data\incoming"
Write-Host ""
Write-Host "LM Studio: Local Server auf Port 1234 aktivieren."
