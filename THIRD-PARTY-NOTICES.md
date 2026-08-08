# Third-party notices

The MIT grant in `LICENSE` covers this repository's own work. Four components carry terms this
project cannot grant, and they are listed here because a licence that quietly covers somebody
else's code is worth nothing.

This file is separate from `LICENSE` on purpose: GitHub's licence detection reads `LICENSE` and
recognises MIT only from its unmodified text. With these notices appended to it, the repository
reported **no licence at all** through the API — measured 2026-08-08, before and after the split.

## llama.cpp — MIT, different copyright holder

`patches/` contains diffs against `ggml-org/llama.cpp`. Those files carry context lines from that
project, and applying them produces a modified copy of it.

Its licence and copyright notice travel with them and are kept **byte-identical** beside them in
`patches/LICENSE.llama.cpp` — "Copyright (c) 2023-2026 The ggml authors". A licence text edited in
transit is worse than none, so it is copied rather than retyped.

The terms are the same as ours. The copyright holders are not, which is why they are kept apart.

## Google Sans Code — SIL Open Font License 1.1, not MIT

`cli/fonts/` ships two `.ttf` files under the OFL. Its full text sits beside them in
`cli/fonts/OFL.txt` and must stay there: under the OFL a font may be bundled and redistributed
**only** with that notice.

`cli/test_crow.py` asserts the file is present, because without it redistribution is a licence
violation that nothing else in the build would catch.

## DeepSeek-V4-Flash — fetched, not shipped

`deepseek-ai/DeepSeek-V4-Flash` is MIT licensed; the GGUF files state it themselves in
`general.license`. The quantisation actually used, `unsloth/DeepSeek-V4-Flash-GGUF` `UD-IQ3_XXS`,
is unsloth's own artefact and carries whatever terms that repository states.

Neither is redistributed by this project. The README points at the source and the user fetches it
with `hf download`.

## NVIDIA CUDA

The CUDA backend is built against NVIDIA's CUDA Toolkit — 13.3 on the development machine.

The build tree carries no CUDA runtime libraries of its own: `bin/Release` holds eleven files, all
of them `ggml-*`, `llama-*` or `mtmd`; `cudart` and `cuBLAS` are not among them. Wherever a release
does ship NVIDIA redistributable libraries alongside them, those files stay under NVIDIA's own
licence terms and are **not** covered by the MIT grant.

**Unmeasured:** whether the binaries need such a library present at all. `dumpbin /dependents` on
`ggml-cuda.dll` settles it, and it is point 1 of issue #57.
