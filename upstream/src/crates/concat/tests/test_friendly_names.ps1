$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'src/concat_gpu_client.ps1') -DefineOnly
function Expect([string]$actual,[string]$expected) {if ($actual -cne $expected) {throw "Expected '$expected', got '$actual'"}}
Expect (Friendly-Base 'CON.txt') '_CON.txt'
Expect (Friendly-Base 'My: Project... ') 'My_ Project'
Expect (Friendly-File 'My Video.MP4' '.mp4') 'My Video.mp4'
Expect (Friendly-Numbered 'My Video' 2) 'My Video (2)'
Expect (Friendly-Base '... ') 'Untitled'
$emoji=[char]::ConvertFromUtf32(0x1f3be)
$long=Friendly-Numbered ($emoji*100) 999
if ($long.Length -gt 120 -or [char]::IsHighSurrogate($long[$long.Length-1])) {throw 'UTF16 name truncated unsafely'}
$names=Project-Names ([pscustomobject]@{name='Canonical';outputBasename='Manual: Name'}) 'Fallback'
Expect $names.project_name 'Canonical'
Expect $names.output_filename 'Manual_ Name.mp4'
$temp=Join-Path ([IO.Path]::GetTempPath()) ('concat-naming-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $script:payload=[Text.Encoding]::UTF8.GetBytes('verified-output-fixture')
    [IO.File]::WriteAllText((Join-Path $temp 'My Video.mp4'),'different existing output')
    $script:downloads=0
    function Invoke-WebRequest {param([string]$OutFile) $script:downloads++;[IO.File]::WriteAllBytes($OutFile,$script:payload)}
    $status=[pscustomobject]@{output_basename='My Video';output_sha256=(SHA-Bytes $script:payload);output_size=$script:payload.Length}
    $job='concat-cuda-1234567890abcdef'
    $output=Get-Output 'https://unused.invalid' $job $status @{} $temp
    Expect ([IO.Path]::GetFileName($output)) 'My Video (2).mp4'
    $again=Get-Output 'https://unused.invalid' $job $status @{} $temp
    Expect $again $output
    if ($script:downloads -ne 1) {throw 'Retry duplicated a verified output'}
    Expect ([IO.File]::ReadAllText((Join-Path $temp 'My Video.mp4'))) 'different existing output'
} finally {Remove-Item -LiteralPath $temp -Recurse -Force}
Write-Output 'Friendly filename tests PASS' 
