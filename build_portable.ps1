$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot "venv"
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$devRequirementsPath = Join-Path $projectRoot "requirements-dev.txt"
$portableRoot = Join-Path (Split-Path $projectRoot -Parent) "Brents Budgeting App Portable"
$buildRoot = Join-Path $projectRoot "build"
$distRoot = Join-Path $projectRoot "dist"
$specPath = Join-Path $projectRoot "portable_launcher.spec"
$distAppRoot = Join-Path $distRoot "BrentsBudgetingAppPortable"
$readmePath = Join-Path $projectRoot "README_PORTABLE.txt"
$blankDbPath = Join-Path $portableRoot "budget.sqlite"
$iconPngPath = Join-Path $projectRoot "Brent_WallStreet.png"
$iconIcoPath = Join-Path $projectRoot "Brent_WallStreet.ico"

function New-IcoFromPng {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePng,
        [Parameter(Mandatory = $true)]
        [string]$OutputIco
    )

    Add-Type -AssemblyName System.Drawing

    $sourceImage = [System.Drawing.Image]::FromFile($SourcePng)
    $iconSizes = @(256, 128, 64, 48, 32, 16)
    $pngImages = New-Object 'System.Collections.Generic.List[byte[]]'

    try {
        foreach ($size in $iconSizes) {
            $bitmap = New-Object System.Drawing.Bitmap $size, $size
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $stream = New-Object System.IO.MemoryStream

            try {
                $graphics.Clear([System.Drawing.Color]::Transparent)
                $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                $graphics.DrawImage($sourceImage, 0, 0, $size, $size)
                $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
                $pngImages.Add($stream.ToArray())
            }
            finally {
                $stream.Dispose()
                $graphics.Dispose()
                $bitmap.Dispose()
            }
        }
    }
    finally {
        $sourceImage.Dispose()
    }

    $outputStream = [System.IO.File]::Create($OutputIco)
    $writer = New-Object System.IO.BinaryWriter $outputStream

    try {
        $writer.Write([UInt16]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]$pngImages.Count)

        $offset = 6 + (16 * $pngImages.Count)
        for ($index = 0; $index -lt $pngImages.Count; $index++) {
            $pngImage = $pngImages[$index]
            $size = $iconSizes[$index]
            $sizeByte = if ($size -eq 256) { 0 } else { $size }
            $writer.Write([byte]$sizeByte)
            $writer.Write([byte]$sizeByte)
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([UInt16]1)
            $writer.Write([UInt16]32)
            $writer.Write([UInt32]$pngImage.Length)
            $writer.Write([UInt32]$offset)
            $offset += $pngImage.Length
        }

        foreach ($pngImage in $pngImages) {
            $writer.Write($pngImage)
        }
    }
    finally {
        $writer.Dispose()
        $outputStream.Dispose()
    }
}

if (-not (Test-Path $pythonExe)) {
    Write-Host "Creating project virtual environment..."
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        & py -3 -m venv $venvPath
    }
    else {
        $systemPython = Get-Command python -ErrorAction SilentlyContinue
        if (-not $systemPython) {
            throw "Python was not found. Install Python 3, then rerun this script."
        }
        & $systemPython.Source -m venv $venvPath
    }

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pythonExe)) {
        throw "Virtual environment creation failed."
    }
}

if (-not (Test-Path $iconPngPath)) {
    throw "Launcher icon image was not found at $iconPngPath"
}

Write-Host "Installing Python dependencies..."
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed."
}
& $pythonExe -m pip install -r $requirementsPath -r $devRequirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

Write-Host "Preparing launcher icon..."
New-IcoFromPng -SourcePng $iconPngPath -OutputIco $iconIcoPath

Write-Host "Checking PyInstaller..."
& $pythonExe -m pip show pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller into the project venv..."
    & $pythonExe -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller installation failed."
    }
}

foreach ($path in @($buildRoot, $distRoot, $portableRoot)) {
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

Write-Host "Building portable launcher..."
& $pythonExe -m PyInstaller --noconfirm --clean $specPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host "Creating portable output folder..."
Copy-Item -Path $distAppRoot -Destination $portableRoot -Recurse

Write-Host "Creating blank starter database..."
& $pythonExe (Join-Path $projectRoot "create_blank_db.py") $blankDbPath | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Blank portable database creation failed."
}

Write-Host "Writing portable README..."
@"
Brent's Budgeting App Portable
==============================

1. Double-click BrentsBudgetingAppPortable.exe to launch the app.
2. Your portable data lives beside the EXE in budget.sqlite.
3. Templates, static assets, and dependencies are bundled into this folder for portable use.

Notes
-----
- This copy starts with a blank database that already has the current schema.
- Keep the whole folder together when moving it to another machine or USB drive.
- The launcher opens the app in your default web browser and keeps a small control window open so you can close the server cleanly.
"@ | Set-Content -Path $readmePath -Encoding UTF8

Copy-Item -Path $readmePath -Destination (Join-Path $portableRoot "README_PORTABLE.txt")

Write-Host ""
Write-Host "Portable build created at:"
Write-Host "  $portableRoot"
