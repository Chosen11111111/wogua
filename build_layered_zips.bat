@echo off
setlocal
set "PYTHON=E:\ChosenSkin2.0\Chosen\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo 找不到 Chosen venv Python: %PYTHON%
  pause
  exit /b 1
)
if "%~1"=="" (
  echo 用法: 把 ChosenreleaseNew\Chosen2.17 文件夹拖到本 bat 上
  echo 或: build_layered_zips.bat E:\ChosenSkin2.0\ChosenreleaseNew\Chosen2.17
  pause
  exit /b 1
)
"%PYTHON%" "%~dp0build_layered_zips.py" "%~1"
echo.
pause
