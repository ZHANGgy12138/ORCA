$ErrorActionPreference = 'Stop'
$orcaRoot = Split-Path $PSScriptRoot -Parent
$orcaPython = Join-Path (Split-Path $orcaRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
$env:PDFMT_DATA_DIR = Join-Path $orcaRoot 'test-data\orca-ui-development'
$env:PYTHONPATH = Join-Path $orcaRoot 'vendor'
$env:PYTHONUTF8 = '1'
$orcaProcess = Start-Process -FilePath $orcaPython -ArgumentList @(('"' + (Join-Path $orcaRoot 'app.py') + '"'),'--ui-test') -WorkingDirectory $orcaRoot -WindowStyle Hidden -PassThru -Wait
Get-Content -LiteralPath (Join-Path $env:PDFMT_DATA_DIR 'ui-test.json') -Encoding UTF8
exit $orcaProcess.ExitCode
