param(
  [int]$Port = 8765,
  [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "workspace/config.json")) {
  uv run job-finger init
}
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
  Write-Host "Job Finger UI already running at http://$HostName`:$Port/"
  return
}
uv run job-finger ui --config workspace/config.json --host $HostName --port $Port @args
