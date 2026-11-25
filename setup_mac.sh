#!/bin/bash
# ============================================================
#  CynoType — macOS developer setup (run one time)
#  (End users can simply open dist/CynoType-Installer.dmg)
# ============================================================
set -e
cd "$(dirname "$0")"

echo "==> CynoType setup for macOS"

find_python() {
  for p in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           $(command -v python3 2>/dev/null); do
    if [ -x "$p" ] && [ "$p" != "/usr/bin/python3" ]; then
      if "$p" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
        echo "$p"; return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python || true)"

if [ -z "$PY" ]; then
  echo "==> No Python 3.9+ found."
  if ! command -v brew >/dev/null 2>&1; then
    echo "==> Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
  fi
  echo "==> Installing Python with Homebrew..."
  brew install python
  PY="$(find_python || true)"
fi

if [ -z "$PY" ]; then
  echo "ERROR: Could not find Python 3.9+. Install from https://www.python.org/downloads/macos/"
  exit 1
fi

echo "==> Using Python: $PY ($("$PY" --version))"
echo "==> Creating virtual environment (.venv)..."
rm -rf .venv
"$PY" -m venv .venv

echo "==> Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

echo "==> Verifying macOS frameworks..."
.venv/bin/python -c "import AppKit, Quartz, pynput, pyperclip; print('All macOS frameworks OK.')"

chmod +x run_mac.command build_mac.sh 2>/dev/null || true

echo ""
echo "============================================================"
echo " Setup complete!"
echo " Run app:         ./run_mac.command"
echo " Build installer: ./build_mac.sh  -> dist/CynoType-Installer.dmg"
echo "============================================================"
