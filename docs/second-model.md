# Second model — DeepSeek-V4-Flash-0731

Not the operating point. [`../README.md`](../README.md) is Qwen3.8-27B; this page is the other
server `install.ps1` sets up, kept current because the installer starts it.

| | |
|---|---|
| Model | `DeepSeek-V4-Flash-0731`, `UD-IQ2_XXS` |
| Architecture | MoE, 304B total, 13.3B active |
| Experts | streamed off the SSD, not resident |
| Port | 8081 |
| Context | `-c 200000`, one slot (`-np 1`) |
| KV | f16 |
| Template | `manifests/0731-chat-template.jinja` — the GGUF's embedded one fails its own golden vector 4 |
| Source of truth | [`../manifests/operating-point.json`](../manifests/operating-point.json), key `operating-point` |

---

## Model

```powershell
hf download unsloth/DeepSeek-V4-Flash-0731-GGUF --include "*UD-IQ2_XXS*" --local-dir $env:LOCALAPPDATA\Crow\models\0731-gguf
```

## Server

```powershell
$env:LOCALAPPDATA\Crow\bin\llama-server.exe `
  -m $env:LOCALAPPDATA\Crow\models\0731-gguf\UD-IQ2_XXS\DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00003.gguf `
  --port 8081 -c 200000 -ngl 99 -np 1 --jinja `
  --slot-save-path $env:LOCALAPPDATA\Crow\session `
  --chat-template-file $env:LOCALAPPDATA\Crow\manifests\0731-chat-template.jinja `
  --moe-stream --moe-stream-cache 58s --moe-stream-io-threads 8 --moe-stream-direct `
  --moe-stream-l2 32
```

## Client

```powershell
python $env:LOCALAPPDATA\Crow\cli\crow.py
```

The terminal client defaults to `:8081`. The window reads the port off the running process.

---

## Flags this model needs and Qwen does not

| flag | value | why |
|---|---|---|
| `--moe-stream` | on | routes expert tensors through a slot cache. Qwen has no expert tensors |
| `--moe-stream-cache` | `58s` | 58 slots. Measured: 18.03 tok/s against 11.04 at the earlier value |
| `--moe-stream-io-threads` | `8` | |
| `--moe-stream-direct` | on | |
| `--moe-stream-l2` | `32` | computed by `install.ps1` from detected RAM; the manifest records this machine's value |
| `--chat-template-file` | path | the embedded template fails golden vector 4 |
| `--spec-type` | absent | its speculation path needs a separate draft model and costs 6.06 % |

## Reasoning levels

| rows offered | collapses |
|---|---|
| `low` (default), `max` | `off`, `low`, `high` all render the same prompt |

## Numbers

| | |
|---|---|
| decode, 1,653 tokens of context | 74.09 tok/s |
| decode, 35,984 tokens | 64.50 tok/s |
| expert cache at 58 slots vs the earlier value | 18.03 vs 11.04 tok/s |
| speculation | not used — 6.06 % cost, separate draft model |

## Not measured

| open | |
|---|---|
| this model under `--spec-type` | never run to completion |
| the host RAM tier's effect | flag present, contribution unseparated |
