@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0..\Chosen\.venv\Scripts\pythonw.exe"
if not exist "%PYTHON%" (
    echo Missing project Python environment: %PYTHON%
    pause
    exit /b 1
)
start "" "%PYTHON%" "%~dp0publish_software_release.py"
exit /b 0
