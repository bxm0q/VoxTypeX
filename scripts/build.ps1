param([string]$PythonPath = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Invoke-Python([string[]]$Arguments) {
    & $script:buildPython @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($Arguments -join ' ')" }
}

$buildPython = Join-Path $root ".venv-build\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $buildPython)) {
    if ($PythonPath) { & $PythonPath -m venv .venv-build }
    else { & py -3.14 -m venv .venv-build }
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.14 x64 or pass -PythonPath." }
}
Invoke-Python -Arguments @("-c", "import sys, struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3, 14) and struct.calcsize('P') == 8, 'Build requires Windows / Python 3.14 x64'")
Invoke-Python -Arguments @("-m", "pip", "install", "-r", "requirements-build.lock")
Invoke-Python -Arguments @("-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", ".")
Invoke-Python -Arguments @("-m", "pip", "check")
Invoke-Python -Arguments @("-m", "ruff", "check", "src", "tests", "packaging")
Invoke-Python -Arguments @("-m", "pytest", "-q")
$previousPath = $env:PATH
try {
    # Avoid collecting unrelated DLLs from IDEs, multimedia tools and other PATH entries.
    $env:PATH = "$(Split-Path -Parent $buildPython);$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-Python -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", "packaging/VoxTypeX.spec")
} finally { $env:PATH = $previousPath }
Invoke-Python -Arguments @("scripts/release_files.py")
Write-Output "Built: $root\dist\VoxTypeX\VoxTypeX.exe"
Write-Output "Release: $root\dist\VoxTypeX-0.1.0-win-x64.zip"
Write-Output "Run scripts/smoke.ps1 with a speech WAV before publishing."
