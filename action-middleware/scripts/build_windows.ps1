$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Invoke-Step "Generating branded icons..." { python scripts/generate_icons.py }

Invoke-Step "Ensuring PyInstaller is available..." { python -m pip install pyinstaller }

Invoke-Step "Building ActionFlow Windows desktop package..." { python -m PyInstaller packaging/ActionFlow.spec --noconfirm --clean }

Write-Host ""
Write-Host "Build complete."
Write-Host "Output folder: $root\\dist\\ActionFlow"
Write-Host "Executable:    $root\\dist\\ActionFlow\\ActionFlow.exe"
