"""AgentForge server launcher.

start.bat runs this with python.exe in a visible console window so server
logs are live on screen; it can also run windowless with pythonw.exe.
Logging always goes to server.log in the repo root, and additionally to
the console when one is attached (python.exe).
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

from logging.handlers import RotatingFileHandler

handlers = [RotatingFileHandler(os.path.join(os.path.dirname(HERE), "server.log"),
                                maxBytes=10_485_760, backupCount=5)]
if sys.stderr is not None:
    handlers.append(logging.StreamHandler(sys.stderr))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=handlers,
)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_config=None)
