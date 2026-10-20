param([string]$PythonPath = "", [string]$EnvironmentPath = "", [switch]$SkipNativeBuild)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Invoke-Python([string[]]$Arguments) {
    & $script:buildPython @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($Arguments -join ' ')" }
}

$buildEnvironment = if ($EnvironmentPath) { $EnvironmentPath } else { Join-Path $root '.local\build-venv' }
$buildPython = Join-Path $buildEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    if ($PythonPath) { & $PythonPath -m venv $buildEnvironment }
    else { & py -3.13 -m venv $buildEnvironment }
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.13 x64 or pass -PythonPath." }
}
Invoke-Python -Arguments @("-c", "import sys, struct; assert sys.platform == 'win32' and sys.version_info >= (3, 12) and struct.calcsize('P') == 8, 'Build requires Windows / Python 3.12+ x64'")
Invoke-Python -Arguments @("-m", "pip", "install", "-r", "requirements-build.lock")
Invoke-Python -Arguments @("-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", ".")
if (-not $SkipNativeBuild) { & "$PSScriptRoot\build-native.ps1" -PythonPath $buildPython }
Invoke-Python -Arguments @("-m", "pip", "check")
Invoke-Python -Arguments @("-m", "ruff", "check", "src", "tests", "packaging", "scripts")
Invoke-Python -Arguments @("-m", "pytest", "-q")
$previousPath = $env:PATH
try {
    # Keep unrelated DLLs off the build PATH.
    $env:PATH = "$(Split-Path -Parent $buildPython);$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-Python -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", "packaging/VoxTypeX.spec")
} finally { $env:PATH = $previousPath }
Invoke-Python -Arguments @("scripts/release_files.py")
Write-Output "Built: $root\dist\VoxTypeX\VoxTypeX.exe"
Write-Output "Release: $root\dist\VoxTypeX-0.1.3-win-x64.zip"
Write-Output "Run scripts/smoke.ps1 with a speech WAV before publishing."
