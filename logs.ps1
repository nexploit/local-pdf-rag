Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
docker compose logs -f pdf-rag
