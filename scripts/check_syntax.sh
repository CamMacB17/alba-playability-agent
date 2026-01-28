#!/bin/bash
# Pre-commit syntax check for main.py
# Exits with code 1 if syntax errors are found

python -m py_compile main.py

if [ $? -eq 0 ]; then
    echo "✓ Syntax check passed"
    exit 0
else
    echo "✗ Syntax check failed"
    exit 1
fi
