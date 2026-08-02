#!/usr/bin/env bash
# =====================================================================
# Text Surgeon v2.3 - One-Click GitHub Deployment Script (Linux/macOS)
# =====================================================================

set -e

echo "====================================================================="
echo "   ✂️ Text Surgeon v2.3 - One-Click GitHub Deployment Helper"
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

echo "[2/5] Verification: Git status (sensitive data excluded):"
git status --short
echo ""

# 4. Remote Repo Configuration
if ! git remote get-url origin &> /dev/null; then
    echo "[3/5] No GitHub remote repository configured."
    read -rp "Enter your GitHub Repository URL (e.g. https://github.com/user/text-surgeon.git): " REPO_URL
    if [ -z "$REPO_URL" ]; then
        echo "[ERROR] Repository URL required."
        exit 1
    fi
    git remote add origin "$REPO_URL"
    echo "[OK] Added remote 'origin': $REPO_URL"
else
    CURRENT_REMOTE=$(git remote get-url origin)
    echo "[3/5] Target Remote: $CURRENT_REMOTE"
fi
echo ""

# 5. Commit
echo "[4/5] Staging files..."
git add .

read -rp "Enter commit message (Press Enter for default: 'Update Text Surgeon v2.3'): " COMMIT_MSG
if [ -z "$COMMIT_MSG" ]; then
    COMMIT_MSG="Update Text Surgeon v2.3"
fi

git commit -m "$COMMIT_MSG" || true

# 6. Push
echo ""
echo "[5/5] Pushing changes to GitHub main branch..."
git push -u origin main

echo ""
echo "====================================================================="
echo "   ✅ DEPLOYMENT SUCCESSFUL! Your changes are live on GitHub."
echo "====================================================================="
