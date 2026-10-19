@echo off
setlocal
cd /d "%~dp0"
set "APP=%~dp0g9_splitter.pyw"

rem --- Try the usual ways Python is installed ---
where pyw >nul 2>nul && ( start "" pyw -3 "%APP%" & exit /b )
where pythonw >nul 2>nul && ( start "" pythonw "%APP%" & exit /b )
for %%V in (314 313 312 311 310) do (
  if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" "%APP%" & exit /b
  )
)

rem --- Python not found: install it once (free, from python.org via winget) ---
echo.
echo  G9 Splitter needs Python (free). Installing it now - this only happens once.
echo.
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%APP%"
  exit /b
)
echo.
echo  Python was installed, but Windows needs a moment to notice it.
echo  Close this window and double-click "Start G9 Splitter" again.
echo  (If it still fails, install Python from https://www.python.org/downloads/ )
pause
