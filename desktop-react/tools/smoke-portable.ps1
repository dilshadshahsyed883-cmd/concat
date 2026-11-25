$ErrorActionPreference = 'Stop'
$exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist/Concat-React/Concat-React.exe'
$report = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist/portable-startup.json'
if (Test-Path $report) { Remove-Item $report }
$env:CONCAT_STARTUP_REPORT = $report
$p = Start-Process -FilePath $exe -PassThru
try {
  $ready = $false
  for ($i=0; $i -lt 240; $i++) {
    Start-Sleep -Milliseconds 500
    $p.Refresh()
    if ($p.HasExited) { throw "Portable EXE exited: $($p.ExitCode)" }
    if (Test-Path $report) {
      $proof = Get-Content $report -Raw | ConvertFrom-Json
      if ($proof.status -ne 'passed' -or !$proof.frozen -or !$proof.native_boot -or !$proof.react_workspace) { throw "Packaged startup check failed: $($proof.error)" }
      if ($p.MainWindowTitle -eq 'Concat React Preview') { $ready=$true; break }
    }
    if ($p.MainWindowTitle -like '*startup*') { throw 'Portable EXE startup failure' }
  }
  if (!$ready) { throw 'Portable EXE never created its window' }
  Start-Sleep -Seconds 4
  $p.Refresh()
  if ($p.HasExited) { throw 'Portable EXE exited after startup' }
  Write-Output 'PASS packaged EXE booted bundled native services and React workspace'
} finally {
  Remove-Item Env:CONCAT_STARTUP_REPORT -ErrorAction SilentlyContinue
  if (!$p.HasExited) { Stop-Process -Id $p.Id -Force }
}
