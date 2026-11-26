"""Shared paths and logging. A --windowed exe has no console, so everything goes to a log file."""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path.home() / ".chess_screen_assistant"
LOG_FILE = APP_DIR / "app.log"
CRASH_FILE = APP_DIR / "crash.log"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

log = logging.getLogger("csa")
_crash_handle = None  # keep the file open for faulthandler


def setup_logging() -> None:
    """Log to ~/.chess_screen_assistant/app.log and capture uncaught errors + native crashes."""
    global _crash_handle
    handlers: list[logging.Handler] = []
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8"))
        _crash_handle = open(CRASH_FILE, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_handle, all_threads=True)
    except OSError:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")

    def _excepthook(t, v, tb):
        log.critical("Uncaught exception", exc_info=(t, v, tb))

    def _thread_hook(args):
        log.critical("Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    log.info("---- start (python %s, frozen=%s) ----", sys.version.split()[0], getattr(sys, "frozen", False))
