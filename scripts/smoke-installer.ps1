param([Parameter(Mandatory = $true)][string]$SpeechWav,
    [string]$Installer = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Installer) { $Installer = Join-Path $root "dist\VoxTypeX-0.1.1-setup-x64.exe" }
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$testRoot = Join-Path $root (".local\installer-smoke-" + [guid]::NewGuid().ToString("N"))
$installDir = Join-Path $testRoot "Установка VoxTypeX"
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{A1C96F62-35B1-4D73-8AC9-216FE73D26A8}_is1"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "VoxTypeX.lnk"
$menuLink = Join-Path ([Environment]::GetFolderPath("Programs")) "VoxTypeX\VoxTypeX.lnk"
if ((Test-Path -LiteralPath $uninstallKey) -or (Test-Path -LiteralPath $desktopLink) -or
    (Test-Path -LiteralPath $menuLink) -or (Get-ItemProperty -LiteralPath $runKey -Name VoxTypeX -ErrorAction SilentlyContinue)) {
    throw "Existing VoxTypeX installation or shortcuts detected. Use a separate Windows account for this smoke test."
}
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$profileSettings = Join-Path $env:LOCALAPPDATA "VoxTypeX\settings.json"
$settingsHash = if (Test-Path -LiteralPath $profileSettings) { (Get-FileHash -LiteralPath $profileSettings).Hash } else { $null }
$checks = [System.Collections.Generic.List[string]]::new()

function Install-Test([string]$Tasks, [string]$Log) {
    $process = Start-Process -FilePath $installerPath -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$installDir`"", "/TASKS=`"$Tasks`"", "/LOG=`"$testRoot\$Log`""
    ) -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(180000)) { throw "Installer timed out; inspect $testRoot" }
    if ($process.ExitCode -ne 0) { throw "Installer failed: $($process.ExitCode)" }
}

try {
    Install-Test "desktopicon,startup" "install.log"
    $exe = Join-Path $installDir "VoxTypeX.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    if ((Get-FileHash -LiteralPath $exe).Hash -ne (Get-FileHash -LiteralPath "$root\dist\VoxTypeX\VoxTypeX.exe").Hash) {
        throw "Installed executable differs from the build."
    }
    if (-not (Test-Path -LiteralPath "$installDir\_internal\PySide6\QtCore.pyd")) { throw "Qt payload missing." }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($link in @($desktopLink, $menuLink)) {
        if (-not (Test-Path -LiteralPath $link) -or $shell.CreateShortcut($link).TargetPath -ne $exe) {
            throw "Shortcut does not point to the installed application."
        }
    }
    if ((Get-ItemProperty -LiteralPath $runKey -Name VoxTypeX).VoxTypeX -ne "`"$exe`"") { throw "Startup path is not quoted correctly." }
    if (-not (Test-Path -LiteralPath $uninstallKey)) { throw "Windows uninstall entry missing." }
    $checks.Add("custom path with spaces and Cyrillic; installed EXE matches the build")
    $checks.Add("desktop and Start menu shortcuts; quoted startup entry; Windows uninstall entry")
    & "$PSScriptRoot\smoke.ps1" -Exe $exe -SpeechWav $SpeechWav
    $checks.Add("installed application: tray, settings, microphone, Whisper process, shutdown")

    Install-Test "" "reinstall.log"
    if ((Test-Path -LiteralPath $desktopLink) -or (Get-ItemProperty -LiteralPath $runKey -Name VoxTypeX -ErrorAction SilentlyContinue)) {
        throw "Deselected options remained active after reinstall."
    }
    $checks.Add("reinstall with desktop shortcut and startup disabled")
} finally {
    $uninstaller = Join-Path $installDir "unins000.exe"
    # Only run the uninstaller created inside this test's unique workspace directory.
    $resolvedInstallDir = [System.IO.Path]::GetFullPath($installDir)
    if (-not $resolvedInstallDir.StartsWith([System.IO.Path]::GetFullPath($testRoot) + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe uninstall target."
    }
    if (Test-Path -LiteralPath $uninstaller) {
        $process = Start-Process -FilePath $uninstaller -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$testRoot\uninstall.log`""
        ) -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit(120000) -or $process.ExitCode -ne 0) { throw "Test uninstall failed; inspect $testRoot" }
    }
}
if ((Test-Path -LiteralPath "$installDir\VoxTypeX.exe") -or (Test-Path -LiteralPath $uninstallKey) -or
    (Test-Path -LiteralPath $desktopLink) -or (Test-Path -LiteralPath $menuLink) -or
    (Get-ItemProperty -LiteralPath $runKey -Name VoxTypeX -ErrorAction SilentlyContinue)) { throw "Uninstall left application entries behind." }
$afterHash = if (Test-Path -LiteralPath $profileSettings) { (Get-FileHash -LiteralPath $profileSettings).Hash } else { $null }
if ($settingsHash -ne $afterHash) { throw "User settings changed during installer smoke test." }
$checks.Add("uninstall removes application, shortcuts and startup; user settings preserved")
$checks | ConvertTo-Json | Set-Content -LiteralPath "$testRoot\result.json" -Encoding UTF8
$checks
Write-Output "Installer smoke passed: $testRoot"
