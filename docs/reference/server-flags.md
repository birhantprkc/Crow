## Server flags

| flag | value | why |
|---|---|---|
| `-c` | `200000` | a coding agent holds files and history. Rollover cuts at 0.9 of the window |
| `-np` | `1` | one user. `-np 4` splits the context four ways |
| `-ctk` / `-ctv` | `q8_0` | f16 left 332.8 MiB of headroom; q8_0 leaves 6,627 |
| `-ngl` | `99` | 16.35 GiB fits on the card whole |
| `--jinja` | on | without it llama.cpp uses its built-in template and the reasoning replay is dropped |
| `--slot-save-path` | `<install>\session` | the server refuses to start against a path that does not exist |
| `--mmproj` | `models\qwen38-gguf\mmproj-F16.gguf` | the vision projector (#142). Without it the same GGUF is a text model and `/props` says `vision: false`. Costs 1,124 MiB VRAM, leaves text prefill unchanged. One image is `(w/32)*(h/32)` tokens after resize, capped at 4,096 (`--image-max-tokens` moves the cap) |
| `--spec-type` | `draft-mtp` | the model's own MTP head. 1.85x decode, measured |
| `--spec-draft-n-max` | `3` (default) | measured, see below |
