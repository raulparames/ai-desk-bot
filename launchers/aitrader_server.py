"""Starts the local AI-Trader server without a console window (for Windows Task Scheduler).

pythonw.exe has no stdout/stderr, and the server's web framework needs them for its logs,
so they go to AI-Trader/service/server/logs/console.log instead.
Run with AI-Trader's own interpreter:  AI-Trader\\.venv\\Scripts\\pythonw.exe <this file>
"""

import os
import runpy
import sys
from pathlib import Path

AI_TRADER = Path(__file__).resolve().parents[2] / "AI-Trader"
SERVER = AI_TRADER / "service" / "server"

(SERVER / "logs").mkdir(parents=True, exist_ok=True)
log = open(SERVER / "logs" / "console.log", "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log

os.chdir(AI_TRADER)  # DB_PATH in .env is relative to the repo root
sys.path.insert(0, str(SERVER))
runpy.run_path(str(SERVER / "main.py"), run_name="__main__")
