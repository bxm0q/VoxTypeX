param([Parameter(Mandatory=$true)][string]$SpeechWav, [string]$Exe='')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe=Join-Path $root 'dist\VoxTypeX\VoxTypeX.exe' }
$exePath=(Resolve-Path -LiteralPath $Exe).Path
$speechPath=(Resolve-Path -LiteralPath $SpeechWav).Path
$resultDirectory=Join-Path $root ('.local\audio-frozen-smoke-'+[guid]::NewGuid().ToString('N'))
$process=Start-Process -FilePath $exePath -ArgumentList @('--smoke-audio',"`"$resultDirectory`"","`"$speechPath`"") -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(120000)) {
    Stop-Process -Id $process.Id
    throw "Audio smoke timed out: $resultDirectory"
}
$result=Join-Path $resultDirectory 'result.json'
if (Test-Path -LiteralPath $result) { Get-Content -LiteralPath $result -Encoding UTF8 }
if ($process.ExitCode -ne 0) { throw "Audio smoke failed ($($process.ExitCode)): $resultDirectory" }
