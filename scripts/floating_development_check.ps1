param([Parameter(Mandatory=$true)][string]$InputPdf)
$ErrorActionPreference = 'Stop'
$orcaRoot = Split-Path $PSScriptRoot -Parent
$orcaPython = Join-Path (Split-Path $orcaRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
$env:PDFMT_DATA_DIR = Join-Path $orcaRoot 'test-data\orca-floating-development'
$env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'floating-test.json'
$env:PDFMT_TEST_INPUT = $InputPdf
$env:PYTHONPATH = Join-Path $orcaRoot 'vendor'
$env:PYTHONUTF8 = '1'
$orcaProcess = Start-Process -FilePath $orcaPython -ArgumentList @(('"' + (Join-Path $orcaRoot 'app.py') + '"'),'--floating-test') -WorkingDirectory $orcaRoot -WindowStyle Hidden -PassThru -Wait
if (-not (Test-Path -LiteralPath $env:PDFMT_REPORT)) { throw 'Floating-window report was not produced.' }
$orcaText = Get-Content -LiteralPath $env:PDFMT_REPORT -Encoding UTF8 -Raw
Write-Output $orcaText
if (-not ($orcaText | ConvertFrom-Json).ok) { exit 1 }
exit $orcaProcess.ExitCode
