# Exercise the embedded EXE functions with authenticated HTTP mocks, no GPU or builds.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "../src/concat_gpu_client.ps1") -DefineOnly
$script:contract = @{id="concat-cuda-hybrid-v1"; version=1}
$script:calls = @()
function Invoke-RestMethod {
    param($MaximumRedirection,$Uri,$Headers,$TimeoutSec)
    if ($MaximumRedirection -ne 0 -or $TimeoutSec -ne 30 -or
        $Headers.Authorization -cne "Bearer fixture") {throw "Unsafe or unauthenticated handshake"}
    $script:calls += $Uri
    switch ($Uri) {
        "https://fixture.invalid/health/ready" {return @{ok=$true; cuda_stage1=$true}}
        "https://fixture.invalid/api/diagnostics/gpu" {return @{cuda_stage1=$true; ffmpeg_available=$true; ffprobe_available=$true}}
        "https://fixture.invalid/api/capabilities" {
            # A matching contract must not invent rendering/ASR availability.
            return @{api_version=1; bundle_versions=@(1); render_modes=@("cuda-hybrid-v1");
                validation_version=1; render_contract=$script:contract;
                gpu_captions=@{available=$false; device="cuda"}}
        }
        default {throw "Unexpected handshake request: $Uri"}
    }
}
$auth = @{Authorization="Bearer fixture"}
foreach ($version in @([int]1,[long]1)) {
    $script:contract = @{id="concat-cuda-hybrid-v1"; version=$version}
    $caps = Test-Worker "https://fixture.invalid" $auth
    if ($caps.gpu_captions.available) {throw "Contract must not imply GPU ASR availability"}
}
function Assert-ContractRejected($bad) {
    $script:contract = $bad
    $rejected = $false
    try {Test-Worker "https://fixture.invalid" $auth | Out-Null}
    catch {
        if ($_.Exception.Message -notlike "*render contract mismatch*") {throw}
        $rejected = $true
    }
    if (!$rejected) {throw "Malformed or mismatched render contract accepted"}
}
foreach ($bad in @(
    $null,
    @{},
    @{id="other"; version=1},
    @{id="CONCAT-CUDA-HYBRID-V1"; version=1},
    @{id="concat-cuda-hybrid-v1"; version=2},
    @{id="concat-cuda-hybrid-v1"; version="1"},
    @{id="concat-cuda-hybrid-v1"; version=$true},
    @{id="concat-cuda-hybrid-v1"; version=[double]1},
    @{id="concat-cuda-hybrid-v1"}
)) {
    Assert-ContractRejected $bad
}
# PowerShell member enumeration can otherwise turn a one-element contract array
# into valid-looking .id/.version values, whereas worker restore requires an object.
Assert-ContractRejected -bad @(@{id="concat-cuda-hybrid-v1"; version=1})
if (@($script:calls | Where-Object {$_ -notmatch '/(health/ready|api/diagnostics/gpu|api/capabilities)$'}).Count) {
    throw "Handshake attempted a render or upload"
}
Write-Host "PASS: strict authenticated EXE render contract; no feature availability inferred"
