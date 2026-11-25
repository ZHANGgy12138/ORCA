param([switch]$SkipPrepare)
$ErrorActionPreference = 'Stop'
$translationRoot = $PSScriptRoot
$translationPython = Join-Path (Split-Path $translationRoot -Parent) 'PDFMathTranslate-local\.venv\Scripts\python.exe'
$translationBuild = Join-Path $translationRoot 'build'
New-Item -ItemType Directory -Path $translationBuild -Force | Out-Null
$env:PYTHONPATH = Join-Path $translationRoot 'vendor'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $translationBuild 'pyinstaller-cache'
$env:PDFMT_DATA_DIR = Join-Path $translationBuild 'analysis-data'
$env:PDFMT_CACHE_DIR = Join-Path $env:PDFMT_DATA_DIR 'cache\babeldoc'
$env:UV_CACHE_DIR = Join-Path $translationBuild 'uv-cache'
if (-not $SkipPrepare) {
    Write-Host 'Preparing application resources and source licenses...'
    & $translationPython (Join-Path $translationRoot 'scripts\prepare.py') --assets (Join-Path $env:USERPROFILE '.cache\babeldoc')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv pip freeze --python $translationPython | Set-Content -LiteralPath (Join-Path $translationRoot 'requirements-build.txt') -Encoding UTF8
}
Write-Host 'Building Windows executable...'
& $translationPython (Join-Path $translationRoot 'scripts\bundle_modules.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$translationArguments = @('-m','PyInstaller','--noconfirm','--distpath',('"' + (Join-Path $translationRoot 'dist') + '"'),'--workpath',('"' + (Join-Path $translationBuild 'pyinstaller') + '"'),('"' + (Join-Path $translationRoot 'desktop.spec') + '"'))
$translationProcess = Start-Process -FilePath $translationPython -ArgumentList $translationArguments -WorkingDirectory $translationRoot -WindowStyle Hidden -PassThru -Wait -RedirectStandardError (Join-Path $translationBuild 'build.log') -RedirectStandardOutput (Join-Path $translationBuild 'build.stdout.log')
$translationBuildExit = $translationProcess.ExitCode
Get-Content -LiteralPath (Join-Path $translationBuild 'build.log') -Tail 25
exit $translationBuildExit
