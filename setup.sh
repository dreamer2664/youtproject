#!/usr/bin/env bash
# ============================================================================
#  Linux / macOS setup. Run from the project folder:
#      bash setup.sh
#
#  No Google Cloud project, no OAuth, no audit — this project uploads
#  nothing by itself. You upload the finished files manually.
# ============================================================================
set -e
cd "$(dirname "$0")"

echo ""
echo "=== youtproject setup ==="
echo ""

# --- Python ---------------------------------------------------------------
if ! command -v python3 >/dev/null; then
    echo "[!!] python3 not found. Install Python 3.11+ first."
    exit 1
fi
echo "[ok] Python: $(python3 --version)"

# --- FFmpeg ---------------------------------------------------------------
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
    echo "[ok] ffmpeg: $(ffmpeg -version | head -1)"
else
    echo "[!!] ffmpeg/ffprobe missing."
    echo "     Debian/Ubuntu: sudo apt install ffmpeg"
    echo "     macOS:         brew install ffmpeg"
    exit 1
fi

# --- Virtual environment --------------------------------------------------
if [ ! -d "venv" ]; then
    echo "[..] creating virtual environment"
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
echo "[ok] venv activated: $(python --version)"

# --- Dependencies ---------------------------------------------------------
echo "[..] installing dependencies"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
echo "[ok] dependencies installed"

# --- Config ---------------------------------------------------------------
if [ ! -f "config.yaml" ]; then
    cp config.example.yaml config.yaml
    echo "[ok] created config.yaml - open it and set channel.topic + ai.gemini_api_key"
else
    echo "[ok] config.yaml already exists"
fi

# --- Commit guard ---------------------------------------------------------
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "[ok] installed secret-blocking pre-commit hook"

echo ""
echo "=== Next steps ==="
echo "  source venv/bin/activate     # every new terminal"
echo "  nano config.yaml             # set your topic and Gemini key"
echo "  python main.py preflight     # check everything"
echo "  python main.py generate      # make a video"
echo "  python main.py package       # build the upload kit"
echo ""
