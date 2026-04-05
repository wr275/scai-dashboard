#!/bin/bash
echo "============================================================"
echo "  SCAI Executive Dashboard — Starting Server"
echo "============================================================"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Install Python 3.10+"
    exit 1
fi

# Install dependencies
echo "Checking dependencies..."
pip3 install -r requirements.txt -q

echo ""
echo "Starting server at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

# Open browser (macOS + Linux)
sleep 2 && (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null) &

python3 server.py
