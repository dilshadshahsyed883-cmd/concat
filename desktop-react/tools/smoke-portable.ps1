$ErrorActionPreference = 'Stop'
$exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist/Concat-React/Concat-React.exe'
$p = Start-Process -FilePath $exe -PassThru
try {
  $ready = $false
  for ($i=0; $i -lt 100; $i++) {
    Start-Sleep -Milliseconds 500
    $p.Refresh()
    if ($p.HasExited) { throw "Portable EXE exited: $($p.ExitCode)" }
    if ($p.MainWindowTitle -eq 'Concat React Preview') { $ready=$true; break }
    if ($p.MainWindowTitle -like '*startup*') { throw 'Portable EXE startup failure' }
  }
  if (!$ready) { throw 'Portable EXE never created its window' }
  Start-Sleep -Seconds 4
  $p.Refresh()
  if ($p.HasExited) { throw 'Portable EXE exited after startup' }
  Write-Output 'PASS packaged EXE created its Windows application window'
} finally { if (!$p.HasExited) { Stop-Process -Id $p.Id -Force } }
