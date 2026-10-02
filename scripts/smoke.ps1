param(
    [Parameter(Mandatory = $true)][string]$SpeechWav,
    [string]$Exe = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $root "dist\VoxTypeX\VoxTypeX.exe" }
$exePath = (Resolve-Path -LiteralPath $Exe).Path
$wavPath = (Resolve-Path -LiteralPath $SpeechWav).Path
$resultDirectory = Join-Path $root (".local\frozen-smoke-" + [guid]::NewGuid().ToString("N"))
$process = Start-Process -FilePath $exePath -ArgumentList @("--smoke-test", "`"$resultDirectory`"", "`"$wavPath`"") -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(750000)) {
    Stop-Process -Id $process.Id
    throw "Frozen smoke test timed out. Inspect $resultDirectory\logs"
}
if ($process.ExitCode -ne 0) { throw "Frozen smoke failed ($($process.ExitCode)). Inspect $resultDirectory" }
Get-Content -LiteralPath (Join-Path $resultDirectory "result.json") -Encoding UTF8
Write-Output "Automatic checks passed. Complete docs/MANUAL_SMOKE_TEST.md with Notepad."
