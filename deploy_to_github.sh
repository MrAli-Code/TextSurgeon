#!/usr/bin/env bash
# =====================================================================
# Text Surgeon v2.3 - One-Click GitHub Deployment Script (Linux/macOS)
# =====================================================================

set -e

echo "====================================================================="
echo "   Text Surgeon v2.3 - One-Click GitHub Deployment Helper"
echo "====================================================================="
echo ""

# 1. Verify Git Installation
if ! command -v git &> /dev/null; then
    echo "[ERROR] Git is not installed. Please install git and try again."
    exit 1
fi

# 2. Initialize Repo if needed
if [ ! -d ".git" ]; then
    echo "[1/5] Initializing local Git repository..."
    git init
    git branch -M main
    echo "[OK] Local repository initialized."
else
    echo "[1/5] Local Git repository detected."
fi

# 3. Verify .gitignore protection
if [ ! -f ".gitignore" ]; then
    echo "[WARNING] Creating default .gitignore protection file..."
    cat << 'EOF' > .gitignore
.env
.env.*
!.env.example
.surgeon_memory.json
.surgeon_session.json
.surgeon_web_state.json
.surgeon/
projects/
git/
*.log
*.tmp
__pycache__/
*.py[cod]
EOF
    echo "[OK] .gitignore created."
fi

# 4. Remote Repo Configuration
if ! git remote get-url origin &> /dev/null; then
    echo "[2/5] No GitHub remote repository configured."
    read -rp "Enter your GitHub Repository URL (e.g. https://github.com/user/text-surgeon.git): " REPO_URL
    if [ -z "$REPO_URL" ]; then
        echo "[ERROR] Repository URL required."
        exit 1
    fi
    git remote add origin "$REPO_URL"
    echo "[OK] Added remote 'origin': $REPO_URL"
else
    CURRENT_REMOTE=$(git remote get-url origin)
    echo "[2/5] Current Target Remote: $CURRENT_REMOTE"
    read -rp "Press Enter to keep this remote, or enter a new URL to change: " CHANGE_REMOTE
    if [ -n "$CHANGE_REMOTE" ]; then
        git remote set-url origin "$CHANGE_REMOTE"
        echo "[OK] Updated remote origin to: $CHANGE_REMOTE"
        CURRENT_REMOTE="$CHANGE_REMOTE"
    fi
fi
echo ""

# Detect current branch name
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
if [ "$CURRENT_BRANCH" = "HEAD" ] || [ -z "$CURRENT_BRANCH" ]; then
    CURRENT_BRANCH="main"
fi
echo "[3/5] Active Branch: $CURRENT_BRANCH"
echo ""

# 5. Stage & Commit
echo "[4/5] Staging files for commit..."
git add .

if ! git diff --cached --quiet 2>/dev/null; then
    read -rp "Enter commit message (Press Enter for default: 'Update Text Surgeon v2.3'): " COMMIT_MSG
    if [ -z "$COMMIT_MSG" ]; then
        COMMIT_MSG="Update Text Surgeon v2.3"
    fi
    echo "Committing changes..."
    git commit -m "$COMMIT_MSG"
else
    echo "[INFO] Working tree is clean. Ready to push existing commits."
fi
echo ""

# 6. Push
echo "[5/5] Pushing changes to GitHub ($CURRENT_BRANCH)..."
if git push -u origin "$CURRENT_BRANCH"; then
    echo ""
    echo "====================================================================="
    echo "   [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub."
    echo "====================================================================="
else
    echo ""
    echo "[NOTICE] Standard push failed. Trying to sync remote branch (git pull --rebase)..."
    if git pull --rebase origin "$CURRENT_BRANCH"; then
        echo "Sync succeeded. Re-attempting push..."
        git push -u origin "$CURRENT_BRANCH"
        echo ""
        echo "====================================================================="
        echo "   [SUCCESS] DEPLOYMENT COMPLETED! Your changes are live on GitHub."
        echo "====================================================================="
    else
        echo ""
        echo "====================================================================="
        echo "   [ERROR] Push to GitHub failed (Permission 403 or Authentication Error)."
        echo "   Please verify your logged-in GitHub account or repository permissions."
        echo "====================================================================="
        exit 1
    fi
fi
