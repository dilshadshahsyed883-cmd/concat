#Requires -Version 5.1
# Embedded in the actual Concat Windows EXE. No additional desktop editor.
# Use only the existing SaladCloud Gateway and existing VPS receiver.
param([Parameter(Mandatory=$true)][string]$ProjectDir,[switch]$BundleOnly,[string]$BundlePath="")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function SHA-Bytes([byte[]]$bytes) {
    $h=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($h.ComputeHash($bytes))).Replace("-","").ToLowerInvariant() }
    finally { $h.Dispose() }
}
function SHA-File([string]$file) {
    return (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Gateway([string]$raw) {
    $value=$raw.Trim().TrimEnd("/")
    $u=$null
    if (-not [Uri]::TryCreate($value,[UriKind]::Absolute,[ref]$u)) { throw "Invalid Salad HTTPS Gateway URL" }
    if ($u.Scheme -ne "https" -or -not $u.Host.ToLowerInvariant().EndsWith(".salad.cloud") -or $u.UserInfo -or $u.Port -ne 443 -or $u.AbsolutePath -ne "/" -or $u.Query -or $u.Fragment) {
        throw "Use the existing Salad Gateway HTTPS origin only"
    }
    return $value
}
function Concat-Bundle([string]$dir,[string]$zip) {
    $manifest=Join-Path $dir "concat.json"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { $manifest=Join-Path $dir "wolfcut.json" }
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "No saved Concat project (concat.json)" }
    $project=Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $project.media) { throw "Concat document has no media table" }
    $media=@($project.media)
    if ($media.Count -gt 20000) { throw "Too many media records" }
    $lookup=New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
    $refs=New-Object 'System.Collections.Generic.List[object]'
    $total=[long]0
    for ($i=0;$i -lt $media.Count;$i++) {
        $raw=[string]$media[$i].path
        if (-not $raw) { continue }
        $candidate=if ([IO.Path]::IsPathRooted($raw)) {$raw} else {Join-Path $dir $raw}
        $source=(Resolve-Path -LiteralPath $candidate -ErrorAction Stop).ProviderPath
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing input media" }
        if (-not $lookup.ContainsKey($source)) {
            $ext=[IO.Path]::GetExtension($source).ToLowerInvariant()
            if ($ext -notmatch '^\.[a-z0-9]{1,12}$') {$ext=""}
            $id=(SHA-Bytes ([Text.Encoding]::UTF8.GetBytes($source))).Substring(0,32)
            $name="media/$id$ext"
            $info=Get-Item -LiteralPath $source
            $total+=[long]$info.Length
            if ($total -gt 4GB) {throw "Project media exceeds 4 GiB Salad limit"}
            $lookup.Add($source,[pscustomobject]@{
                member=$name;source=$source;size=[long]$info.Length;sha256=(SHA-File $source)
            })
        }
        $item=$lookup[$source]
        [void]$refs.Add(@{index=[int]$i;member=$item.member;size=$item.size;sha256=$item.sha256})
    }
    $stream=[IO.File]::Open($zip,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
    try {
        $archive=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Create,$true)
        try {
            $control=@{version=1;project_file=[IO.Path]::GetFileName($manifest);references=@($refs.ToArray())} | ConvertTo-Json -Depth 8 -Compress
            $entry=$archive.CreateEntry("bundle.json",[IO.Compression.CompressionLevel]::Optimal)
            $writer=[IO.StreamWriter]::new($entry.Open(),[Text.UTF8Encoding]::new($false))
            try {$writer.Write($control)} finally {$writer.Dispose()}
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive,$manifest,("project/"+[IO.Path]::GetFileName($manifest)),[IO.Compression.CompressionLevel]::Optimal) | Out-Null
            foreach($f in $lookup.Values) {
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive,$f.source,$f.member,[IO.Compression.CompressionLevel]::NoCompression) | Out-Null
            }
        } finally {$archive.Dispose()}
    } finally {$stream.Dispose()}
    Write-Host "Original Concat media preserved. Packaged $($lookup.Count) files."
}
function Submit-Zip([string]$zip,[string]$job,[string]$domain,$headers) {
    $size=(Get-Item -LiteralPath $zip).Length
    $sha=SHA-File $zip
    $root="$domain/api/uploads/$job"
    $existing=$null
    try {$existing=Invoke-RestMethod -Uri "$domain/api/jobs/$job" -Headers $headers -TimeoutSec 30}
    catch {
        $response=$_.Exception.Response
        if (-not $response -or [int]$response.StatusCode -ne 404) {throw}
    }
    if ($existing) {
        Write-Host "Resuming already submitted job: $job / $($existing.state)"
        return
    }
    $manifest=@{size=[long]$size;sha256=$sha;render_mode="cuda-stage1"} | ConvertTo-Json -Compress
    $session=Invoke-RestMethod -Method Post -Uri $root -Headers $headers -ContentType "application/json" -Body $manifest -TimeoutSec 30
    if ($session.render_mode -ne "cuda-stage1") {throw "GPU image is outdated: CUDA Stage 1 upload not supported"}
    $chunkSize=[long]$session.chunk_size
    $count=[int]$session.total_chunks
    if ($chunkSize -lt 1 -or $chunkSize -gt 8388608 -or $count -ne [int][Math]::Ceiling($size/$chunkSize)) {
        throw "Invalid Salad chunk layout"
    }
    $seen=@($session.uploaded)
    $input=[IO.File]::OpenRead($zip)
    try {
        for($i=0;$i -lt $count;$i++) {
            if ($seen -contains $i) {Write-Host "Chunk $($i+1)/$count already verified";continue}
            $length=[int][Math]::Min($chunkSize,($size-[long]$i*$chunkSize))
            $bytes=New-Object byte[] $length
            [void]$input.Seek(([long]$i*$chunkSize),[IO.SeekOrigin]::Begin)
            $offset=0
            while($offset -lt $length) {
                $n=$input.Read($bytes,$offset,$length-$offset)
                if ($n -le 0) {throw "Unexpected ZIP EOF"}
                $offset+=$n
            }
            $h=@{
                "Salad-Api-Key"=$headers["Salad-Api-Key"];
                "Authorization"=$headers["Authorization"];
                "X-Content-SHA256"=(SHA-Bytes $bytes)
            }
            $ok=$false
            for($attempt=1;$attempt -le 3;$attempt++) {
                try {
                    Invoke-RestMethod -Method Put -Uri "$root/chunks/$i" -Headers $h -ContentType "application/octet-stream" -Body $bytes -TimeoutSec 95 | Out-Null
                    $ok=$true
                    break
                } catch {
                    try {
                        $check=Invoke-RestMethod -Uri $root -Headers $headers -TimeoutSec 30
                        if (@($check.uploaded) -contains $i) {$ok=$true;break}
                    } catch {}
                    if ($attempt -eq 3) {throw}
                    Start-Sleep -Seconds (2*$attempt)
                }
            }
            if (-not $ok) {throw "Upload failed at chunk $i"}
            Write-Host "Uploaded $($i+1) of $count chunks"
        }
    } finally {$input.Dispose()}
    $done=Invoke-RestMethod -Method Post -Uri "$root/complete" -Headers $headers -TimeoutSec 95
    Write-Host "ZIP checksum verified by Salad: $($done.state)"
}
$root=(Resolve-Path -LiteralPath $ProjectDir).ProviderPath
$projectManifest=Join-Path $root "concat.json"
if (-not (Test-Path -LiteralPath $projectManifest -PathType Leaf)) {
    $projectManifest=Join-Path $root "wolfcut.json"
}
if (-not (Test-Path -LiteralPath $projectManifest -PathType Leaf)) {throw "Save a real Concat project first"}
if ($BundleOnly) {
    if (-not $BundlePath) {throw "Offline bundle test requires -BundlePath"}
    Concat-Bundle $root $BundlePath
    Write-Host "Offline native Concat ZIP contract: PASS"
    return
}
$home=Join-Path $env:LOCALAPPDATA "Concat\GPU"
$jobs=Join-Path $home "Jobs"
New-Item -ItemType Directory -Force -Path $jobs | Out-Null
$configFile=Join-Path $home "connection.json"
$stateFile=Join-Path $home ("upload-"+(SHA-Bytes ([Text.Encoding]::UTF8.GetBytes($root))).Substring(0,24)+".json")
$config=$null
if (Test-Path -LiteralPath $configFile) {
    $config=Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
}
if ($null -eq $config) {
    Write-Host "First use: configure EXISTING Salad connection. No VPS settings change."
    $domain=Gateway (Read-Host "Salad Gateway HTTPS URL")
    $saladSec=Read-Host "Salad API Key (hidden, encrypted with Windows DPAPI)" -AsSecureString
    $renderSec=Read-Host "RENDERER_API_TOKEN (hidden, encrypted with Windows DPAPI)" -AsSecureString
    $config=[pscustomobject]@{
        domain=$domain;
        salad=($saladSec | ConvertFrom-SecureString);
        renderer=($renderSec | ConvertFrom-SecureString)
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding UTF8
}
$domain=Gateway $config.domain
$sPtr=[IntPtr]::Zero
$rPtr=[IntPtr]::Zero
try {
    $sPtr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR((ConvertTo-SecureString ([string]$config.salad)))
    $rPtr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR((ConvertTo-SecureString ([string]$config.renderer)))
    $saladKey=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($sPtr)
    $rendererKey=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($rPtr)
    if (-not $saladKey -or $rendererKey.Length -lt 32) {throw "Missing Salad authentication"}
    $headers=@{"Salad-Api-Key"=$saladKey;"Authorization"="Bearer $rendererKey"}
    $health=Invoke-RestMethod -Uri "$domain/health/ready" -Headers $headers -TimeoutSec 30
    if (-not $health.ok) {throw "Existing Salad worker is not ready"}
    if ($health.cuda_stage1 -ne $true) {
        throw "Existing Salad image lacks native Concat CUDA Stage 1. Upgrade matching GPU image before uploading media."
    }
    $fp=SHA-File $projectManifest
    $state=$null
    if (Test-Path -LiteralPath $stateFile) {
        $state=Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if ($state -and $state.fingerprint -eq $fp -and $state.domain -eq $domain -and (Test-Path -LiteralPath $state.zip -PathType Leaf)) {
        $job=[string]$state.job
        $zip=[string]$state.zip
        Write-Host "Resuming saved project ZIP snapshot: $job"
    } else {
        $job="concat-cuda-"+[Guid]::NewGuid().ToString("N").Substring(0,16)
        $zip=Join-Path $jobs ($job+".zip")
        try {Concat-Bundle $root $zip}
        catch {Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue;throw}
        @{job=$job;zip=$zip;domain=$domain;fingerprint=$fp} |
            ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    }
    Submit-Zip $zip $job $domain $headers
    Write-Host "CUDA render running. Checking existing VPS delivery..."
    $deadline=[DateTime]::UtcNow.AddHours(2)
    while([DateTime]::UtcNow -lt $deadline) {
        $status=Invoke-RestMethod -Uri "$domain/api/jobs/$job" -Headers $headers -TimeoutSec 30
        Write-Host "GPU job $job : $($status.state)"
        if ($status.state -eq "delivered") {
            Write-Host "SUCCESS: MP4 verified on existing VPS Cloud Render as $job.mp4"
            Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
            break
        }
        if ($status.state -eq "rendered") {
            Write-Warning "Render complete; worker VPS delivery not configured. Output remains on Salad."
            break
        }
        if ($status.state -in @("failed","delivery_failed")) {
            throw ("GPU job failed: "+[string]$status.error)
        }
        Start-Sleep -Seconds 6
    }
    if ([DateTime]::UtcNow -ge $deadline) {throw "GPU job timed out; ZIP snapshot kept for recovery"}
} finally {
    if ($sPtr -ne [IntPtr]::Zero) {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($sPtr)}
    if ($rPtr -ne [IntPtr]::Zero) {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($rPtr)}
    Remove-Variable saladKey,rendererKey,headers -ErrorAction SilentlyContinue
}
