param([Parameter(Mandatory=$true)][string]$InputPdf, [string]$Pages = '7', [switch]$Reader)
$ErrorActionPreference = 'Stop'
$orcaRoot = Split-Path $PSScriptRoot -Parent
$orcaPython = Join-Path (Split-Path $orcaRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $orcaRoot 'vendor'
$env:PYTHONUTF8 = '1'
$env:PDFMT_DATA_DIR = Join-Path $orcaRoot 'test-data\orca-v090-index-development'
$env:PDFMT_TEST_INPUT = $InputPdf
$env:PDFMT_TEST_ENGINE = 'indextranslate'
$env:PDFMT_TEST_PAGES = $Pages
$orcaMode = if ($Reader) { 'reader' } else { 'index' }
$env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR ($orcaMode + '-test.json')
$orcaProcess = Start-Process -FilePath $orcaPython -ArgumentList @(('"' + (Join-Path $orcaRoot 'app.py') + '"'),('--' + $orcaMode + '-test')) -WorkingDirectory $orcaRoot -WindowStyle Hidden -PassThru -Wait
if (-not (Test-Path -LiteralPath $env:PDFMT_REPORT)) { throw 'Expected Index report was not produced.' }
$orcaText = Get-Content -LiteralPath $env:PDFMT_REPORT -Encoding UTF8 -Raw
Write-Output $orcaText
if (-not ($orcaText | ConvertFrom-Json).ok) { exit 1 }
exit $orcaProcess.ExitCode
