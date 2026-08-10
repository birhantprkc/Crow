# Does a saved slot survive a SERVER RESTART? A RAM cache cannot pass this by
# construction, which is the whole point: #67 showed a 15-minute pause costing a
# full 23,400-token re-prefill, and raising --cache-ram only moves where the
# volatile copy lives.
#
# Builds a context, saves it, KILLS llama-server, starts it again, restores, and
# reads the verdict off the NEXT turn's prompt_n -- not off the HTTP status. A
# restore reports success either way; only prompt_n says whether the state is
# usable.
#
# Measured 2026-08-08: 2,517 tokens saved (33.6 MiB, 31 ms), restored in 22 ms
# after a restart, next turn re-read 18 tokens. The first run of this script,
# before it replayed reasoning_content, re-read 2,487 of 2,650 -- that run is
# the negative control, and it is why the two functions below exist.
#
# THE SERVER MUST RUN WITH --slot-save-path, and this script restarts it with
# exactly the operating point of the handbook page plus that flag.
#
#   powershell -File tools\measure-slot-restart.ps1

. "$PSScriptRoot\model-paths.ps1"
$ErrorActionPreference = 'Stop'
$U   = 'http://127.0.0.1:8081'
$SP  = 'C:\Users\robin\dev\crow-lab\slotsave'
$EXE = 'C:\Users\robin\AppData\Local\Crow\bin\llama-server.exe'
$MDL = (Get-ModelPath 'operating-point')
$FILE = 'restart-test-2.bin'
$ARGS = @('-m',$MDL,'--port','8081','-c','200000','-ngl','99','-np','1','--jinja',
          '--moe-stream','--moe-stream-cache','64s','--moe-stream-io-threads','8',
          '--moe-stream-direct','--slot-save-path',$SP)

# tools ist NICHT optional: dieses Template behaelt die Gedanken eines frueheren
# Zuges nur, wenn der Request Tools traegt. Ohne sie rendert es <think></think>
# und der Replay verpufft -- gemessen 2026-08-08, #60.
$TOOLS = @(@{
  type='function'
  function=@{ name='read_file'; description='Read a UTF-8 text file from disk.'
              parameters=@{ type='object'; properties=@{ path=@{ type='string' } }; required=@('path') } }
})

function Ask($messages, $maxTok) {
  $body = @{ model='crow'; messages=$messages; tools=$TOOLS; temperature=0.6; stream=$false; max_tokens=$maxTok } | ConvertTo-Json -Depth 8
  $t0 = Get-Date
  $r = Invoke-RestMethod -Uri "$U/v1/chat/completions" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 1800
  [pscustomobject]@{
    content   = $r.choices[0].message.content
    reasoning = $r.choices[0].message.reasoning_content
    prompt_n  = $r.timings.prompt_n
    wall      = [math]::Round(((Get-Date)-$t0).TotalSeconds,1)
  }
}

# Der Fehler des ersten Laufs: ein behaltener Zug OHNE reasoning_content laesst
# einen leeren Think-Block stehen, der Praefix weicht dort ab, wo die Gedanken
# anfingen, und der Test misst seinen eigenen Defekt statt den des Servers.
function AsAssistant($reply) {
  $m = @{ role='assistant'; content=$reply.content }
  if ($reply.reasoning) { $m['reasoning_content'] = $reply.reasoning }
  return $m
}
function WaitReady($limit=90) {
  for ($i=0; $i -lt $limit; $i++) {
    try { if ((Invoke-RestMethod "$U/health" -TimeoutSec 5).status -eq 'ok') { return $true } } catch {}
    Start-Sleep -Seconds 10
  }
  return $false
}

# --- groesseren Kontext bauen: zweiter Punkt fuer die Linearitaet ----------
$msgs = @(@{role='system'; content='You are Crow, a local coding assistant.'})
$qs = @('What is a hash map good for? Two paragraphs.',
        'When is a B-tree better? Two paragraphs.',
        'Explain amortised complexity with a dynamic array. Two paragraphs.',
        'Why is float == risky? Two paragraphs.',
        'What is a Bloom filter good for? Two paragraphs.',
        'Explain Dijkstra briefly. Two paragraphs.',
        'What does fsync guarantee? Two paragraphs.',
        'Explain copy-on-write. Two paragraphs.')
Write-Output "Kontext bauen (8 Zuege, groesser als der erste Lauf)"
foreach ($q in $qs) {
  $msgs += @{role='user'; content=$q}
  $a = Ask $msgs 600
  $msgs += (AsAssistant $a)
  Write-Output ("  prompt_n={0,6}  {1,6}s" -f $a.prompt_n, $a.wall)
}
$held = (Invoke-RestMethod "$U/slots")[0].n_prompt_tokens
Write-Output "Kontext jetzt: $held Tokens"

# --- speichern ------------------------------------------------------------
$t0 = Get-Date
$sv = Invoke-RestMethod -Uri "$U/slots/0?action=save" -Method Post -Body (@{filename=$FILE}|ConvertTo-Json) -ContentType 'application/json' -TimeoutSec 600
$saveWall = [math]::Round(((Get-Date)-$t0).TotalSeconds,3)
$size = (Get-Item (Join-Path $SP $FILE)).Length
Write-Output ""
Write-Output "gespeichert: $($size.ToString('N0')) Bytes  ($([math]::Round($size/1MB,1)) MiB)  in $saveWall s  (Server: $($sv.timings.save_ms) ms)"
Write-Output "pro Token  : $([math]::Round($size/$held,0)) Bytes"

# --- SERVER NEU STARTEN ---------------------------------------------------
Write-Output ""
Write-Output "=== SERVER WIRD NEU GESTARTET -- ab hier kann kein RAM-Cache mehr helfen ==="
Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 5
Start-Process -FilePath $EXE -ArgumentList $ARGS -RedirectStandardError "$SP\server2.log" -RedirectStandardOutput "$SP\server2.out" -WindowStyle Hidden
if (-not (WaitReady)) { Write-Output "FAIL: Server kam nicht hoch"; exit 1 }
Write-Output "Server ist wieder da"

# --- wiederherstellen -----------------------------------------------------
$t0 = Get-Date
$rs = Invoke-RestMethod -Uri "$U/slots/0?action=restore" -Method Post -Body (@{filename=$FILE}|ConvertTo-Json) -ContentType 'application/json' -TimeoutSec 600
$restWall = [math]::Round(((Get-Date)-$t0).TotalSeconds,3)
Write-Output "wiederhergestellt: $($rs.n_restored) Tokens in $restWall s  (Server: $($rs.timings.restore_ms) ms)"

# --- DIE Zahl: haelt der Praefix nach dem Neustart? -----------------------
$msgs += @{role='user'; content='One more: what is a trie good for? Two paragraphs.'}
$after = Ask $msgs 600
Write-Output ""
Write-Output ("=" * 62)
Write-Output "ERGEBNIS"
Write-Output ("=" * 62)
Write-Output "  Kontext vor dem Neustart : $held Tokens"
Write-Output "  neu gelesen danach       : $($after.prompt_n) Tokens"
Write-Output "  wiederverwendet          : $($held - $after.prompt_n) Tokens"
Write-Output "  Dateigroesse             : $([math]::Round($size/1MB,1)) MiB"
Write-Output "  Restore                  : $restWall s"
if ($after.prompt_n -lt ($held * 0.2)) {
  Write-Output "  PRAEFIX HAELT UEBER DEN NEUSTART."
} else {
  Write-Output "  PRAEFIX HAELT NICHT -- Restore meldete Erfolg, Server las trotzdem neu."
}
