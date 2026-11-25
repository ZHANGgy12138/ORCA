param([switch]$Translate, [switch]$CancelTest, [switch]$UiTest, [switch]$BatchTest, [switch]$FloatingTest, [switch]$ReaderTest, [switch]$IndexTest, [switch]$FigureLabels, [switch]$EngineTest, [switch]$FreeEngineTest, [switch]$ExportTest, [switch]$Launch, [string]$InputPdf, [string]$SecondPdf, [string]$TestEngine = "bing", [string]$TestPages = "7")
$ErrorActionPreference = 'Stop'
$translationRoot = Split-Path $PSScriptRoot -Parent
$translationBuildStamp = (Get-Item -LiteralPath (Join-Path $translationRoot 'dist\ORCA-0.9.1\ORCA.exe')).LastWriteTimeUtc.Ticks
$translationTestParent = Join-Path $translationRoot ('build\portable test\' + $translationBuildStamp)
$translationTarget = Join-Path $translationTestParent 'ORCA-0.9.1'
New-Item -ItemType Directory -Path $translationTestParent -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $translationTarget 'ORCA.exe'))) {
    Copy-Item -LiteralPath (Join-Path $translationRoot 'dist\ORCA-0.9.1') -Destination $translationTestParent -Recurse -Force
}
$env:PDFMT_DATA_DIR = Join-Path $translationRoot 'test-data\orca-v091-frozen'
$env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'diagnostics.json'
$env:PYTHONPATH = $null
$env:PATH = $env:SystemRoot + '\system32;' + $env:SystemRoot
$translationExe = Join-Path $translationTarget 'ORCA.exe'
if ($Launch) {
    $env:PDFMT_DATA_DIR = Join-Path $translationRoot 'test-data\desktop-window'
    $translationProcess = Start-Process -FilePath $translationExe -WorkingDirectory $translationTarget -WindowStyle Hidden -PassThru
    Write-Output ('Desktop process: ' + $translationProcess.Id)
    exit 0
}
$translationArguments = @('--diagnostics')
if ($ExportTest) {
    $translationArguments = @('--export-test')
    $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR ('export-test-' + [guid]::NewGuid().ToString('N') + '.json')
}
if ($EngineTest) {
    $translationArguments = @('--engine-test')
    $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'engine-test.json'
}
if ($FreeEngineTest) {
    $translationArguments = @('--free-engine-test')
    $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'free-engine-test.json'
}
if ($Translate -or $CancelTest -or $UiTest -or $BatchTest -or $FloatingTest -or $ReaderTest -or $IndexTest) {
    if (-not $InputPdf) { throw 'InputPdf is required for the translation test.' }
    $env:PDFMT_TEST_INPUT = $InputPdf
    $env:PDFMT_TEST_ENGINE = $TestEngine
    $env:PDFMT_TEST_PAGES = $TestPages
    $translationArguments = @('--self-test')
    $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'translation-test.json'
    if ($CancelTest) {
        $translationArguments = @('--cancel-test')
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'cancel-test.json'
    }
    if ($UiTest) {
        $translationArguments = @('--ui-test')
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'ui-test.json'
        if ($SecondPdf) { $env:PDFMT_TEST_INPUTS = ConvertTo-Json -InputObject @($InputPdf, $SecondPdf) -Compress }
    }
    if ($BatchTest) {
        if (-not $SecondPdf) { throw 'SecondPdf is required for batch verification.' }
        $translationArguments = @('--batch-test')
        $env:PDFMT_TEST_INPUTS = ConvertTo-Json -InputObject @($InputPdf, $SecondPdf) -Compress
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'batch-test.json'
    }
    if ($FloatingTest) {
        $translationArguments = @('--floating-test')
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'floating-test.json'
    }
    if ($IndexTest) {
        $translationArguments = @("--index-test")
        if ($FigureLabels) { $translationArguments += "--test-figure-labels" }
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR "index-test.json"
    }
    if ($ReaderTest) {
        $translationArguments = @('--reader-test')
        $env:PDFMT_REPORT = Join-Path $env:PDFMT_DATA_DIR 'reader-test.json'
    }
}
$translationProcess = Start-Process -FilePath $translationExe -ArgumentList $translationArguments -WorkingDirectory $translationTarget -WindowStyle Hidden -PassThru -Wait
if (Test-Path -LiteralPath $env:PDFMT_REPORT) {
    $orcaReportText = Get-Content -LiteralPath $env:PDFMT_REPORT -Encoding UTF8 -Raw
    Write-Output $orcaReportText
    $orcaReport = $orcaReportText | ConvertFrom-Json
    if (-not $orcaReport.ok) { exit 1 }
} else {
    Write-Output 'Expected verification report was not produced.'
    exit 1
}
Write-Output ('Executable exit: ' + $translationProcess.ExitCode)
exit $translationProcess.ExitCode
