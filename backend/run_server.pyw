"""Windowless Agent Office server launcher.

Run with pythonw.exe (no console) from start.bat. Logging goes to
server.log in the repo root; stderr/stdout are never touched, so the
server survives console/window closing.
"""
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# Generated project workspaces are runtime data — keep them OUTSIDE the
# published source tree (repo root), otherwise deploy scans package them.
os.environ.setdefault("AGENT_OFFICE_WORKSPACES",
                      os.path.join(os.path.expanduser("~"), ".verdent", "agentforge-projects"))

logging.basicConfig(
    filename=os.path.join(os.path.dirname(HERE), "server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_config=None)
