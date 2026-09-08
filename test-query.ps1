param([string]$Question = "Welche Informationen gibt es in meiner Sammlung zu Linux Hardening?")
$body = @{ question = $Question; use_llm = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8787/ask" -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8
