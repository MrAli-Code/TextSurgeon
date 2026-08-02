@echo off
title Text Surgeon v2.3 - AI Agent and Precision Splice Editor
pushd "%~dp0"

echo =====================================================================
echo    Text Surgeon v2.3 (Local Web UI and Agent Engine)
echo =====================================================================
echo.

if not exist ".env" (
    if exist ".env.example" (
        echo [INFO] First-time setup detected: Creating default .env configuration...
        copy ".env.example" ".env" >nul
        echo [OK] Created .env file.
        echo.
    )
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 surgeon_web.py %*
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python surgeon_web.py %*
    goto :done
)

echo.
echo [ERROR] Python 3 was not found on this computer.
echo Please install Python 3.8+ from https://www.python.org/downloads/
echo Make sure to check "Add python.exe to PATH" during installation.
echo.
pause
popd
exit /b 1

:done
if errorlevel 1 pause
popd
