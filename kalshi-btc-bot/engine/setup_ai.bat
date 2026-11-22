@echo off
REM Optional: installs the local AI forecaster (Amazon Chronos-Bolt) to run on your NVIDIA graphics card. About 3 GB download.
cd /d "%~dp0"
if not exist .venv (echo Run setup.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
echo Installing PyTorch with CUDA 12.8 (needed for RTX 50-series cards)...
python -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu128 || (echo PyTorch install failed. & pause & exit /b 1)
python -m pip install "chronos-forecasting>=1.5" || (echo Chronos install failed. & pause & exit /b 1)
echo Downloading the model and testing the graphics card...
python -c "import torch;print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none - will use CPU');from chronos import BaseChronosPipeline as B;B.from_pretrained('amazon/chronos-bolt-small');print('Model ready.')"
echo.
echo Local AI installed. Close and reopen run_engine.bat to start using it.
pause
