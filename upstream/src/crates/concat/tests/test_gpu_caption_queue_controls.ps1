# Source-only tests: load actual client functions with network mocks; no GPU or builds.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "../src/concat_gpu_client.ps1") -DefineOnly
$script:calls = @()
$script:statuses = 0
function Invoke-RestMethod { param($Method,$MaximumRedirection,$Uri,$Headers,$TimeoutSec)
    if ($Method -ne "Post" -or $MaximumRedirection -ne 0 -or $TimeoutSec -ne 20) {throw "Unsafe request configuration"}
    if ($Headers.Authorization -ne "fixture") {throw "Authentication missing"}
    $script:calls += $Uri
}
function Queue-Status { param($job,$domain,$headers,$gpuDataDir) $script:statuses++ }
function Report { param($message,$progress,$location,$failed,$phase)
    if ($phase -ne "cancelled") {throw "Wrong cancellation result"}
}
$auth = @{Authorization="fixture"}
Invoke-QueueControl "Retry" "same-job" "https://fixture.invalid" $auth "unused"
if ($script:calls.Count -ne 1 -or $script:calls[0] -ne "https://fixture.invalid/api/jobs/same-job/retry" -or $script:statuses -ne 1) {throw "Retry did not reuse the submitted job before polling"}
Invoke-QueueControl "Cancel" "same-job" "https://fixture.invalid" $auth "unused"
if ($script:calls.Count -ne 2 -or $script:calls[1] -ne "https://fixture.invalid/api/jobs/same-job/cancel" -or $script:statuses -ne 1) {throw "Cancellation must not poll or upload"}
Invoke-QueueControl "Status" "same-job" "https://fixture.invalid" $auth "unused"
if ($script:calls.Count -ne 2 -or $script:statuses -ne 2) {throw "Read-only status changed the remote job"}
function Invoke-RestMethod { throw "offline fixture" }
$failed = $false
try { Invoke-QueueControl "Cancel" "same-job" "https://fixture.invalid" $auth "unused" } catch { $failed = $true }
if (!$failed) {throw "Offline cancellation must not claim remote confirmation"}
Write-Host "GPU caption queue retry/cancel/status mocks PASS"
