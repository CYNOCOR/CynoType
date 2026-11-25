#!/bin/bash
# Double-click this file in Finder to start Auto Typer
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  ./setup_mac.sh || exit 1
fi
exec .venv/bin/python auto_typer.py
