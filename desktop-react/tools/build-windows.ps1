$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
  python -m pip install -r requirements.txt
  if ($LASTEXITCODE) { throw 'Python dependency installation failed' }
  Push-Location frontend
  npm ci
  if ($LASTEXITCODE) { throw 'Frontend dependency installation failed' }
  npm test
  if ($LASTEXITCODE) { throw 'Frontend tests failed' }
  npm run build
  if ($LASTEXITCODE) { throw 'Frontend build failed' }
  Pop-Location
  New-Item -ItemType Directory -Force native | Out-Null
  Copy-Item ../dist/Concat-0.2.8-GPU-Windows-x64/* native/ -Recurse -Force
  Copy-Item ../upstream/src/target/x86_64-pc-windows-msvc/quick/concat-ui-presets.exe native/
  & ./tools/prepare-cpu-renderer.ps1
  $env:CONCAT_GPU = 'software'
  python tools/engine-smoke.py --engine native/concat.exe --presets native/concat-ui-presets.exe --report dist/engine-smoke.json
  if ($LASTEXITCODE) { throw 'Real native bridge integration test failed' }
  python -m pytest tests -q
  if ($LASTEXITCODE) { throw 'Desktop host tests failed' }
  python tests/windows_ui.py
  if ($LASTEXITCODE) { throw 'Real WebView2 integration failed' }
  python -m PyInstaller --noconfirm --clean --windowed --name Concat-React --add-data 'frontend/dist;frontend/dist' --add-data 'native;native' app.py
  if ($LASTEXITCODE) { throw 'Portable Windows packaging failed' }
  & ./tools/smoke-portable.ps1
  Copy-Item ../upstream/LICENSE, ../upstream/THIRD_PARTY_NOTICES.md, README.md, CHANGELOG.md, Launch-Concat.cmd, Launch-CPU.cmd dist/Concat-React/
  Copy-Item docs/TEST-REPORT.md dist/Concat-React/
  $sha = $env:GITHUB_SHA
  if (!$sha) { $sha = (git rev-parse HEAD) }
  Compress-Archive -Path dist/Concat-React/* -DestinationPath "dist/Concat-React-Windows-x64-$sha.zip" -Force
  $zip = Get-Item "dist/Concat-React-Windows-x64-$sha.zip"
  $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
  "$hash  $($zip.Name)" | Set-Content dist/SHA256SUMS -Encoding ascii
  @{source_sha=$sha;sha256=$hash;artifact=$zip.Name;native_bridge_test='passed';main_merged=$false;salad_deployed=$false} | ConvertTo-Json | Set-Content dist/build-metadata.json
} finally { Pop-Location }
