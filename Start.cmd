@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 goto folder_error

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
    if not errorlevel 1 goto run_venv
)

py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if not errorlevel 1 goto run_py

python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if not errorlevel 1 goto run_python

echo Prospero Library requires Python 3.12 or newer. See README.md.
set "PROSPERO_EXIT=1"
goto finish

:run_venv
".venv\Scripts\python.exe" scripts\start_local.py
set "PROSPERO_EXIT=%errorlevel%"
goto finish

:run_py
py -3 scripts\start_local.py
set "PROSPERO_EXIT=%errorlevel%"
goto finish

:run_python
python scripts\start_local.py
set "PROSPERO_EXIT=%errorlevel%"

:finish
popd
if not "%PROSPERO_EXIT%"=="0" pause
exit /b %PROSPERO_EXIT%

:folder_error
echo Could not open the Prospero Library folder.
pause
exit /b 1
