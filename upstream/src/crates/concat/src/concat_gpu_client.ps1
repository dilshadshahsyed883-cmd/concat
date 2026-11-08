#Requires -Version 5.1
# Embedded in the actual Concat Windows EXE. No additional desktop editor.
# Use only the existing SaladCloud Gateway and existing VPS receiver.
param(
    [ValidateSet("Submit","Save","Test","Prepare","Upload","Status")][string]$Operation="Submit",
    [string]$ProjectDir,
    [string]$Job,
    [string]$SnapshotID,
    [string]$JobDomain,
    [switch]$BundleOnly,
    [string]$BundlePath,
    [switch]$DefineOnly
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
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
function Concat-Bundle([string]$dir,[string]$zip,[string]$snapshot="") {
    $manifest=Join-Path $dir "concat.json"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { $manifest=Join-Path $dir "wolfcut.json" }
    if ($snapshot) {$manifest=$snapshot}
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
    Report "Packaged $($lookup.Count) media files." 0
}
function Format-ByteSize([long]$bytes) {
    if ($bytes -lt 0) { $bytes = 0 }
    if ($bytes -ge 1GB) { return ("{0:0.00} GB" -f ($bytes / 1GB)) }
    if ($bytes -ge 1MB) { return ("{0:0.00} MB" -f ($bytes / 1MB)) }
    if ($bytes -ge 1KB) { return ("{0:0.00} KB" -f ($bytes / 1KB)) }
    return ("{0} B" -f $bytes)
}

function Format-UploadProgress([long]$transferred, [long]$total) {
    if ($total -le 0) { return "0 B / 0 B (0%)" }
    $pct = [int][Math]::Min(100, [Math]::Max(0, [Math]::Floor(([double]$transferred / [double]$total) * 100)))
    $tStr = Format-ByteSize $transferred
    $totStr = Format-ByteSize $total
    return "$tStr / $totStr ($pct%)"
}

function Submit-Zip([string]$zip,[string]$job,[string]$domain,$headers) {
    $size=(Get-Item -LiteralPath $zip).Length
    # Surface the real package size even before worker negotiation or hashing.
    Report "GPU upload package: $(Format-UploadProgress 0 $size)" 0 "" $false "" "" 0 $size
    $sha=SHA-File $zip
    $root="$domain/api/uploads/$job"
    $existing=$null
    try {$existing=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/api/jobs/$job" -Headers $headers -TimeoutSec 30}
    catch {
        $response=$_.Exception.Response
        if (-not $response -or [int]$response.StatusCode -ne 404) {throw}
    }
    if ($existing) {
        if ($existing.sha256 -ne $sha -or [long]$existing.size -ne $size) {
            throw "Existing worker job does not match the saved snapshot."
        }
        $formatted = Format-UploadProgress $size $size
        Report "Resuming job $job ($($existing.state)). $formatted" 100 "" $false "" "" $size $size
        return
    }
    $manifest=@{size=[long]$size;sha256=$sha;render_mode="cuda-hybrid-v1"} | ConvertTo-Json -Compress
    $session=Invoke-RestMethod -MaximumRedirection 0 -Method Post -Uri $root -Headers $headers -ContentType "application/json" -Body $manifest -TimeoutSec 30
    if ($session.render_mode -ne "cuda-hybrid-v1") {throw "This worker does not support the matching CUDA hybrid render mode"}
    $chunkSize=[long]$session.chunk_size
    $count=[int]$session.total_chunks
    if ($chunkSize -lt 1 -or $chunkSize -gt 8388608 -or $count -ne [int][Math]::Ceiling($size/$chunkSize)) {
        throw "Invalid Salad chunk layout"
    }
    $seen=@($session.uploaded)
    $uploadedMap = New-Object 'System.Collections.Generic.HashSet[int]'
    [long]$transferredBytes = 0
    foreach ($idx in $seen) {
        if ($idx -ge 0 -and $idx -lt $count -and $uploadedMap.Add([int]$idx)) {
            $chunkLen = [long][Math]::Min($chunkSize, ($size - [long]$idx * $chunkSize))
            if ($chunkLen -gt 0) { $transferredBytes += $chunkLen }
        }
    }
    $input=[IO.File]::OpenRead($zip)
    try {
        for($i=0;$i -lt $count;$i++) {
            $chunkLen = [long][Math]::Min($chunkSize, ($size - [long]$i * $chunkSize))
            if ($seen -contains $i) {
                if ($uploadedMap.Add([int]$i)) {
                    $transferredBytes += $chunkLen
                }
                $pct = if ($size -gt 0) { [int][Math]::Min(100, [Math]::Max(0, [Math]::Floor(([double]$transferredBytes / [double]$size) * 100))) } else { 100 }
                $formatted = Format-UploadProgress $transferredBytes $size
                Report "Verified upload chunk $($i+1) of $count ($formatted)." $pct "" $false "" "" $transferredBytes $size
                continue
            }
            $length=[int]$chunkLen
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
                    Invoke-RestMethod -MaximumRedirection 0 -Method Put -Uri "$root/chunks/$i" -Headers $h -ContentType "application/octet-stream" -Body $bytes -TimeoutSec 95 | Out-Null
                    $ok=$true
                    break
                } catch {
                    try {
                        $check=Invoke-RestMethod -MaximumRedirection 0 -Uri $root -Headers $headers -TimeoutSec 30
                        if (@($check.uploaded) -contains $i) {$ok=$true;break}
                    } catch {}
                    if ($attempt -eq 3) {throw}
                    Start-Sleep -Seconds (2*$attempt)
                }
            }
            if (-not $ok) {throw "Upload failed at chunk $i"}
            if ($uploadedMap.Add([int]$i)) {
                $transferredBytes += $chunkLen
            }
            $pct = if ($size -gt 0) { [int][Math]::Min(100, [Math]::Max(0, [Math]::Floor(([double]$transferredBytes / [double]$size) * 100))) } else { 100 }
            $formatted = Format-UploadProgress $transferredBytes $size
            Report "Uploaded chunk $($i+1) of $count ($formatted)." $pct "" $false "" "" $transferredBytes $size
        }
    } finally {$input.Dispose()}
    $done=Invoke-RestMethod -MaximumRedirection 0 -Method Post -Uri "$root/complete" -Headers $headers -TimeoutSec 95
    $formatted = Format-UploadProgress $size $size
    Report "Salad verified the upload ($($done.state)). $formatted" 100 "" $false "" "" $size $size
}
function Report([string]$message,[int]$progress=-1,[string]$location="",[bool]$failed=$false,[string]$phase="",[string]$metrics="",[long]$transferred_bytes=-1,[long]$total_bytes=-1) {
    if ($headers) {
        foreach ($secret in @($headers["Salad-Api-Key"],$headers.Authorization.Substring(7))) {
            if ($secret) {$message=$message.Replace($secret,"[redacted]")}
        }
    }
    $obj=[ordered]@{message=$message;progress=$progress;location=$location;failed=$failed;phase=$phase;metrics=$metrics}
    if ($transferred_bytes -ge 0) { $obj["transferred_bytes"] = $transferred_bytes }
    if ($total_bytes -ge 0) { $obj["total_bytes"] = $total_bytes }
    [Console]::WriteLine(($obj | ConvertTo-Json -Compress))
}

function Format-RenderMetrics($status) {
    # Older workers omit the fields; they must remain compatible.
    if ($null -eq $status.render_time_seconds -or $null -eq $status.average_fps -or
        $null -eq $status.speed_multiplier) {return ""}
    try {
        $seconds=[double]$status.render_time_seconds
        $fps=[double]$status.average_fps
        $speed=[double]$status.speed_multiplier
        if ($seconds -le 0 -or $fps -le 0 -or $speed -le 0 -or
            [double]::IsNaN($seconds) -or [double]::IsInfinity($seconds) -or
            [double]::IsNaN($fps) -or [double]::IsInfinity($fps) -or
            [double]::IsNaN($speed) -or [double]::IsInfinity($speed)) {return ""}
        return ("Render Time: {0:0.00} s | Average FPS: {1:0.00} | Speed: {2:0.00}x" -f $seconds,$fps,$speed)
    } catch {return ""}
}

function Read-Connection([string]$configFile) {
    if (Test-Path -LiteralPath $configFile) {
        return Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
}

function Resolve-Connection($saved,$draft) {
    if ($null -eq $draft) {
        if (!$saved) {throw "Set up Salad in Settings > GPU Config before sending a project."}
        $saved.domain=Gateway $saved.domain
        return $saved
    }
    $domain=Gateway ([string]$draft.domain)
    if ($saved -and $saved.domain -ne $domain -and (!$draft.salad -or !$draft.renderer)) {
        throw "Enter both credentials when changing the Salad access domain."
    }
    if ($draft.renderer -and ([string]$draft.renderer).Trim().Length -lt 32) {
        throw "Renderer API Token must contain at least 32 characters."
    }
    $salad=if ($draft.salad) {
        ConvertFrom-SecureString (ConvertTo-SecureString ([string]$draft.salad).Trim() -AsPlainText -Force)
    } elseif ($saved) {$saved.salad}
    $renderer=if ($draft.renderer) {
        ConvertFrom-SecureString (ConvertTo-SecureString ([string]$draft.renderer).Trim() -AsPlainText -Force)
    } elseif ($saved) {$saved.renderer}
    if (!$salad -or !$renderer) {throw "Enter the Salad API Key and Renderer API Token."}
    [pscustomobject]@{domain=$domain;salad=$salad;renderer=$renderer}
}

function Save-Connection($config,[string]$path) {
    # Windows PowerShell encrypts these two values with user-scoped DPAPI.
    $temporary=$path+"."+[Guid]::NewGuid().ToString("N")+".tmp"
    try {
        [IO.File]::WriteAllText($temporary,($config | ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($path)) {[IO.File]::Replace($temporary,$path,$null)}
        else {[IO.File]::Move($temporary,$path)}
    } finally {
        Remove-Item -LiteralPath $temporary -ErrorAction SilentlyContinue
    }
}

function New-ConnectionHeaders($config) {
    $saladPtr=[IntPtr]::Zero
    $rendererPtr=[IntPtr]::Zero
    try {
        $saladPtr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR((ConvertTo-SecureString ([string]$config.salad)))
        $rendererPtr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR((ConvertTo-SecureString ([string]$config.renderer)))
        $saladKey=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($saladPtr)
        $rendererKey=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($rendererPtr)
        if (!$saladKey -or $rendererKey.Length -lt 32) {throw "Invalid saved credentials."}
        @{"Salad-Api-Key"=$saladKey;"Authorization"="Bearer $rendererKey"}
    } catch {
        throw "Saved credentials could not be unlocked. Re-enter both keys in GPU Config."
    } finally {
        if ($saladPtr -ne [IntPtr]::Zero) {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($saladPtr)}
        if ($rendererPtr -ne [IntPtr]::Zero) {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($rendererPtr)}
    }
}

function Test-Worker([string]$domain,$headers) {
    $health=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/health/ready" -Headers $headers -TimeoutSec 30
    if (!$health.ok -or $health.cuda_stage1 -ne $true) {
        throw "The worker is not ready for Concat CUDA Stage 1."
    }
    # Readiness can be public. This route also verifies the renderer token.
    $diagnostics=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/api/diagnostics/gpu" -Headers $headers -TimeoutSec 30
    if ($diagnostics.cuda_stage1 -ne $true -or !$diagnostics.ffmpeg_available -or !$diagnostics.ffprobe_available) {
        throw "The worker lacks the required Concat CUDA tools."
    }
    try {
        $capabilities=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/api/capabilities" -Headers $headers -TimeoutSec 30
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) {
            throw "The Salad worker uses an older image. Start the matching Concat GPU candidate."
        }
        throw
    }
    if ($capabilities.api_version -ne 1 -or @($capabilities.bundle_versions) -notcontains 1 -or
        @($capabilities.render_modes) -notcontains "cuda-hybrid-v1" -or
        $capabilities.validation_version -ne 1) {
        throw "The Salad worker image does not match this Concat GPU build."
    }
}


function Validate-WorkerProject([string]$manifest,[string]$domain,$headers) {
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "GPU project manifest missing; save the project again."
    }
    $payload=[IO.File]::ReadAllBytes($manifest)
    if (!$payload.Length -or $payload.Length -gt 2MB) {
        throw "GPU project manifest is empty or exceeds the 2 MiB compatibility limit."
    }
    $result=Invoke-RestMethod -MaximumRedirection 0 -Method Post -Uri "$domain/api/validate" -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 45
    if ($result.ok -ne $true) {
        $reason=@($result.errors) -join "; "
        if (!$reason) {$reason="Unknown unsupported project feature"}
        throw "GPU project compatibility failed: $reason"
    }
    Report "GPU compatibility verified before media upload." 0
}

function Get-Output([string]$domain,[string]$job,$status,$headers,[string]$outputs) {
    if ($job -cnotmatch '^concat-cuda-[a-f0-9]{16}$' -or $status.output_sha256 -cnotmatch '^[a-f0-9]{64}$' -or
        [long]$status.output_size -le 0) {throw "The worker did not provide valid MP4 integrity metadata."}
    New-Item -ItemType Directory -Force -Path $outputs | Out-Null
    $output=Join-Path $outputs "$job.mp4"
    if (Test-Path -LiteralPath $output) {
        if ((SHA-File $output) -eq $status.output_sha256) {return $output}
        throw "The existing output has a different checksum. Move it before retrying."
    }
    $partial=$output+"."+[Guid]::NewGuid().ToString("N")+".part"
    try {
        Invoke-WebRequest -UseBasicParsing -MaximumRedirection 0 -Uri "$domain/api/jobs/$job/output" -Headers $headers -TimeoutSec 900 -OutFile $partial
        if ((Get-Item -LiteralPath $partial).Length -ne [long]$status.output_size -or
            (SHA-File $partial) -ne $status.output_sha256) {throw "Downloaded MP4 failed checksum verification."}
        [IO.File]::Move($partial,$output)
        return $output
    } finally {
        Remove-Item -LiteralPath $partial -ErrorAction SilentlyContinue
    }
}

function Send-Project([string]$root,[string]$gpuDataDir,[string]$domain,$headers) {
    $projectManifest=Join-Path $root "concat.json"
    if (!(Test-Path -LiteralPath $projectManifest -PathType Leaf)) {$projectManifest=Join-Path $root "wolfcut.json"}
    if (!(Test-Path -LiteralPath $projectManifest -PathType Leaf)) {throw "Save a Concat project before sending it."}
    Validate-WorkerProject $projectManifest $domain $headers
    $jobs=Join-Path $gpuDataDir "Jobs"
    New-Item -ItemType Directory -Force -Path $jobs | Out-Null
    $stateFile=Join-Path $gpuDataDir ("upload-"+(SHA-Bytes ([Text.Encoding]::UTF8.GetBytes($root))).Substring(0,24)+".json")
    $fp=SHA-File $projectManifest
    $state=$null
    if (Test-Path -LiteralPath $stateFile) {
        $state=Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if ($state -and $state.fingerprint -eq $fp -and $state.domain -eq $domain -and $state.mode -eq "cuda-hybrid-v1" -and
        $state.job -cmatch '^concat-cuda-[a-f0-9]{16}$' -and
        $state.zip -eq (Join-Path $jobs ($state.job+".zip")) -and (Test-Path -LiteralPath $state.zip -PathType Leaf)) {
        $job=[string]$state.job
        $zip=[string]$state.zip
        Report "Resuming saved project upload $job." 0
    } else {
        $job="concat-cuda-"+[Guid]::NewGuid().ToString("N").Substring(0,16)
        $zip=Join-Path $jobs ($job+".zip")
        Report "Preparing project media for upload." 0
        try {Concat-Bundle $root $zip}
        catch {Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue;throw}
        @{job=$job;zip=$zip;domain=$domain;fingerprint=$fp;mode="cuda-hybrid-v1"} |
            ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    }
    Submit-Zip $zip $job $domain $headers
    $deadline=[DateTime]::UtcNow.AddHours(2)
    $lastState=""
    :poll while ([DateTime]::UtcNow -lt $deadline) {
        $status=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/api/jobs/$job" -Headers $headers -TimeoutSec 30
        if ($status.state -ne $lastState) {
            Report "Job $job · $($status.state)" 100
            $lastState=$status.state
        }
        switch ($status.state) {
            "delivered" {
                Report "Render delivered and verified." 100 "VPS Cloud Render · $job.mp4"
                break
            }
            {$_ -in "rendered","delivery_failed"} {
                Report "Downloading and verifying the MP4." 100
                try {$output=Get-Output $domain $job $status $headers (Join-Path $gpuDataDir "Outputs")}
                catch {
                    # The worker can move from rendered to delivering before this GET.
                    if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404 -and $_.Exception.Response.ResponseUri.AbsolutePath.EndsWith("/output")) {
                        Start-Sleep -Seconds 2
                        continue poll
                    }
                    throw
                }
                if ($status.state -eq "delivery_failed") {
                    Report ("MP4 downloaded and verified. VPS delivery failed: "+[string]$status.error) 100 $output $true
                } else {Report "Render complete. MP4 downloaded and verified." 100 $output}
                break
            }
            "failed" {
                Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
                throw ("GPU render failed: "+[string]$status.error)
            }
        }
        if ($status.state -eq "delivered" -or $output) {
            Remove-Item -LiteralPath $zip,$stateFile -ErrorAction SilentlyContinue
            return
        }
        Start-Sleep -Seconds 6
    }
    throw "GPU job timed out. Send the same saved project to resume status checking."
}

function Queue-Status([string]$job,[string]$domain,$headers,[string]$gpuDataDir) {
    $status=Invoke-RestMethod -MaximumRedirection 0 -Uri "$domain/api/jobs/$job" -Headers $headers -TimeoutSec 20
    $metrics=Format-RenderMetrics $status
    switch ($status.state) {
        "queued" {Report "Uploaded. Waiting in the worker queue." 100 "" $false "queued" $metrics}
        "running" {Report "GPU rendering." 100 "" $false "rendering" $metrics}
        "delivering" {Report "Delivering the rendered MP4." 100 "" $false "rendering" $metrics}
        "delivered" {Report "Render delivered and verified." 100 "VPS Cloud Render - $job.mp4" $false "completed" $metrics}
        {$_ -in "rendered","delivery_failed"} {
            Report "Downloading and verifying the MP4." 100
            try {$output=Get-Output $domain $job $status $headers (Join-Path $gpuDataDir "Outputs")}
            catch {
                if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) {
                    Report "Waiting for the worker to finish delivery." 100 "" $false "rendering" $metrics
                    return
                }
                throw
            }
            if ($status.state -eq "delivery_failed") {
                Report ("MP4 downloaded. Worker delivery failed: "+[string]$status.error) 100 $output $false "completed" $metrics
            } else {Report "MP4 downloaded and verified." 100 $output $false "completed" $metrics}
        }
        "failed" {Report ("GPU render failed: "+[string]$status.error) 100 "" $true "render_failed" $metrics}
        default {throw "The worker returned an unknown render state."}
    }
}

if ($DefineOnly) {return}
if ($BundleOnly) {
    if (!$BundlePath) {throw "Offline bundle test requires -BundlePath"}
    Concat-Bundle (Resolve-Path -LiteralPath $ProjectDir).ProviderPath $BundlePath
    return
}

$headers=$null
try {
    if (!$env:LOCALAPPDATA) {throw "Windows did not provide a local application data directory."}
    $gpuDataDir=Join-Path $env:LOCALAPPDATA "Concat\GPU"
    if ($Operation -in "Prepare","Upload","Status") {
        if ($Job -cnotmatch '^concat-cuda-[a-f0-9]{16}$' -or
            $SnapshotID -cnotmatch '^concat-cuda-[a-f0-9]{16}$') {throw "Invalid queue job identifier."}
        $queueDir=Join-Path $gpuDataDir "Queue"
        $zip=Join-Path $queueDir ($SnapshotID+".zip")
        if ($Operation -eq "Prepare") {
            if (!(Test-Path -LiteralPath $zip)) {
                $stage=$zip+"."+[Guid]::NewGuid().ToString("N")+".part"
                $manifest=Join-Path (Join-Path $queueDir $SnapshotID) "concat.json"
                try {
                    Concat-Bundle (Resolve-Path -LiteralPath $ProjectDir).ProviderPath $stage $manifest
                    [IO.File]::Move($stage,$zip)
                } finally {Remove-Item -LiteralPath $stage -ErrorAction SilentlyContinue}
            }
            Report "Project snapshot ready." 0 "" $false "prepared"
            return
        }
    }
    $configFile=Join-Path $gpuDataDir "connection.json"
    $saved=Read-Connection $configFile
    $draft=$null
    if ($Operation -in "Save","Test") {
        $draft=[Console]::In.ReadToEnd() | ConvertFrom-Json
        if (!$draft) {throw "GPU settings were not provided."}
    }
    $config=Resolve-Connection $saved $draft
    $headers=New-ConnectionHeaders $config
    if ($Operation -in "Upload","Status") {
        if ($config.domain -ne $JobDomain) {throw "Restore the saved worker address for this job before resuming."}
        if ($Operation -eq "Upload") {
            try {
                # A helper can outlive the editor. Its lease prevents the
                # restarted editor from opening a second upload session.
                $lease=[IO.File]::Open((Join-Path $queueDir "upload.lock"),
                    [IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
            } catch [IO.IOException] {
                Report "The previous upload is still active. Click Start Queue again after it stops." -1 "" $true "offline"
                return
            }
            try {
                Report "Checking the worker before uploading." 0
                Test-Worker $config.domain $headers
                Validate-WorkerProject (Join-Path (Join-Path $queueDir $SnapshotID) "concat.json") $config.domain $headers
                Report "Uploading the saved snapshot." 0 "" $false "attempted"
                Submit-Zip $zip $Job $config.domain $headers
                $zipSize = (Get-Item -LiteralPath $zip).Length
                $formatted = Format-UploadProgress $zipSize $zipSize
                Report "Upload verified. Waiting for GPU rendering ($formatted)." 100 "" $false "uploaded" "" $zipSize $zipSize
            } finally {$lease.Dispose()}
        } else {
            Queue-Status $Job $config.domain $headers $gpuDataDir
        }
        return
    }
    if ($Operation -eq "Save") {
        New-Item -ItemType Directory -Force -Path $gpuDataDir | Out-Null
        Save-Connection $config $configFile
        Report "GPU settings saved for this Windows user." -1 $config.domain
    } else {
        Report "Checking Salad gateway and renderer authentication."
        Test-Worker $config.domain $headers
        if ($Operation -eq "Test") {Report "Connected. The authenticated CUDA worker is ready."}
        else {Send-Project (Resolve-Path -LiteralPath $ProjectDir).ProviderPath $gpuDataDir $config.domain $headers}
    }
} catch {
    $message=$_.Exception.Message
    $phase=""
    if ($Operation -in "Upload","Status" -and $_.Exception -is [Net.WebException] -and
        (!$_.Exception.Response -or [int]$_.Exception.Response.StatusCode -in 502,503,504)) {
        $phase="offline"
    }
    if ($_.Exception.Response) {
        $code=[int]$_.Exception.Response.StatusCode
        $message=switch ($code) {
            {$_ -in 401,403} {"Salad or the renderer rejected authentication. Check both keys in GPU Config."}
            404 {"The worker endpoint was not found. Check the Salad access domain and worker image."}
            429 {"The worker is busy. Try again shortly."}
            503 {"The Salad worker is not ready. Check the container group."}
            default {"The Salad request failed (HTTP $code). Check the worker logs."}
        }
    }
    Report $message -1 "" $true $phase
    exit 1
}
