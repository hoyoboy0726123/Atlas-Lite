@echo off
setlocal
title Atlas-Lite - GUI grounding plugin setup
REM Installs the local GUI-grounding model plugin into its own venv.
REM
REM Why a separate venv: torch + CUDA is about 3 GB and the model weights are
REM another 8 GB. Nobody who does not use this feature should pay for that, so
REM the main backend never imports torch - it talks to a child process instead.
REM
REM NOTE: pure ASCII only. cmd.exe parses .bat with the system ANSI codepage,
REM which cannot represent Chinese; any CJK character breaks that whole line.

echo ==================================================
echo  GUI grounding plugin  (optional)
echo ==================================================
echo.
echo  What this installs:
echo    1. plugins\vlm_grounding\.venv        torch + transformers  ~3 GB
echo    2. plugins\vlm_grounding\models\      Mano-CUA-4B weights   ~8.3 GB
echo.
echo  Requirements: NVIDIA GPU with 6 GB+ VRAM, and about 12 GB free disk.
echo  Without it everything still works - you just lose the "direct locate"
echo  option on click_image steps and fall back to CV template matching.
echo.

set /p GO=Install now? [y/N]
if /i not "%GO%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

REM ---- 1. venv ---------------------------------------
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo.
    echo [1/3] Creating plugin virtual environment...
    python -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo [X] Failed. Install Python 3.11+ and make sure "python" is on PATH.
        pause
        exit /b 1
    )
)

REM ---- 2. torch (CUDA build) -------------------------
REM PyPI's "torch" is the CPU build - installing that leaves
REM torch.cuda.is_available() False and the plugin refuses to start.
REM cu128 covers CUDA 12.8 (up to Blackwell / sm_120). Older cards: use cu121.
echo.
echo [2/3] Installing torch + torchvision (CUDA 12.8 build, several GB)...
"%~dp0.venv\Scripts\python.exe" -m pip install -q --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 (
    echo [X] torch install failed. If your CUDA is older, edit this file and
    echo     change cu128 to cu121, then run again.
    pause
    exit /b 1
)

echo.
echo [3/3] Installing the rest of the plugin dependencies...
"%~dp0.venv\Scripts\python.exe" -m pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [X] Dependency install failed. See the errors above.
    pause
    exit /b 1
)

REM ---- 3. model weights ------------------------------
echo.
if exist "%~dp0models\*.safetensors" goto have_model
dir /s /b "%~dp0models\*.safetensors" >NUL 2>&1
if not errorlevel 1 goto have_model

echo Model weights not found. Downloading Mano-CUA-4B-Thinking-1.1 (~8.3 GB)...
"%~dp0.venv\Scripts\python.exe" -m pip install -q huggingface_hub
"%~dp0.venv\Scripts\python.exe" -c "from huggingface_hub import snapshot_download; snapshot_download('Mininglamp-AI/Mano-CUA-4B-Thinking-1.1', local_dir=r'%~dp0models\Mano-CUA-4B-Thinking-1.1')"
if errorlevel 1 (
    echo [X] Download failed. Check your network, then run this script again.
    echo     Already-downloaded parts are kept, so it resumes rather than restarts.
    pause
    exit /b 1
)

:have_model
echo.
echo Verifying...
"%~dp0.venv\Scripts\python.exe" -c "import torch;print('  CUDA available:', torch.cuda.is_available());print('  GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"

echo.
echo ==================================================
echo  Done. Restart Atlas-Lite, then open a desktop
echo  automation step - "direct locate" is now enabled.
echo ==================================================
echo.
pause
