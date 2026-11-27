"""Background thread: watch the board, recognise it, ask Stockfish, post results to the UI queue."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import chess
import chess.engine
import mss
import numpy as np

from chessvision import BoardRecognizer
from common import log
from engines import EngineStartError, candidate_engines, close_engine, kill_engine, open_engine, set_skill
from logic import build_board, engine_safe, format_score, white_win_percent

POLL_SECONDS = 0.25
MAX_CONSECUTIVE_ERRORS = 10
MAX_ENGINE_RESTARTS = 8
WATCHDOG_EXTRA = 8.0   # seconds beyond the think time before a silent engine is declared hung


class Worker(threading.Thread):
    def __init__(self, user_engine: str | None, region: dict, recognizer: BoardRecognizer,
                 settings: dict, out: "queue.Queue"):
        super().__init__(name="board-worker", daemon=True)
        self.user_engine, self.region, self.rec = user_engine, region, recognizer
        self.s, self.q = settings, out
        # NOTE: never call this `_stop` - threading.Thread has an internal method with that name,
        # and shadowing it makes is_alive()/join() raise "'Event' object is not callable".
        self._halt = threading.Event()
        self._sct: mss.base.MSSBase | None = None
        self._reset_state()

    def stop(self) -> None:
        self._halt.set()

    def _reset_state(self) -> None:
        self.candidate: dict | None = None
        self.stable = 0
        self.accepted: tuple | None = None
        self.prev_board: chess.Board | None = None
        self.last_skill: int | None = None

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        try:
            self._supervise()
        except Exception as e:  # noqa: BLE001
            log.exception("Worker crashed")
            self.q.put(("error", f"The background worker crashed:\n{e!r}\n\nDetails are in the log file."))
        finally:
            self._close_capture()

    def _supervise(self) -> None:
        """Keep an engine alive: restart on crash, move to the next build if one keeps crashing."""
        candidates = candidate_engines(self.user_engine)
        skip: set[str] = set()
        crashes: dict[str, int] = {}
        restarts = 0
        while not self._halt.is_set():
            try:
                engine, path = open_engine(candidates, skip)
            except EngineStartError as e:
                self.q.put(("error", f"Could not start Stockfish:\n{e}"))
                return
            self.q.put(("engine", Path(path).name))
            started = time.monotonic()
            try:
                self._loop(engine)
                return
            except chess.engine.EngineError as e:  # includes EngineTerminatedError
                log.warning("Engine %s died: %r", path, e)
                crashes[path] = crashes.get(path, 0) + 1
                if crashes[path] >= 2:
                    skip.add(path)
                if time.monotonic() - started > 120:   # it ran fine for a while: not a crash loop
                    restarts = 0
                restarts += 1
                self._reset_state()
                self._halt.wait(min(2.0, 0.3 * restarts))   # brief back-off so we never spin
                if restarts > MAX_ENGINE_RESTARTS:
                    self.q.put(("error", "Stockfish keeps crashing.\nTry 'Choose Stockfish .exe...' with a "
                                         "different build, and check your antivirus."))
                    return
                self.q.put(("status", "Engine stopped unexpectedly - restarting...", self.s["flipped"]))
                self._reset_state()
            finally:
                close_engine(engine)

    # ------------------------------------------------------------------ capture
    def _grab(self) -> np.ndarray:
        if self._sct is None:
            self._sct = mss.mss()
        return np.ascontiguousarray(np.array(self._sct.grab(self.region))[:, :, :3])

    def _close_capture(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None

    # ------------------------------------------------------------------ main loop
    def _loop(self, engine: chess.engine.SimpleEngine) -> None:
        errors = 0
        while not self._halt.wait(POLL_SECONDS):
            try:
                self._tick(engine)
                errors = 0
            except chess.engine.EngineError:
                raise
            except Exception:  # noqa: BLE001  (screen hiccups, bad frames, ...)
                errors += 1
                log.exception("Tick failed (%d/%d)", errors, MAX_CONSECUTIVE_ERRORS)
                self._close_capture()  # re-created on the next grab (monitor change, sleep/wake, ...)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError("Reading the screen keeps failing - see the log file.") from None
                self.q.put(("status", "Hiccup while reading the screen - retrying...", self.s["flipped"]))

    def _tick(self, engine: chess.engine.SimpleEngine) -> None:
        flipped = bool(self.s["flipped"])
        frame = self._grab()
        pieces, unknown = self.rec.recognize(frame, flipped)

        if pieces == self.candidate:
            self.stable += 1
        else:
            self.candidate, self.stable = pieces, 1
        sig = (flipped, bool(self.s["both"]), int(self.s["lines"]), int(self.s["skill"]), float(self.s["time"]))
        if self.stable < 2 or (pieces, sig) == self.accepted:
            return
        self.accepted = (pieces, sig)

        # One or two squares we could not read (arrow overlay, animation, ...)? If we know the previous
        # position, assume those squares did not change and let build_board() reconcile the rest.
        if unknown and self.prev_board is not None and unknown <= 2:
            for f, r in self.rec.last_unknown:
                prev_piece = self.prev_board.piece_at(chess.square(f, r))
                if prev_piece is not None:
                    pieces[(f, r)] = prev_piece.symbol()
            unknown = 0
        values = list(pieces.values())
        if unknown or values.count("K") != 1 or values.count("k") != 1:
            self.q.put(("status", "Can't read the board (is it fully visible and not covered?)", flipped))
            return

        my_color = chess.BLACK if flipped else chess.WHITE
        board = build_board(pieces, self.prev_board, my_color)
        if not engine_safe(board):
            self.prev_board = None
            self.q.put(("status", "Position looks illegal - probably misread. Recalibrate if it persists.", flipped))
            return
        self.prev_board = board
        self.q.put(("fen", board.fen()))

        if board.is_game_over():
            self.q.put(("status", "Game over.", flipped))
            return
        if board.turn != my_color and not self.s["both"]:
            self.q.put(("status", "Waiting for the opponent...", flipped))
            return

        skill = int(self.s["skill"])
        if skill != self.last_skill:
            set_skill(engine, skill)
            self.last_skill = skill

        self.q.put(("status", "Thinking...", flipped))
        n = max(1, min(int(self.s["lines"]), board.legal_moves.count()))
        think = float(self.s["time"])
        watchdog = threading.Timer(think + WATCHDOG_EXTRA, kill_engine, args=(engine,))
        watchdog.daemon = True
        watchdog.start()
        try:
            result = engine.analyse(board, chess.engine.Limit(time=think), multipv=n)
        finally:
            watchdog.cancel()
        infos = result if isinstance(result, list) else [result]

        moves = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv or "score" not in info:
                continue
            try:
                line = board.variation_san(pv[:6])
            except Exception:  # noqa: BLE001
                line = ""
            moves.append({"uci": pv[0].uci(), "san": board.san(pv[0]),
                          "eval": format_score(info["score"], board.turn), "line": line})
        if not moves:
            return
        self.q.put(("move", {
            "moves": moves, "flipped": flipped, "side": "White" if board.turn else "Black",
            "win": white_win_percent(infos[0]["score"]),
        }))
