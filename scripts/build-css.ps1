# scripts/build-css.ps1
# Compile Tailwind CSS on Windows. Downloads the standalone tailwindcss binary
# on first run (~30 MB, cached in .tailwind/) — no Node required.
#
# Usage:
#   .\scripts\build-css.ps1              # one-shot minified build
#   .\scripts\build-css.ps1 -Watch       # rebuild on template change

param(
    [switch]$Watch,
    [string]$TailwindVersion = '3.4.15'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$binDir = Join-Path $root '.tailwind'
$binPath = Join-Path $binDir 'tailwindcss.exe'

if (-not (Test-Path $binPath)) {
    Write-Host "Downloading standalone tailwindcss v$TailwindVersion..."
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $url = "https://github.com/tailwindlabs/tailwindcss/releases/download/v$TailwindVersion/tailwindcss-windows-x64.exe"
    Invoke-WebRequest -Uri $url -OutFile $binPath -UseBasicParsing
    Write-Host "Cached at $binPath"
}

$cliArgs = @(
    '-c', (Join-Path $root 'tailwind.config.js'),
    '-i', (Join-Path $root 'src/docgen/ui/tailwind.src.css'),
    '-o', (Join-Path $root 'src/docgen/ui/static/app.css')
)
if ($Watch) {
    $cliArgs += '--watch'
} else {
    $cliArgs += '--minify'
}

& $binPath @cliArgs
