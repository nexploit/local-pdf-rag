Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
docker compose up -d
docker compose ps
