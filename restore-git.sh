#!/bin/bash
# EMERGENCY GIT RESTORE
# Run this if .git was accidentally deleted

if [ ! -d ".git.backup" ]; then
    echo "❌ No .git.backup found!"
    exit 1
fi

if [ -d ".git" ]; then
    echo "⚠️  .git already exists. Backing up current .git to .git.current..."
    mv .git .git.current
fi

echo "Restoring .git from backup..."
cp -r .git.backup .git

echo "✓ .git restored from backup"
echo ""
echo "Current status:"
git status
