#!/bin/sh
cd "$(dirname "$0")"
(sleep 1; open http://localhost:8000/host 2>/dev/null || xdg-open http://localhost:8000/host 2>/dev/null) &
python3 quiz_server.py
