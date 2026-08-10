<#
measure-host-peak - what the server actually takes from the host, with and without the tier.

WHY IT EXISTS. On 2026-08-09 the README's headline host-RAM figure (33.46 GiB) had no raw run
behind it. It had been read off a live process during a test and written straight into the
document -- true when it was read, and unverifiable afterwards, which is the same as unmeasured.
The VRAM figure the README tells users to check against (31,838 of 32,607 MiB) had the same
problem.

WHAT IT REPORTS. PeakWorkingSet64 is the number that matters: WorkingSet64 sampled at one moment
misses the peak, and the peak is what a user's machine has to survive. Private bytes are reported
beside it because on CUDA they include mapped device memory and are NOT host RAM -- quoting them
as host memory would overstate it by tens of GiB.

THE ARMS DIFFER IN ONE VALUE, --moe-stream-l2. Both load the same model into the same server, and
both are given the same warm-up request, because a process that never decoded has not reached its
peak.

Usage:
  measure-host-peak.ps1 -Out runs\2026-08-09\host-peak
  measure-host-peak.ps1 -Selftest
#>
param(
    [string] $Exe   = 'C:\Users\robin\dev\crow-lab\wt-25\build-25\bin\Release\llama-server.exe',
    [string] $Model = $null,
    [string] $Lab   = 'C:\Users\robin\dev\crow-lab',
    [int]    $Ctx   = 200000,
    [int]    $Port  = 8081,
    [string] $Out   = '',
    [switch] $Selftest
)

. "$PSScriptRoot\model-paths.ps1"
if (-not $Model) { $Model = Get-ModelPath 'operating-point' }
$ErrorActionPreference = 'Continue'

# Value function: turns one process plus one nvidia-smi line into the row this tool reports.
function Get-Peak {
    param($Proc, [string]$SmiLine)
    if ($null -eq $Proc) { return $null }
    $used, $total = ($SmiLine -split ',') | ForEach-Object { [int]($_ -replace '[^\d]', '') }
    return [pscustomobject]@{
        peak_host_gib = [math]::Round($Proc.PeakWorkingSet64 / 1GB, 2)
        host_gib      = [math]::Round($Proc.WorkingSet64     / 1GB, 2)
        private_gib   = [math]::Round($Proc.PrivateMemorySize64 / 1GB, 2)
        vram_used_mib = $used
        vram_total_mib= $total
    }
}

if ($Selftest) {
    $pass = 0; $fail = 0
    function C($n, $w, $g) { if ("$w" -eq "$g") { $script:pass++; "  PASS  $n" } else { $script:fail++; "  FAIL  $n want=$w got=$g" } }
    Write-Output "measure-host-peak selftest"
    $fake = [pscustomobject]@{ PeakWorkingSet64 = 35929310822; WorkingSet64 = 2211860480; PrivateMemorySize64 = 36265318400 }
    $r = Get-Peak -Proc $fake -SmiLine "32018 MiB, 32607 MiB"
    C 'peak host GiB'  33.46 $r.peak_host_gib
    C 'current is NOT the peak' $true ($r.host_gib -lt $r.peak_host_gib)
    C 'private is reported apart' 33.77 $r.private_gib
    C 'vram used'  32018 $r.vram_used_mib
    C 'vram total' 32607 $r.vram_total_mib
    # A dead process has no peak. Returning a row for it would put a zero in a table.
    C 'no process -> null' $null (Get-Peak -Proc $null -SmiLine "1 MiB, 2 MiB")
    Write-Output ""; Write-Output "$pass passed, $fail failed"
    if ($fail -gt 0) { exit 1 }
    exit 0
}

if (-not $Out) { Write-Output "SETUP ERROR: -Out is required"; exit 2 }
if (-not (Test-Path $Exe)) { Write-Output "MISSING: $Exe"; exit 2 }
New-Item -ItemType Directory -Force -Path $Out | Out-Null

$rows = @()
foreach ($arm in @('l2','base')) {
    $flags = @('--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8','--moe-stream-direct','--jinja')
    if ($arm -eq 'l2') { $flags += @('--moe-stream-l2','32') }
    $log = Join-Path $Out "$arm.err"

    Write-Output ("[{0}] {1}: starting" -f (Get-Date -Format 'HH:mm:ss'), $arm)
    $p = Start-Process -FilePath $Exe -WorkingDirectory $Lab -PassThru -RedirectStandardError $log `
         -ArgumentList (@('-m', $Model, '--host', '127.0.0.1', '--port', "$Port", '-c', "$Ctx",
                          '-ngl', '99', '-np', '1') + $flags)

    $healthy = $false
    for ($i = 0; $i -lt 300; $i++) {
        Start-Sleep -Seconds 2
        try { Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 | Out-Null; $healthy = $true; break } catch {}
    }
    if (-not $healthy) { Write-Output "    INVALID - never became healthy"; Stop-Process -Id $p.Id -Force -EA SilentlyContinue; continue }

    # One decode, so the process has actually reached its working peak. A server that only loaded
    # has not touched the tier at all.
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -TimeoutSec 600 `
            -ContentType 'application/json' `
            -Body (@{ model = 'crow'
                      messages = @(@{ role = 'user'; content = 'Reply with the single word: ok' })
                      max_tokens = 64
                      temperature = (Get-SamplingDefault temperature) } | ConvertTo-Json -Depth 5) | Out-Null
    } catch { Write-Output "    warn: warm-up request failed, peak may be low" }

    $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
    $smi  = (nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader) -join ''
    $r    = Get-Peak -Proc $proc -SmiLine $smi

    # The arm must BE its arm, read back from the server's own log.
    $tier = (Select-String -Path $log -Pattern 'MoE L2 host tier' -Quiet) -eq $true
    if ($tier -ne ($arm -eq 'l2')) { Write-Output "    INVALID - arm '$arm' but tier present = $tier" }
    elseif ($null -eq $r)          { Write-Output "    INVALID - process gone before it was read" }
    else {
        $rows += ($r | Add-Member -NotePropertyName arm -NotePropertyValue $arm -PassThru)
        Write-Output ("    peak host {0,6:N2} GiB   current {1,5:N2}   private {2,6:N2}   VRAM {3:N0} of {4:N0} MiB" -f `
                      $r.peak_host_gib, $r.host_gib, $r.private_gib, $r.vram_used_mib, $r.vram_total_mib)
    }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

if ($rows.Count -eq 0) { Write-Output "NO VALID RUNS"; exit 1 }
$rows | Export-Csv -Path (Join-Path $Out 'host-peak.csv') -NoTypeInformation
Write-Output ""
Write-Output "arm    peak host GiB   VRAM MiB"
foreach ($r in $rows) { '{0,-6} {1,13:N2}   {2:N0} of {3:N0}' -f $r.arm, $r.peak_host_gib, $r.vram_used_mib, $r.vram_total_mib }
exit 0
