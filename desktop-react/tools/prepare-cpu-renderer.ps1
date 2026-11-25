param([string]$Destination = 'native/cpu-vulkan')
$ErrorActionPreference = 'Stop'
$destinationPath = [IO.Path]::GetFullPath($Destination)
$work = Join-Path (Get-Location) 'build/cpu-renderer'
New-Item -ItemType Directory -Force $work,$destinationPath | Out-Null
$archive = Join-Path $work 'mesa.7z'
Invoke-WebRequest 'https://github.com/pal1000/mesa-dist-win/releases/download/26.3.1/mesa3d-26.3.1-release-msvc.7z' -OutFile $archive
if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '64e84cf13d67dd354640bf443ed0445e6a83afd06a5729f0750b3744d633b3f1') { throw 'Mesa CPU runtime checksum mismatch' }
& 7z x $archive "-o$work/mesa" -y 'x64/vulkan_lvp.dll' 'x64/lvp_icd.x86_64.json' 'readme.txt'
if ($LASTEXITCODE) { throw 'Mesa runtime extraction failed' }
Copy-Item "$work/mesa/x64/vulkan_lvp.dll","$work/mesa/x64/lvp_icd.x86_64.json" $destinationPath
Copy-Item "$work/mesa/readme.txt" "$destinationPath/Mesa-distribution-readme.txt"

$loader = Join-Path $work 'loader'
if (!(Test-Path "$loader/.git")) {
  git clone --depth 1 --branch vulkan-sdk-1.4.335.0 https://github.com/KhronosGroup/Vulkan-Loader.git $loader
  if ($LASTEXITCODE) { throw 'Vulkan loader source download failed' }
}
$revision = git -C $loader rev-parse HEAD
if ($revision -ne '057ca1e5f2b61bbb3942e3a7464d488d5bde4a3c') { throw 'Unexpected Vulkan loader source revision' }
cmake -S $loader -B "$work/loader-build" -A x64 -DUPDATE_DEPS=ON -DBUILD_TESTS=OFF "-DCMAKE_INSTALL_PREFIX=$work/loader-install"
if ($LASTEXITCODE) { throw 'Vulkan loader configuration failed' }
cmake --build "$work/loader-build" --config Release --target install --parallel 2
if ($LASTEXITCODE) { throw 'Vulkan loader compilation failed' }
Copy-Item "$work/loader-install/bin/vulkan-1.dll" $destinationPath
Copy-Item "$loader/LICENSE.txt" "$destinationPath/Vulkan-Loader-LICENSE.txt"
Invoke-WebRequest 'https://gitlab.freedesktop.org/mesa/mesa/-/raw/mesa-26.3.1/docs/license.rst' -OutFile "$destinationPath/Mesa-LICENSE.txt"
@{mesa='26.3.1';mesa_sha256='64e84cf13d67dd354640bf443ed0445e6a83afd06a5729f0750b3744d633b3f1';loader_source=$revision;purpose='CPU Vulkan adapter for the existing WGSL renderer; no effect substitution'} | ConvertTo-Json | Set-Content "$destinationPath/runtime-metadata.json"
