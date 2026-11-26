"""Stockfish discovery, launching (with fallbacks) and a self-test.

Why this exists: a Stockfish build that needs AVX2 dies instantly ("illegal instruction") on older
CPUs, and PyInstaller one-file apps unpack the engine into a temp folder that antivirus tools love
to delete. So we (a) ship a fast AND a compatible build, (b) copy the engine to a stable folder,
(c) really run a tiny search before trusting an engine, and (d) fall back to the next one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import chess
import chess.engine

from common import APP_DIR, CREATE_NO_WINDOW, log

ENGINE_CACHE = APP_DIR / "engines"


class EngineStartError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- discovery
def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        dirs.append(Path(mp))
    dirs.append(Path(__file__).resolve().parent)
    dirs.append(Path(sys.argv[0]).resolve().parent)
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    return dirs


def _rank(p: Path) -> int:
    n = p.stem.lower()
    return 0 if "fast" in n else 1 if "compat" in n else 2


def bundled_engines() -> list[Path]:
    seen: set[str] = set()
    found: list[Path] = []
    for base in _search_dirs():
        for sub in ("engine", "."):
            d = base / sub
            if not d.is_dir():
                continue
            for p in d.glob("stockfish*"):
                if not p.is_file() or p.suffix.lower() not in (".exe", ""):
                    continue
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(p)
    return sorted(found, key=lambda p: (_rank(p), p.name))


def candidate_engines(user_engine: str | None = None) -> list[str]:
    """User-chosen engine first, then bundled (fast -> compatible), then anything on PATH."""
    out: list[str] = []
    if user_engine and os.path.exists(user_engine):
        out.append(user_engine)
    out += [str(p) for p in bundled_engines()]
    on_path = shutil.which("stockfish")
    if on_path:
        out.append(on_path)
    return list(dict.fromkeys(out))


def _launchable(path: str) -> str:
    """One-file builds unpack to a temp dir; run a copy from a stable folder instead."""
    mp = getattr(sys, "_MEIPASS", None)
    if not mp or not Path(mp).name.startswith("_MEI"):
        return path
    try:
        src = Path(path)
        dst = ENGINE_CACHE / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            ENGINE_CACHE.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            log.info("Copied engine to %s", dst)
        return str(dst)
    except OSError:
        log.exception("Could not copy engine to a stable folder; using it in place")
        return path


# ---------------------------------------------------------------------------- launching
def close_engine(engine: chess.engine.SimpleEngine) -> None:
    for fn in (engine.quit, engine.close):
        try:
            fn()
            return
        except Exception:  # noqa: BLE001
            continue


def _setopt(engine: chess.engine.SimpleEngine, name: str, value) -> None:
    if name in engine.options:
        try:
            engine.configure({name: value})
        except Exception:  # noqa: BLE001
            log.warning("Could not set engine option %s=%r", name, value)


def open_engine(candidates: list[str], skip: set[str] | None = None
                ) -> tuple[chess.engine.SimpleEngine, str]:
    """Start the first candidate that survives a real (tiny) search."""
    skip = skip or set()
    errors: list[str] = []
    kwargs = {"creationflags": CREATE_NO_WINDOW} if CREATE_NO_WINDOW else {}
    for path in candidates:
        if path in skip:
            continue
        engine = None
        try:
            exe = _launchable(path)
            engine = chess.engine.SimpleEngine.popen_uci(exe, timeout=20, stderr=subprocess.DEVNULL, **kwargs)
            _setopt(engine, "Threads", max(1, min(4, (os.cpu_count() or 2) - 1)))
            _setopt(engine, "Hash", 128)
            # Exercise the evaluation code: an unsupported-CPU build typically only crashes here.
            engine.analyse(chess.Board(), chess.engine.Limit(depth=4))
            log.info("Engine OK: %s (%s)", Path(path).name, engine.id.get("name", "?"))
            return engine, path
        except Exception as e:  # noqa: BLE001
            log.warning("Engine failed: %s -> %r", path, e)
            errors.append(f"{Path(path).name}: {e!r}")
            if engine is not None:
                close_engine(engine)
    raise EngineStartError("No working Stockfish found.\n" + "\n".join(errors) if errors
                           else "No Stockfish executable found.")


def set_skill(engine: chess.engine.SimpleEngine, skill: int) -> None:
    _setopt(engine, "Skill Level", int(skill))


# ---------------------------------------------------------------------------- self-test
def run_selftest() -> int:
    """`app.exe --selftest`: try every engine we can find. Exit 0 = all fine, 1 = some failed, 2 = none work."""
    from common import setup_logging
    setup_logging()
    lines: list[str] = []
    cands = candidate_engines(None)
    ok = 0
    if not cands:
        lines.append("FAIL: no Stockfish executable found")
    for path in cands:
        try:
            engine, _ = open_engine([path])
            try:
                info = engine.analyse(chess.Board(), chess.engine.Limit(depth=8))
                lines.append(f"OK   {Path(path).name}: {engine.id.get('name')} -> {info['pv'][0].uci()}")
                ok += 1
            finally:
                close_engine(engine)
        except Exception as e:  # noqa: BLE001
            lines.append(f"FAIL {Path(path).name}: {e}")
    text = "\n".join(lines)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "selftest.txt").write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    log.info("selftest:\n%s", text)
    return 0 if cands and ok == len(cands) else (1 if ok else 2)
