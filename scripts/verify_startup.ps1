param([switch]$Source, [switch]$Retry)
$ErrorActionPreference = 'Stop'
$orcaRoot = Split-Path $PSScriptRoot -Parent
$orcaMode = if ($Source) { 'source' } else { 'frozen' }
$orcaScenario = if ($Retry) { 'retry' } else { 'normal' }
$orcaToken = [guid]::NewGuid().ToString('N')
$env:PDFMT_DATA_DIR = Join-Path $orcaRoot ('test-data\startup-' + $orcaMode + '\' + $orcaScenario + '-' + $orcaToken)
New-Item -ItemType Directory -Path $env:PDFMT_DATA_DIR -Force | Out-Null
$env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'startup-test.json'
$env:PDFMT_CACHE_DIR = Join-Path $env:PDFMT_DATA_DIR 'cache\babeldoc'
$env:ORCA_STARTUP_TEST_DELAY = if ($Retry) { '3' } else { '0' }
$env:ORCA_STARTUP_TEST_FAIL_ONCE = if ($Retry) { '1' } else { '0' }
$env:PYTHONUTF8 = '1'
[System.IO.File]::WriteAllText((Join-Path $env:PDFMT_DATA_DIR 'preferences.v2.json'),'{"preferences":{"theme":"dark","engine":"bing","engine_settings":{}},"history":[]}',[System.Text.UTF8Encoding]::new($false))
if ($Source) {
    $orcaExe = Join-Path (Split-Path $orcaRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
    $orcaArgs = @(('"' + (Join-Path $orcaRoot 'app.py') + '"'))
    $env:PYTHONPATH = Join-Path $orcaRoot 'vendor'
    $orcaWork = $orcaRoot
} else {
    $orcaStamp = (Get-Item -LiteralPath (Join-Path $orcaRoot 'dist\ORCA-0.9.1\ORCA.exe')).LastWriteTimeUtc.Ticks
    $orcaParent = Join-Path $orcaRoot ('build\portable test\' + $orcaStamp)
    $orcaWork = Join-Path $orcaParent 'ORCA-0.9.1'
    if (-not (Test-Path -LiteralPath (Join-Path $orcaWork 'ORCA.exe'))) {
        New-Item -ItemType Directory -Path $orcaParent -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $orcaRoot 'dist\ORCA-0.9.1') -Destination $orcaParent -Recurse -Force
    }
    $orcaExe = Join-Path $orcaWork 'ORCA.exe'
    $orcaArgs = @()
    $env:PYTHONPATH = $null
    $env:PATH = $env:SystemRoot + '\system32;' + $env:SystemRoot
}
function Wait-OrcaMarker([string]$Name) {
    $orcaDeadline = [DateTime]::UtcNow.AddSeconds(50)
    while (-not (Test-Path -LiteralPath (Join-Path $env:PDFMT_DATA_DIR $Name))) {
        if ($orcaPrimary.HasExited) { throw 'Startup owner exited before expected marker.' }
        if ([DateTime]::UtcNow -gt $orcaDeadline) { throw ('Timed out: ' + $Name) }
        Start-Sleep -Milliseconds 50
    }
}
$orcaProcesses = @()
try {
    $env:ORCA_STARTUP_TEST_LAUNCHED_AT = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000).ToString([Globalization.CultureInfo]::InvariantCulture)
    $orcaPrimary = Start-Process -FilePath $orcaExe -ArgumentList (@($orcaArgs) + @('--startup-test')) -WorkingDirectory $orcaWork -WindowStyle Normal -PassThru
    $orcaProcesses += $orcaPrimary
    Wait-OrcaMarker 'owner-started.json'
    $orcaSecondary = Start-Process -FilePath $orcaExe -ArgumentList (@($orcaArgs) + @('--startup-duplicate')) -WorkingDirectory $orcaWork -WindowStyle Hidden -PassThru
    $orcaProcesses += $orcaSecondary
    if (-not $orcaSecondary.WaitForExit(15000) -or $orcaSecondary.ExitCode -ne 0) { throw 'Early duplicate did not exit cleanly.' }
    Wait-OrcaMarker 'ready-for-duplicate.json'
    $orcaLate = Start-Process -FilePath $orcaExe -ArgumentList (@($orcaArgs) + @('--startup-duplicate')) -WorkingDirectory $orcaWork -WindowStyle Hidden -PassThru
    $orcaProcesses += $orcaLate
    if (-not $orcaLate.WaitForExit(15000) -or $orcaLate.ExitCode -ne 0) { throw 'Late duplicate did not exit cleanly.' }
    if (-not $orcaPrimary.WaitForExit(40000)) { throw 'Startup test owner did not finish.' }
    $orcaText = Get-Content -LiteralPath $env:PDFMT_REPORT -Raw -Encoding UTF8
    Write-Output $orcaText
    if (-not ($orcaText | ConvertFrom-Json).ok -or $orcaPrimary.ExitCode -ne 0) { throw 'Startup integration check failed.' }
} finally {
    foreach ($orcaProcess in $orcaProcesses) { if (-not $orcaProcess.HasExited) { Stop-Process -Id $orcaProcess.Id -Force } }
}
