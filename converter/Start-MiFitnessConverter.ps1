[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host 'Python 3 was not found. Install Python 3, then run this launcher again.' -ForegroundColor Red
    Read-Host 'Press Enter to exit'
    exit 1
}

Write-Host 'Mi Fitness local converter' -ForegroundColor Cyan
Write-Host 'This reads only the export directory you choose. It does not upload data or modify source CSV files.'
$inputPath = Read-Host 'Paste the Mi Fitness export folder path'
if (-not (Test-Path -LiteralPath $inputPath -PathType Container)) { throw 'The export directory does not exist or is not a folder.' }
$outputFolder = Read-Host 'Paste an existing local folder for JSON and reports'
if (-not (Test-Path -LiteralPath $outputFolder -PathType Container)) { throw 'The output directory does not exist or is not a folder.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dryReport = Join-Path $outputFolder "mi-fitness-dry-run-report-$stamp.json"
Write-Host "`nRunning a check without printing health values..." -ForegroundColor Yellow
& $python.Source (Join-Path $toolRoot 'convert.py') --input $inputPath --dry-run --report $dryReport
if ($LASTEXITCODE -ne 0) { throw 'The check did not complete. Review the error counts shown above.' }
Write-Host "Check report saved: $dryReport" -ForegroundColor Green

$answer = Read-Host 'Generate an import JSON from this check? Enter Y to continue; anything else exits'
if ($answer -notmatch '^[Yy]$') { Write-Host 'Exited without generating an import JSON.'; exit 0 }

$mode = Read-Host 'Default: a one-record trial package without displaying health values. Enter F only for a full package'
$maxRecordArgs = @('--max-records', '1')
$packageName = "mi-fitness-trial-import-$stamp.json"
if ($mode -match '^[Ff]$') {
    $maxRecordArgs = @()
    $packageName = "mi-fitness-full-import-$stamp.json"
    Write-Host 'Full package selected. Health values will still not be displayed.' -ForegroundColor Yellow
} else {
    Write-Host 'Creating a trial package with one validated, deterministically selected record. Health values will not be displayed.' -ForegroundColor Yellow
}

$package = Join-Path $outputFolder $packageName
$generatedReport = Join-Path $outputFolder "mi-fitness-generated-report-$stamp.json"
& $python.Source (Join-Path $toolRoot 'convert.py') --input $inputPath --output $package --report $generatedReport @maxRecordArgs
if ($LASTEXITCODE -ne 0) { throw 'JSON was not generated. Review the error information shown above.' }
Write-Host "`nLocal import JSON generated: $package" -ForegroundColor Green
Write-Host 'Before importing, review the preview and validation results in the iPhone app.'
Read-Host 'Press Enter to exit'
