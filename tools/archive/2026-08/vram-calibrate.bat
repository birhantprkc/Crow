@echo off
REM ============================================================================
REM vram-calibrate - what does llama.cpp actually put in VRAM, and does our
REM prediction from the GGUF tensor table hold?
REM
REM gguf_header.py --budget-gib predicts, counting weights only. This measures.
REM The two are meant to disagree somewhere: the gap is the KV cache, the compute
REM buffers and the CUDA context, and that gap decides Crow's 12 GB operating
REM point. See Crow #33.
REM
REM   NEG  -ncmoe 0   43 layers of experts on the GPU = 137 GiB of weights on a
REM                   31 GiB card. MUST fail. If this loads, VRAM is not the
REM                   binding constraint and every number below means nothing.
REM   POS  -ncmoe 36  predicted 7 expert layers on the GPU, ~30 GiB. MUST load.
REM
REM Exit 0 = both behaved as required. Non-zero = read the VERDICT lines.
REM ============================================================================

setlocal
set "FAIL=0"

set "BIN=C:\Users\robin\dev\crow-lab\bin"
set "MODEL=C:\Users\robin\dev\crow-lab\models\DeepSeek-V4-Flash-MXFP4.gguf"
set "PROMPT=%~dp0prompts\probe-a-chat.txt"

if not exist "%MODEL%" echo SETUP ERROR: model not found: %MODEL% & exit /b 2
if not exist "%PROMPT%" echo SETUP ERROR: prompt not found: %PROMPT% & exit /b 2

REM -ctk/-ctv deliberately left at the f16 default: a quantised KV cache produces
REM mojibake on its own (llama.cpp #26423) and would contaminate this.
REM -np 1 because llama-server defaults to 4 parallel streams with a unified KV
REM cache (advice from fairydreaming on #26423).
set "COMMON=-m "%MODEL%" -f "%PROMPT%" -no-cnv --temp 0 --seed 1234 -n 1 -c 4096 -np 1 --no-warmup -ngl 99"

echo ################ NEG  -ncmoe 0  (must fail)  START %TIME%
"%BIN%\llama-completion.exe" %COMMON% -ncmoe 0
set "NEG=%ERRORLEVEL%"
echo ################ NEG  EXIT=%NEG%  END %TIME%
if "%NEG%"=="0" set "FAIL=1" & echo ################ NEG VERDICT: UNEXPECTED - 137 GiB of experts loaded onto a 31 GiB card. The VRAM model is wrong.
if not "%NEG%"=="0" echo ################ NEG VERDICT: OK - rejected as required
echo.

echo ################ POS  -ncmoe 36  (must load)  START %TIME%
"%BIN%\llama-completion.exe" %COMMON% -ncmoe 36 --verbose-prompt
set "POS=%ERRORLEVEL%"
echo ################ POS  EXIT=%POS%  END %TIME%
if not "%POS%"=="0" set "FAIL=1" & echo ################ POS VERDICT: UNEXPECTED - the predicted-to-fit configuration did not load
if "%POS%"=="0" echo ################ POS VERDICT: OK - loaded as predicted
echo.

if "%FAIL%"=="0" echo RESULT: PASS - the negative probe was rejected and the positive probe loaded
if not "%FAIL%"=="0" echo RESULT: FAIL - see the VERDICT lines above
exit /b %FAIL%
