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

:: 4. Check / Configure Remote URL
git remote get-url origin >nul 2>nul
if %errorlevel% neq 0 (
    echo [2/5] No GitHub remote repository configured.
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
    echo [2/5] Current Target Remote: !CURRENT_REMOTE!
    set CHANGE_REMOTE=
    set /p CHANGE_REMOTE="Press Enter to keep this remote, or enter a new URL to change: "
    if not "!CHANGE_REMOTE!"=="" (
        git remote set-url origin !CHANGE_REMOTE!
        echo [OK] Updated remote origin to: !CHANGE_REMOTE!
        set CURRENT_REMOTE=!CHANGE_REMOTE!
    )
)
echo.

:: Detect current branch name
set CURRENT_BRANCH=
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set CURRENT_BRANCH=%%b
if "!CURRENT_BRANCH!"=="" set CURRENT_BRANCH=main
if "!CURRENT_BRANCH!"=="HEAD" set CURRENT_BRANCH=main
echo [3/5] Active Branch: !CURRENT_BRANCH!
echo.

:: 5. Stage & Commit
echo [4/5] Staging files for commit...
git add .

git diff --cached --quiet 2>nul
if %errorlevel% neq 0 (
    set COMMIT_MSG=
    set /p COMMIT_MSG="Enter commit message (Press Enter for default: Update Text Surgeon v2.3): "
    if "!COMMIT_MSG!"=="" set COMMIT_MSG=Update Text Surgeon v2.3
    
    echo Committing changes...
    git commit -m "!COMMIT_MSG!"
) else (
    echo [INFO] Working tree is clean. Ready to push existing commits.
)
echo.

:: 6. Push to GitHub
echo [5/5] Pushing changes to GitHub (!CURRENT_BRANCH!)...
git push -u origin !CURRENT_BRANCH!
if %errorlevel% eq 0 (
    echo.
    echo =====================================================================
    echo    [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub.
    echo =====================================================================
    goto :done
)

echo.
echo [NOTICE] Standard push failed. Trying to sync remote branch (git pull --rebase)...
git pull --rebase origin !CURRENT_BRANCH! 2>nul
if %errorlevel% eq 0 (
    echo Sync succeeded. Re-attempting push...
    git push -u origin !CURRENT_BRANCH!
    if %errorlevel% eq 0 (
        echo.
        echo =====================================================================
        echo    [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub.
        echo =====================================================================
        goto :done
    )
)

echo.
echo [NOTICE] Trying fallback: git push -u origin HEAD:!CURRENT_BRANCH! ...
git push -u origin HEAD:!CURRENT_BRANCH!
if %errorlevel% eq 0 (
    echo.
    echo =====================================================================
    echo    [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub.
    echo =====================================================================
    goto :done
)

echo.
echo =====================================================================
echo [ERROR] Push to GitHub failed (Permission 403 or Authentication Error).
echo.
echo How to resolve:
echo 1. Check which GitHub account you are logged in as.
echo    If your GitHub account is different from the repo owner, either:
echo    - Run: gh auth login
echo    - Or change your remote to your own repository in step 2.
echo 2. Check repository permissions on GitHub.
echo =====================================================================

:done
echo.
pause
popd
