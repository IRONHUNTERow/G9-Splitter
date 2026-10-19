@echo off
setlocal
title G9 Splitter 2
cd /d "%~dp0"
set "APP=%~dp0g9_splitter.pyw"
set "PY="

rem --- Find Python 3.12 / 3.13 / 3.11 - the window add-on works best with these ---
for %%V in (312 313 311) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
)
for %%V in (3.12 3.13 3.11) do (
  if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-%%V-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-%%V-64\python.exe"
)

rem --- Not found: install Python 3.12 once - free, from python.org via winget ---
if not defined PY (
  echo.
  echo  G9 Splitter 2 needs Python 3.12 - installing it now, this only happens once.
  echo.
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo  Could not find or install Python.
  echo  Install Python 3.12 from https://www.python.org/downloads/ and double-click this file again.
  pause
  exit /b 1
)
for %%F in ("%PY%") do set "PYDIR=%%~dpF"

rem --- The add-on that gives G9 Splitter 2 its own app window - pywebview ---
"%PY%" -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Installing the window add-on for the new look - pywebview - one time only...
  echo.
  "%PY%" -m pip install --user --disable-pip-version-check --quiet pywebview
)

set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%APP%"
exit /b 0
