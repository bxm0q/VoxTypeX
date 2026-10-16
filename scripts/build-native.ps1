param([Parameter(Mandatory=$true)][string]$PythonPath, [string]$CMakePath="")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pin = Get-Content -LiteralPath "$root\packaging\native-runtime.json" -Raw | ConvertFrom-Json
$source = Join-Path $root '.local\tools\transcribe-cpp'
$wheels = Join-Path $root '.local\native-wheels'
if (-not (Test-Path -LiteralPath "$source\.git")) {
    & git clone --no-checkout $pin.repository $source
    if ($LASTEXITCODE -ne 0) { throw 'Native source clone failed.' }
    & git -C $source fetch --depth 1 origin $pin.revision
    if ($LASTEXITCODE -ne 0) { throw 'Native source fetch failed.' }
    & git -C $source checkout --detach $pin.revision
    if ($LASTEXITCODE -ne 0) { throw 'Native source checkout failed.' }
}
if ((& git -C $source rev-parse HEAD).Trim() -ne $pin.revision) {
    throw 'Existing native checkout has another revision. Use the exact checked-in pin.'
}
if (& git -C $source status --porcelain --untracked-files=no) {
    throw 'Native source contains tracked modifications. Restore the pinned source before building.'
}
if (-not $CMakePath) {
    $available = Get-Command cmake -ErrorAction SilentlyContinue
    if ($available) { $CMakePath = $available.Source }
    else { $CMakePath = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" }
}
if (-not (Test-Path -LiteralPath $CMakePath)) { throw 'Install CMake and MSVC C++ Build Tools, or pass -CMakePath.' }
$oldPath = $env:PATH
$oldGenerator = $env:CMAKE_GENERATOR
$oldPlatform = $env:CMAKE_GENERATOR_PLATFORM
$oldParallel = $env:CMAKE_BUILD_PARALLEL_LEVEL
$oldLane = $env:TRANSCRIBE_WHEEL_LANE
try {
    $env:PATH = "$(Split-Path -Parent $CMakePath);$env:PATH"
    $env:CMAKE_GENERATOR = 'Visual Studio 17 2022'
    $env:CMAKE_GENERATOR_PLATFORM = 'x64'
    $env:CMAKE_BUILD_PARALLEL_LEVEL = '4'
    $env:TRANSCRIBE_WHEEL_LANE = 'voxtypex-cpu'
    $nativeWheel = Join-Path $wheels 'transcribe_cpp_native-0.4.0-py3-none-win_amd64.whl'
    & $PythonPath "$PSScriptRoot\native_build_cache.py" check $nativeWheel
    if ($LASTEXITCODE -ne 0) {
        $definitions = @($pin.cmake_options.PSObject.Properties | ForEach-Object { "-Ccmake.define.$($_.Name)=$($_.Value)" })
        & $PythonPath -m pip wheel --no-deps --wheel-dir $wheels @definitions '-Cbuild.tool-args=-m:4' $source
        if ($LASTEXITCODE -ne 0) { throw 'Native CPU wheel compilation failed.' }
        & $PythonPath "$PSScriptRoot\native_build_cache.py" record $nativeWheel
        if ($LASTEXITCODE -ne 0) { throw 'Could not record the native source/options/hash build receipt.' }
    }
    & $PythonPath -m pip install --no-deps $nativeWheel "$source\bindings\python"
    if ($LASTEXITCODE -ne 0) { throw 'Native provider / binding installation failed.' }
    & $PythonPath "$PSScriptRoot\stage_native.py"
    if ($LASTEXITCODE -ne 0) { throw 'Native runtime staging failed.' }
    & $PythonPath -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Native runtime dependency check failed.' }
} finally {
    $env:PATH=$oldPath
    $env:CMAKE_GENERATOR=$oldGenerator
    $env:CMAKE_GENERATOR_PLATFORM=$oldPlatform
    $env:CMAKE_BUILD_PARALLEL_LEVEL=$oldParallel
    $env:TRANSCRIBE_WHEEL_LANE=$oldLane
}
