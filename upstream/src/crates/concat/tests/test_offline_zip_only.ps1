# SPDX-License-Identifier: AGPL-3.0-or-later
# Windows integration test: the new OFFLINE path must produce exactly the
# existing GPU Queue bundle contract, with no credentials or queue mutation.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Archive-ContentHash($archive, [string]$name) {
    $entry=$archive.GetEntry($name)
    if ($null -eq $entry) {throw "Missing ZIP member: $name"}
    $stream=$entry.Open()
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-","").ToLowerInvariant()
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}
function Archive-Control($archive) {
    $stream=$archive.GetEntry("bundle.json").Open()
    $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8)
    try {return ($reader.ReadToEnd() | ConvertFrom-Json)}
    finally {$reader.Dispose();$stream.Dispose()}
}
function Run-Client($client,[string[]]$arguments) {
    $output=@(& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $client @arguments)
    return [pscustomobject]@{exit=$LASTEXITCODE; output=@($output)}
}
$client=(Resolve-Path (Join-Path $PSScriptRoot "..\src\concat_gpu_client.ps1")).ProviderPath
$temp=Join-Path ([IO.Path]::GetTempPath()) ("concat-offline-zip-test-"+[Guid]::NewGuid().ToString("N"))
$originalLocalData=$env:LOCALAPPDATA
try {
    $project=Join-Path $temp "User Project"
    $outDir=Join-Path $temp "Output"
    $env:LOCALAPPDATA=Join-Path $temp "NoCredentials"
    [IO.Directory]::CreateDirectory($project) | Out-Null
    [IO.Directory]::CreateDirectory($outDir) | Out-Null
    [IO.Directory]::CreateDirectory($env:LOCALAPPDATA) | Out-Null
    $video=Join-Path $project "clip.mp4"
    $voice=Join-Path $project "voice.wav"
    [IO.File]::WriteAllBytes($video,[Text.Encoding]::UTF8.GetBytes("VIDEO NEVER CHANGED"*500))
    [IO.File]::WriteAllBytes($voice,[Text.Encoding]::UTF8.GetBytes("VOICE NEVER CHANGED"*500))
    $manifest=Join-Path $project "concat.json"
    @{
      version=1
      title="Project snapshot"
      media=@(@{path="clip.mp4";kind="video"},@{path="voice.wav";kind="audio"},@{path="clip.mp4";kind="video"})
    } | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifest -Encoding UTF8
    $sourceHashes=@{}
    foreach($src in @($video,$voice,$manifest)) {
        $sourceHashes[$src]=(Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash
    }

    # Prepare the established GPU queue archive from the same saved manifest.
    $job="concat-cuda-1234567890abcdef"
    $queue=Join-Path $env:LOCALAPPDATA "Concat\GPU\Queue"
    $snapshot=Join-Path $queue $job
    [IO.Directory]::CreateDirectory($snapshot) | Out-Null
    [IO.File]::Copy($manifest,(Join-Path $snapshot "concat.json"))
    $existing=Run-Client $client @("-Operation","Prepare","-ProjectDir",$project,"-Job",$job,"-SnapshotID",$job)
    if ($existing.exit -ne 0) {throw "Existing Send to GPU Prepare failed: $($existing.output -join '; ') "}
    $queueZip=Join-Path $queue "$job.zip"
    if (!(Test-Path -LiteralPath $queueZip)) {throw "Existing GPU queue ZIP missing"}
    $queueHash=(Get-FileHash -LiteralPath $queueZip).Hash
    $queueListing=@(Get-ChildItem -LiteralPath $queue -Recurse -File | Select-Object -ExpandProperty FullName)

    # Offline operation MUST NOT need connection.json or contact any endpoint.
    if (Test-Path (Join-Path $env:LOCALAPPDATA "Concat\GPU\connection.json")) {
        throw "Test precondition: credentials must not exist"
    }
    $target=Join-Path $outDir "User Project.zip"
    $offline=Run-Client $client @("-Operation","OfflineBundle","-ProjectDir",$project,"-BundlePath",$target)
    if ($offline.exit -ne 0) {throw "OfflineBundle failed: $($offline.output -join '; ') "}
    if (!(Test-Path -LiteralPath $target)) {throw "Offline ZIP was not created"}
    $last=@($offline.output | Where-Object {$_ -match '^\{.*\}$'} | ForEach-Object {$_ | ConvertFrom-Json})[-1]
    if ($last.failed -or $last.progress -ne 100 -or $last.location -ne $target) {
        throw "Offline success did not report 100% and the requested destination"
    }
    $percentages=@($offline.output | Where-Object {$_ -match '^\{.*\}$'} | ForEach-Object {($_ | ConvertFrom-Json).progress})
    if (!($percentages -contains 0) -or !($percentages -contains 100)) {
        throw "ZIP preparation does not report progress"
    }

    $gpuZip=[IO.Compression.ZipFile]::OpenRead($queueZip)
    $localZip=[IO.Compression.ZipFile]::OpenRead($target)
    try {
        $gpuNames=@($gpuZip.Entries | ForEach-Object {$_.FullName})
        $localNames=@($localZip.Entries | ForEach-Object {$_.FullName})
        if (($gpuNames -join "|") -cne ($localNames -join "|")) {
            throw "Offline and GPU queue ZIP members/order differ"
        }
        foreach ($name in $gpuNames) {
            if ((Archive-ContentHash $gpuZip $name) -ne (Archive-ContentHash $localZip $name)) {
                throw "ZIP member differs between GPU and Offline: $name"
            }
        }
        $a=Archive-Control $gpuZip
        $b=Archive-Control $localZip
        if ($a.version -ne 1 -or $b.version -ne 1 -or
            $a.project_file -ne "concat.json" -or $b.project_file -ne "concat.json") {
            throw "Worker bundle contract changed"
        }
        $refA=@($a.references | ForEach-Object {"$($_.index):$($_.member):$($_.size):$($_.sha256)"})
        $refB=@($b.references | ForEach-Object {"$($_.index):$($_.member):$($_.size):$($_.sha256)"})
        if (($refA -join "|") -cne ($refB -join "|") -or $refA.Count -ne 3) {
            throw "Worker references changed"
        }
    } finally {
        $gpuZip.Dispose()
        $localZip.Dispose()
    }
    if ((Get-FileHash -LiteralPath $queueZip).Hash -ne $queueHash) {
        throw "Offline ZIP modified the existing GPU queue archive"
    }
    $currentListing=@(Get-ChildItem -LiteralPath $queue -Recurse -File | Select-Object -ExpandProperty FullName)
    if (($queueListing -join "|") -cne ($currentListing -join "|")) {
        throw "Offline ZIP wrote queue or snapshot files"
    }

    # Existing destination must never be replaced or deleted.
    $existingOutput=Join-Path $outDir "preserve.zip"
    [IO.File]::WriteAllBytes($existingOutput,[Text.Encoding]::UTF8.GetBytes("DO NOT REPLACE"))
    $originalOutputHash=(Get-FileHash $existingOutput).Hash
    $second=Run-Client $client @("-Operation","OfflineBundle","-ProjectDir",$project,"-BundlePath",$existingOutput)
    if ($second.exit -eq 0 -or (Get-FileHash $existingOutput).Hash -ne $originalOutputHash) {
        throw "Overwrite protection failed"
    }
    foreach($src in $sourceHashes.Keys) {
        if (!(Test-Path -LiteralPath $src) -or (Get-FileHash $src).Hash -ne $sourceHashes[$src]) {
            throw "Source project/media was changed: $src"
        }
    }
    if (@(Get-ChildItem -LiteralPath $outDir -Filter "*.part").Count -gt 0) {
        throw "Offline ZIP left unfinished archives"
    }
    Write-Host "PASS: exact existing ZIP members and worker contract, offline progress, no credentials, no queue mutation, original media preserved"
} finally {
    $env:LOCALAPPDATA=$originalLocalData
    if (Test-Path -LiteralPath $temp) {
        Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
# The last child intentionally failed an overwrite; restore a successful
# shell exit code after all assertions and cleanup have passed.
$global:LASTEXITCODE = 0
