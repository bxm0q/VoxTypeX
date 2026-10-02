param([switch]$SkipAppBuild, [string]$CompilerPath = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

if (-not $SkipAppBuild) { & "$PSScriptRoot\build.ps1" }
$sourceDir = Join-Path $root "dist\VoxTypeX"
if (-not (Test-Path -LiteralPath "$sourceDir\VoxTypeX.exe")) { throw "Build the application first." }
$versionMatch = [regex]::Match((Get-Content -LiteralPath "$root\pyproject.toml" -Raw), '(?m)^version = "([0-9.]+)"')
if (-not $versionMatch.Success) { throw "Application version not found." }
$appVersion = $versionMatch.Groups[1].Value

if (-not $CompilerPath) {
    $toolVersion = "6.7.3"
    $toolDir = Join-Path $root ".local\tools\inno-$toolVersion"
    $CompilerPath = Join-Path $toolDir "ISCC.exe"
    if (-not (Test-Path -LiteralPath $CompilerPath)) {
        New-Item -ItemType Directory -Path "$root\.local\tools" -Force | Out-Null
        $download = Join-Path $root ".local\tools\innosetup-$toolVersion.exe"
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/jrsoftware/issrc/releases/tags/is-6_7_3" -Headers @{'User-Agent' = 'VoxTypeX-build'}
        $asset = @($release.assets | Where-Object name -EQ "innosetup-$toolVersion.exe")
        if ($asset.Count -ne 1) { throw "Official Inno Setup package not found." }
        Invoke-WebRequest -Uri $asset[0].browser_download_url -OutFile $download -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $download
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') {
            throw "Inno Setup signature verification failed."
        }
        $tool = Start-Process -FilePath $download -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS", "/TASKS=`"`"", "/DIR=`"$toolDir`""
        ) -Wait -PassThru -WindowStyle Hidden
        if ($tool.ExitCode -ne 0) { throw "Inno Setup installation failed: $($tool.ExitCode)" }
    }
}
$CompilerPath = (Resolve-Path -LiteralPath $CompilerPath).Path
$assetsDir = Join-Path $root "build\installer-assets"
& "$PSScriptRoot\installer-art.ps1" -OutputDir $assetsDir
# Refresh the distributed instructions without changing the compiled application.
& "$root\.venv-build\Scripts\python.exe" "$PSScriptRoot\release_files.py"
if ($LASTEXITCODE -ne 0) { throw "Release files could not be prepared." }
& $CompilerPath "/Q" "/DAppVersion=$appVersion" "/DSourceDir=$sourceDir" "/DAssetsDir=$assetsDir" "/DOutputDir=$root\dist" "$root\packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed." }
$output = Join-Path $root "dist\VoxTypeX-$appVersion-setup-x64.exe"
$digest = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText("$output.sha256", "$digest  $([System.IO.Path]::GetFileName($output))`n", [System.Text.Encoding]::ASCII)
Write-Output "Installer: $output"
