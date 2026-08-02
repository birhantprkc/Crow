@echo off
REM ============================================================================
REM probe-89 - does ggml-cuda/quantize.cu compile for compute_89 at all?
REM
REM Background: nvfp4_native_scale_error (quantize.cu:14-50) and the
REM __nv_fp4x4_e2m1 uses at :284-287 and :400-423 sit behind CUDART_VERSION
REM >= 12080 with no __CUDA_ARCH__ guard anywhere in the file (0 occurrences).
REM The question was whether that breaks an 89-virtual build. See Crow #33.
REM
REM Four runs, one variable: the --generate-code argument. Everything else is
REM taken verbatim from build-native/CMakeFiles/impl-Release.ninja:1382-1385,
REM the rule that builds this exact translation unit. One deliberate deviation:
REM -DCMAKE_INTDIR=\"Release\" is dropped, it is CMake bookkeeping.
REM
REM   A  positive control - the architecture we intend to build. MUST compile.
REM   B  89-virtual, the actual question.                        MUST compile.
REM   C  compute_89/sm_89, what the release build already does.  MUST compile.
REM   D  negative control - compute_50, dropped in CUDA 13.      MUST FAIL.
REM
REM D exists so that a green result means something. Without a run that has to
REM fail, this script cannot tell "everything compiles" from "nothing was
REM actually checked".
REM
REM Exit code 0 = all four behaved as required. Non-zero = read the EXIT lines.
REM ============================================================================

setlocal
set "FAIL=0"

call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
set "PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin;%PATH%"

set "SRC=C:\Users\robin\dev\crow-lab\src\ggml\src\ggml-cuda\quantize.cu"
set "OUT=%~dp0obj"
if not exist "%SRC%" echo SETUP ERROR: source not found: %SRC% & exit /b 2
if not exist "%OUT%" mkdir "%OUT%"
del /q "%OUT%\*.obj" 2>nul

set "DEFS=-DGGML_BACKEND_BUILD -DGGML_BACKEND_SHARED -DGGML_CUDA_PEER_MAX_BATCH_SIZE=128 -DGGML_CUDA_USE_GRAPHS -DGGML_SCHED_MAX_COPIES=4 -DGGML_SHARED -D_CRT_SECURE_NO_WARNINGS -D_XOPEN_SOURCE=600 -Dggml_cuda_EXPORTS"
set "INCS=-IC:\Users\robin\dev\crow-lab\src\ggml\src\ggml-cuda\.. -IC:\Users\robin\dev\crow-lab\src\ggml\src\..\include -isystem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\include" -isystem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\include\cccl""
set "FLAGS=-D_WINDOWS -Xcompiler=" /EHsc" -Xcompiler="-O2 -Ob2" -DNDEBUG -std=c++17 -Xcompiler=-MD -use_fast_math -extended-lambda -compress-mode=size -Xcompiler /Zc:preprocessor"

echo === toolchain ===
nvcc --version | findstr /C:"release"
cl 2>&1 | findstr /C:"Version"
echo.

echo ### A compute_120a/sm_120a (positive control)  START %TIME%
nvcc -c "%SRC%" -o "%OUT%\quantize_120a.obj" %DEFS% %INCS% %FLAGS% "--generate-code=arch=compute_120a,code=[sm_120a]"
echo ### A EXIT=%ERRORLEVEL%  END %TIME%
if errorlevel 1 set "FAIL=1" & echo ### A UNEXPECTED: positive control failed
echo.

echo ### B compute_89/compute_89 (89-virtual)  START %TIME%
nvcc -c "%SRC%" -o "%OUT%\quantize_89virtual.obj" %DEFS% %INCS% %FLAGS% "--generate-code=arch=compute_89,code=[compute_89]"
echo ### B EXIT=%ERRORLEVEL%  END %TIME%
if errorlevel 1 set "FAIL=1" & echo ### B UNEXPECTED: 89-virtual failed to compile
echo.

echo ### C compute_89/sm_89 (as the release build does)  START %TIME%
nvcc -c "%SRC%" -o "%OUT%\quantize_89sm.obj" %DEFS% %INCS% %FLAGS% "--generate-code=arch=compute_89,code=[sm_89]"
echo ### C EXIT=%ERRORLEVEL%  END %TIME%
if errorlevel 1 set "FAIL=1" & echo ### C UNEXPECTED: sm_89 failed to compile
echo.

echo ### D compute_50 (negative control, must fail)  START %TIME%
nvcc -c "%SRC%" -o "%OUT%\quantize_50.obj" %DEFS% %INCS% %FLAGS% "--generate-code=arch=compute_50,code=[compute_50]" 2>&1 | findstr /C:"nvcc fatal" /C:"Unsupported" /C:"error"
echo ### D END %TIME%
if exist "%OUT%\quantize_50.obj" set "FAIL=1" & echo ### D UNEXPECTED: negative control produced an object - this probe proves nothing
if not exist "%OUT%\quantize_50.obj" echo ### D OK: rejected as required
echo.

echo === device code actually emitted (not inferred from the configuration) ===
for %%F in (quantize_120a.obj quantize_89virtual.obj quantize_89sm.obj) do (
  echo -- %%F
  cuobjdump --list-elf "%OUT%\%%F" 2>nul | findstr /C:"ELF file"
  cuobjdump --list-ptx "%OUT%\%%F" 2>nul | findstr /C:"PTX file"
)
echo.

if "%FAIL%"=="0" echo RESULT: PASS - three required compiles succeeded, negative control rejected
if not "%FAIL%"=="0" echo RESULT: FAIL - see the UNEXPECTED lines above
exit /b %FAIL%
