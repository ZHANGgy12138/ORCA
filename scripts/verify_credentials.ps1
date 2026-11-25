param([switch]$Source)
$ErrorActionPreference = 'Stop'
$orcaRoot = Split-Path $PSScriptRoot -Parent
$orcaToken = [guid]::NewGuid().ToString('N')
$env:ORCA_TEST_CREDENTIAL_NAMESPACE = 'ORCA/credential-test/' + $orcaToken
$env:PYTHONUTF8 = '1'
$orcaMode = if ($Source) { 'source' } else { 'frozen' }
$env:PDFMT_DATA_DIR = Join-Path $orcaRoot ('test-data\credentials-' + $orcaMode + '\' + $orcaToken)
if ($Source) {
    $orcaExecutable = Join-Path (Split-Path $orcaRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
    $orcaPrefix = @(('"' + (Join-Path $orcaRoot 'app.py') + '"'))
    $env:PYTHONPATH = Join-Path $orcaRoot 'vendor'
} else {
    $orcaStamp = (Get-Item -LiteralPath (Join-Path $orcaRoot 'dist\ORCA-0.9.1\ORCA.exe')).LastWriteTimeUtc.Ticks
    $orcaCopyParent = Join-Path $orcaRoot ('build\portable test\' + $orcaStamp)
    $orcaCopy = Join-Path $orcaCopyParent 'ORCA-0.9.1'
    if (-not (Test-Path -LiteralPath (Join-Path $orcaCopy 'ORCA.exe'))) {
        New-Item -ItemType Directory -Path $orcaCopyParent -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $orcaRoot 'dist\ORCA-0.9.1') -Destination $orcaCopyParent -Recurse -Force
    }
    $orcaExecutable = Join-Path $orcaCopy 'ORCA.exe'
    $orcaPrefix = @()
    $env:PYTHONPATH = $null
    $env:PATH = $env:SystemRoot + '\system32;' + $env:SystemRoot
}
try {
    foreach ($orcaStage in @(1, 2)) {
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR ('credential-stage-' + $orcaStage + '.json')
        $orcaArguments = @($orcaPrefix) + @('--credential-ui-test-stage' + $orcaStage)
        $orcaProcess = Start-Process -FilePath $orcaExecutable -ArgumentList $orcaArguments -WorkingDirectory $orcaRoot -WindowStyle Hidden -Wait -PassThru
        if (-not (Test-Path -LiteralPath $env:PDFMT_REPORT)) { throw 'Credential UI report missing.' }
        $orcaReport = Get-Content -LiteralPath $env:PDFMT_REPORT -Raw -Encoding UTF8
        Write-Output $orcaReport
        if (-not ($orcaReport | ConvertFrom-Json).ok -or $orcaProcess.ExitCode -ne 0) { throw 'Credential UI verification failed.' }
    }
} finally {
    $orcaCleanupArguments = @($orcaPrefix) + @('--credential-cleanup-test')
    $orcaCleanup = Start-Process -FilePath $orcaExecutable -ArgumentList $orcaCleanupArguments -WorkingDirectory $orcaRoot -WindowStyle Hidden -Wait -PassThru
    if ($orcaCleanup.ExitCode -ne 0) { throw 'Isolated credential cleanup failed.' }
}
