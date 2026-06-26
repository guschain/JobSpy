$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "workspace/config.json")) {
  uv run job-finger init
}
if (-not (Test-Path "workspace/cv.pdf") -and -not ($args -contains "--input")) {
  throw "Put your CV PDF at workspace/cv.pdf, or pass --input path\to\cv.pdf"
}
uv run job-finger cv --config workspace/config.json @args
