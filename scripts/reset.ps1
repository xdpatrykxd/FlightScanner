param([switch]$Yes)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Yes) {
    Write-Host "This clears AirScout's saved trip, scan history, and alert deduplication."
    Write-Host "Back up data/listings.sqlite3 first if you need the existing records."
    Write-Host "Run: .\scripts\reset.ps1 -Yes"
    exit 0
}
Set-Location $Root
docker compose stop airscout
Remove-Item -Force -ErrorAction SilentlyContinue data/listings.sqlite3, data/listings.sqlite3-shm, data/listings.sqlite3-wal
docker compose up -d --build airscout
Write-Host "AirScout database reset. Create a new trip from http://localhost:8080."
