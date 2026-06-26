$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "workspace/config.json")) {
  uv run job-finger init
}
uv run job-finger ui --config workspace/config.json @args

