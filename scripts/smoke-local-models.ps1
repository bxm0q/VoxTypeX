param([Parameter(Mandatory=$true)][string]$Manifest, [string]$Exe="")
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe=Join-Path $root 'dist\VoxTypeX\VoxTypeX.exe' }
$exePath=(Resolve-Path -LiteralPath $Exe).Path
$manifestPath=(Resolve-Path -LiteralPath $Manifest).Path
$resultDirectory=Join-Path $root ('.local\native-frozen-smoke-'+[guid]::NewGuid().ToString('N'))
$process=Start-Process -FilePath $exePath -ArgumentList @('--smoke-local-models',"`"$resultDirectory`"","`"$manifestPath`"") -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(750000)) {
    Stop-Process -Id $process.Id
    throw "Native frozen smoke timed out: $resultDirectory"
}
if ($process.ExitCode -ne 0) { throw "Native frozen smoke failed ($($process.ExitCode)): $resultDirectory" }
Get-Content -LiteralPath (Join-Path $resultDirectory 'result.json') -Encoding UTF8
