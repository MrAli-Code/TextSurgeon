@echo off
setlocal enabledelayedexpansion
title Text Surgeon - GitHub One-Click Deployment
pushd "%~dp0"

echo =====================================================================
echo    Text Surgeon v2.3 - One-Click GitHub Deployment Helper
echo =====================================================================
echo.

:: 1. Verify Git Installation
where git >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Git is not installed or not in your system PATH!
    echo Please install Git from https://git-scm.com and try again.
    echo.
    pause
    popd
    exit /b 1
)

:: 2. Initialize Git Repo if needed
if not exist ".git" (
    echo [1/5] Initializing local Git repository...
    git init
    git branch -M main
    echo [OK] Git repository initialized.
) else (
    echo [1/5] Local Git repository detected.
)
echo.

:: 3. Verify .gitignore protection
if not exist ".gitignore" (
    echo [WARNING] .gitignore not found! Creating default protection file...
    (
        echo .env
        echo .env.*
        echo !.env.example
        echo .surgeon_memory.json
        echo .surgeon_session.json
        echo .surgeon_web_state.json
        echo .surgeon/
        echo projects/
        echo git/
        echo *.log
        echo *.tmp
        echo __pycache__/
        echo *.py[cod]
    ) > .gitignore
    echo [OK] Protection .gitignore created.
)

echo [2/5] Verification: Checking git status (sensitive files excluded)...
git status --short
echo.

:: 4. Check / Configure Remote URL
git remote get-url origin >nul 2>nul
if %errorlevel% neq 0 (
    echo [3/5] No GitHub remote repository configured.
    set /p REPO_URL="Enter your GitHub Repository URL (e.g. https://github.com/username/text-surgeon.git): "
    if "!REPO_URL!"=="" (
        echo [ERROR] Repository URL cannot be empty. Aborting deployment.
        pause
        popd
        exit /b 1
    )
    git remote add origin !REPO_URL!
    echo [OK] Remote origin added: !REPO_URL!
) else (
    for /f "delims=" %%r in ('git remote get-url origin') do set CURRENT_REMOTE=%%r
    echo [3/5] Target Remote: !CURRENT_REMOTE!
)
echo.

:: 5. Prompt for Commit Message
echo [4/5] Staging files for commit...
git add .

set COMMIT_MSG=
set /p COMMIT_MSG="Enter commit message (Press Enter for default: Update Text Surgeon v2.3): "
if "!COMMIT_MSG!"=="" set COMMIT_MSG=Update Text Surgeon v2.3

echo Committing changes...
git commit -m "!COMMIT_MSG!"

:: 6. Push to GitHub
echo.
echo [5/5] Pushing changes to GitHub main branch...
git push -u origin main
if %errorlevel% eq 0 (
    echo.
    echo =====================================================================
    echo    [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub.
    echo =====================================================================
) else (
    echo.
    echo [NOTICE] Standard push failed. Trying fallback git push origin main...
    git push origin main
    if %errorlevel% eq 0 (
        echo.
        echo =====================================================================
        echo    [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub.
        echo =====================================================================
    ) else (
        echo.
        echo [ERROR] Failed to push to GitHub.
        echo Please check your repository URL and GitHub credentials.
    )
)

echo.
pause
popd
