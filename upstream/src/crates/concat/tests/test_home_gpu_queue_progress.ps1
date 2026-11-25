# SPDX-License-Identifier: AGPL-3.0-or-later
# Regression tests verifying:
# 1. Byte formatting and progress accuracy in concat_gpu_client.ps1
# 2. Calculation of actual transferred bytes from chunks (not estimated)
# 3. Report outputting transferred_bytes, total_bytes, and formatted readout
# 4. Retired Home GPU Queue remains disconnected; offline ZIP dialog stays available

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$client = (Resolve-Path (Join-Path $scriptDir "..\src\concat_gpu_client.ps1")).ProviderPath
$cloudGpuSlint = (Resolve-Path (Join-Path $scriptDir "..\ui\dialogs\cloud-gpu.slint")).ProviderPath
$startSlint = (Resolve-Path (Join-Path $scriptDir "..\ui\start.slint")).ProviderPath
$appSlint = (Resolve-Path (Join-Path $scriptDir "..\ui\app.slint")).ProviderPath

Write-Host "Running GPU Queue Retirement & Real Upload Progress Regression Tests..."

# -------------------------------------------------------------------------
# Test 1: Byte formatting and upload progress format
# -------------------------------------------------------------------------
Write-Host "Test 1: Byte formatting and upload progress accuracy..."

# Dot-source the client script with -DefineOnly to load functions into scope
. $client -DefineOnly

$b0 = Format-ByteSize 0
if ($b0 -ne "0 B") { throw "Expected '0 B' but got '$b0'" }

$b500 = Format-ByteSize 500
if ($b500 -ne "500 B") { throw "Expected '500 B' but got '$b500'" }

$b1kb = Format-ByteSize 1024
if ($b1kb -ne "1.00 KB") { throw "Expected '1.00 KB' but got '$b1kb'" }

$b1mb = Format-ByteSize (1024 * 1024)
if ($b1mb -ne "1.00 MB") { throw "Expected '1.00 MB' but got '$b1mb'" }

$b12_5mb = Format-ByteSize 13107200
if ($b12_5mb -ne "12.50 MB") { throw "Expected '12.50 MB' but got '$b12_5mb'" }

$b45_2mb = Format-ByteSize 47395635
if ($b45_2mb -ne "45.20 MB") { throw "Expected '45.20 MB' but got '$b45_2mb'" }

$b1gb = Format-ByteSize (1024 * 1024 * 1024)
if ($b1gb -ne "1.00 GB") { throw "Expected '1.00 GB' but got '$b1gb'" }

# Exact requirement string: "12.50 MB / 45.20 MB (27%)"
$progressStr = Format-UploadProgress 13107200 47395635
if ($progressStr -ne "12.50 MB / 45.20 MB (27%)") {
    throw "Expected '12.50 MB / 45.20 MB (27%)' but got '$progressStr'"
}

$progress100 = Format-UploadProgress 47395635 47395635
if ($progress100 -ne "45.20 MB / 45.20 MB (100%)") {
    throw "Expected '45.20 MB / 45.20 MB (100%)' but got '$progress100'"
}

$progress0 = Format-UploadProgress 0 47395635
if ($progress0 -ne "0 B / 45.20 MB (0%)") {
    throw "Expected '0 B / 45.20 MB (0%)' but got '$progress0'"
}

Write-Host "  [PASS] Byte formatting and progress calculation accurate."

# -------------------------------------------------------------------------
# Test 2: Report function emits transferred_bytes, total_bytes & formatted message
# -------------------------------------------------------------------------
Write-Host "Test 2: Report function event structure..."

$reportOutput = powershell.exe -NoProfile -ExecutionPolicy Bypass -Command @"
    . '$client' -DefineOnly
    Report 'Uploaded chunk 1 of 4 (12.50 MB / 45.20 MB (27%)).' 27 '' `$false 'uploading' '' 13107200 47395635
"@

$parsedEvent = $reportOutput | ConvertFrom-Json
if ($parsedEvent.progress -ne 27) { throw "Expected progress 27, got $($parsedEvent.progress)" }
if ($parsedEvent.transferred_bytes -ne 13107200) { throw "Expected transferred_bytes 13107200, got $($parsedEvent.transferred_bytes)" }
if ($parsedEvent.total_bytes -ne 47395635) { throw "Expected total_bytes 47395635, got $($parsedEvent.total_bytes)" }
if (-not $parsedEvent.message.Contains("12.50 MB / 45.20 MB (27%)")) {
    throw "Expected formatted readout in message, got '$($parsedEvent.message)'"
}

Write-Host "  [PASS] Report properly emits structured transferred_bytes, total_bytes, and message."

# -------------------------------------------------------------------------
# Test 3: Slint UI retirement and offline ZIP contract verification
# -------------------------------------------------------------------------
Write-Host "Test 3: Retired Home Queue and supported offline ZIP dialog..."

$cloudGpuContent = [IO.File]::ReadAllText($cloudGpuSlint)
if (-not $cloudGpuContent.Contains("export component GpuQueueDialog inherits Modal")) {
    throw "GpuQueueDialog component inheriting Modal missing from cloud-gpu.slint"
}
if (-not $cloudGpuContent.Contains("in-out property <bool> queue-open")) {
    throw "CloudGpu.queue-open property missing from cloud-gpu.slint"
}
if (-not $cloudGpuContent.Contains("bytes: string")) {
    throw "bytes field missing from GpuQueueRow"
}
if (-not $cloudGpuContent.Contains("export component GpuQueueSection inherits DialogSection")) {
    throw "GpuQueueSection missing from cloud-gpu.slint"
}

$startContent = [IO.File]::ReadAllText($startSlint)
foreach ($component in @("component WideHome inherits Rectangle", "component NarrowHome inherits Rectangle", "export component StartScreen inherits Rectangle")) {
    if (-not $startContent.Contains($component)) {
        throw "$component missing from start.slint"
    }
}
# PR #170 intentionally retired the desktop GPU queue launcher and callbacks.
if ($startContent -match 'open-gpu-queue|label:\s*"GPU Queue"') {
    throw "Retired Home GPU Queue launcher or callback reintroduced"
}

$appContent = [IO.File]::ReadAllText($appSlint)
if ($appContent -match 'open-gpu-queue|CloudGpu\.queue-open|\bGpuQueueDialog\s*\{') {
    throw "Retired desktop GPU Queue dialog or callback reintroduced"
}
if ($appContent -notmatch 'OfflineZipDialog\s*\{[\s\S]*?open:\s*CloudGpu\.zip-open\s*;') {
    throw "Supported offline ZIP dialog or open binding missing"
}
if ($appContent -notmatch 'closed\s*=>\s*\{\s*CloudGpu\.zip-open\s*=\s*false\s*;\s*\}') {
    throw "Offline ZIP close binding missing"
}

Write-Host "  [PASS] Retired GPU Queue remains disconnected; offline ZIP dialog and legacy helper contracts verified."

Write-Host "`nAll GPU Queue Retirement & Real Upload Progress Tests PASS!"
