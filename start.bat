@echo off
setlocal
cd /d "%~dp0"
rem Prefer the normal python command. The installed Python Launcher can point
rem to a removed interpreter, so validate the actual interpreter before use.
set PYTHON_CMD=python
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 set PYTHON_CMD=py -3.11
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto no_python
if not exist .venv\Scripts\python.exe %PYTHON_CMD% -m venv .venv
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error
if not exist data mkdir data
echo Opening http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto error
goto end
:no_python
echo Python 3.11 or newer could not be found.
echo Install Python 3.11+ and make the python command available on PATH.
pause
exit /b 1
:error
echo Startup failed. Check the message above.
pause
:end
endlocal
