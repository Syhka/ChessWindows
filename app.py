"""Chess Screen Assistant - reads a chessboard on your screen and shows Stockfish's best move."""
from __future__ import annotations

import ctypes
import glob
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import chess
import chess.engine
import mss
import numpy as np

from chessvision import START, BoardRecognizer, cell_for

APP_DIR = Path.home() / ".chess_screen_assistant"
CFG_FILE = APP_DIR / "config.json"
TPL_FILE = APP_DIR / "templates.npz"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------------------- helpers
def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_stockfish() -> str | None:
    bases = [Path(getattr(sys, "_MEIPASS", Path(__file__).parent)), Path(sys.argv[0]).resolve().parent]
    for base in bases:
        for pattern in ("engine/stockfish*.exe", "engine/stockfish*", "stockfish*.exe"):
            hits = sorted(glob.glob(str(base / pattern)))
            if hits:
                return hits[0]
    return shutil.which("stockfish")


def to_piece_map(pieces: dict[tuple[int, int], str]) -> dict[int, chess.Piece]:
    return {chess.square(f, r): chess.Piece.from_symbol(s) for (f, r), s in pieces.items()}


def infer_castling(board: chess.Board) -> str:
    s = ""
    wk, bk = chess.Piece(chess.KING, True), chess.Piece(chess.KING, False)
    wr, br = chess.Piece(chess.ROOK, True), chess.Piece(chess.ROOK, False)
    if board.piece_at(chess.E1) == wk:
        s += "K" if board.piece_at(chess.H1) == wr else ""
        s += "Q" if board.piece_at(chess.A1) == wr else ""
    if board.piece_at(chess.E8) == bk:
        s += "k" if board.piece_at(chess.H8) == br else ""
        s += "q" if board.piece_at(chess.A8) == br else ""
    return s or "-"


def build_board(pieces, prev: chess.Board | None, default_turn: bool) -> chess.Board:
    """Prefer continuing the previous game (keeps turn / castling / en passant exact)."""
    pm = to_piece_map(pieces)
    if prev is not None:
        if pm == prev.piece_map():
            return prev
        for mv in prev.legal_moves:
            nb = prev.copy(stack=False)
            nb.push(mv)
            if nb.piece_map() == pm:
                return nb
    turn = chess.WHITE if pieces == START else default_turn
    for t in (turn, not turn):
        board = chess.Board(None)
        board.set_piece_map(pm)
        board.turn = t
        board.set_castling_fen(infer_castling(board))
        if board.is_valid():
            return board
    board = chess.Board(None)
    board.set_piece_map(pm)
    board.turn = turn
    return board


def format_score(score: chess.engine.PovScore, turn: bool) -> str:
    sc = score.pov(turn)
    if sc.is_mate():
        m = sc.mate()
        return f"Mate in {abs(m)}" + ("" if m > 0 else " (against)")
    return f"{sc.score() / 100:+.2f}"


# ----------------------------------------------------------------------------- worker
class Worker(threading.Thread):
    def __init__(self, engine_path: str, region: dict, recognizer: BoardRecognizer,
                 settings: dict, out: "queue.Queue"):
        super().__init__(daemon=True)
        self.engine_path, self.region, self.rec = engine_path, region, recognizer
        self.s, self.q = settings, out
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            engine = chess.engine.SimpleEngine.popen_uci(self.engine_path, creationflags=CREATE_NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"Could not start Stockfish:\n{e}"))
            return
        try:
            try:
                engine.configure({"Threads": max(1, min(4, (os.cpu_count() or 2) - 1)), "Hash": 128})
            except Exception:
                pass
            self._loop(engine)
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"Worker crashed:\n{e!r}"))
        finally:
            try:
                engine.quit()
            except Exception:
                pass

    def _loop(self, engine: chess.engine.SimpleEngine) -> None:
        candidate, stable, accepted = None, 0, None
        prev_board: chess.Board | None = None
        last_skill = None
        with mss.mss() as sct:
            while not self._stop.is_set():
                time.sleep(0.25)
                frame = np.ascontiguousarray(np.array(sct.grab(self.region))[:, :, :3])
                flipped = self.s["flipped"]
                pieces, unknown = self.rec.recognize(frame, flipped)

                if pieces == candidate:
                    stable += 1
                else:
                    candidate, stable = pieces, 1
                if stable < 2 or pieces == accepted:
                    continue
                accepted = pieces

                kings_ok = list(pieces.values()).count("K") == 1 and list(pieces.values()).count("k") == 1
                if unknown or not kings_ok:
                    self.q.put(("status", "Can't read the board (is it fully visible and not covered?)", flipped))
                    continue

                my_color = chess.WHITE if not flipped else chess.BLACK
                board = build_board(pieces, prev_board, my_color)
                if not board.is_valid():
                    prev_board = None
                    self.q.put(("status", "Position looks illegal - probably misread. Recalibrate if it persists.", flipped))
                    continue
                prev_board = board

                if board.is_game_over():
                    self.q.put(("status", "Game over.", flipped))
                    continue
                if board.turn != my_color and not self.s["both"]:
                    self.q.put(("status", "Waiting for the opponent...", flipped))
                    continue

                skill = int(self.s["skill"])
                if skill != last_skill:
                    try:
                        engine.configure({"Skill Level": skill})
                    except Exception:
                        pass
                    last_skill = skill

                self.q.put(("status", "Thinking...", flipped))
                info = engine.analyse(board, chess.engine.Limit(time=float(self.s["time"])))
                pv = info.get("pv") or []
                if not pv:
                    continue
                best = pv[0]
                try:
                    line = board.variation_san(pv[:6])
                except Exception:
                    line = ""
                self.q.put(("move", {
                    "uci": best.uci(), "san": board.san(best),
                    "eval": format_score(info["score"], board.turn),
                    "line": line, "flipped": flipped,
                    "side": "White" if board.turn else "Black",
                }))


# ----------------------------------------------------------------------------- overlay
class Overlay:
    KEY = "#010101"

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.KEY)
        self.win.attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(self.win, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.region: dict | None = None
        self.win.withdraw()

    def place(self, region: dict) -> None:
        self.region = region
        self.win.geometry(f"{region['width']}x{region['height']}+{region['left']}+{region['top']}")
        self.win.deiconify()
        self.win.update_idletasks()
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.win.winfo_id())
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)  # layered, click-through, toolwindow
            user32.SetWindowDisplayAffinity(hwnd, 0x11)  # exclude from screen capture (Win10 2004+)
        except Exception:
            pass

    def hide(self) -> None:
        self.canvas.delete("all")
        self.win.withdraw()

    def clear(self) -> None:
        self.canvas.delete("all")

    def show_move(self, uci: str, flipped: bool) -> None:
        if not self.region:
            return
        self.canvas.delete("all")
        cw = self.region["width"] / 8
        ch = self.region["height"] / 8

        def center(sq: int) -> tuple[float, float]:
            row, col = cell_for(chess.square_file(sq), chess.square_rank(sq), flipped)
            return (col + 0.5) * cw, (row + 0.5) * ch

        mv = chess.Move.from_uci(uci)
        (x0, y0), (x1, y1) = center(mv.from_square), center(mv.to_square)
        r = cw * 0.48
        self.canvas.create_rectangle(x0 - r, y0 - r, x0 + r, y0 + r, outline="#22c55e", width=4)
        self.canvas.create_rectangle(x1 - r, y1 - r, x1 + r, y1 + r, outline="#22c55e", width=4)
        self.canvas.create_line(x0, y0, x1, y1, fill="#22c55e", width=max(6, int(cw * 0.14)),
                                arrow=tk.LAST, arrowshape=(26, 30, 12), capstyle="round")


# ----------------------------------------------------------------------------- region picker
class RegionSelector:
    def __init__(self, root: tk.Tk, on_done):
        with mss.mss() as s:
            mon = s.monitors[0]
        self.on_done = on_done
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.35)
        self.top.configure(bg="black")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(mon["width"] // 2, 50, fill="white", font=("Segoe UI", 18),
                                text="Drag from the TOP-LEFT to the BOTTOM-RIGHT corner of the 64 squares  (Esc = cancel)")
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.top.bind("<Escape>", self.cancel)
        self.top.focus_force()
        self.origin = self.local = None
        self.rect = None

    def press(self, e):
        self.origin, self.local = (e.x_root, e.y_root), (e.x, e.y)

    def drag(self, e):
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(*self.local, e.x, e.y, outline="#22c55e", width=3)

    def release(self, e):
        x0, y0 = self.origin
        x1, y1 = e.x_root, e.y_root
        w, h = abs(x1 - x0), abs(y1 - y0)
        self.top.destroy()
        if w < 80 or h < 80:
            self.on_done(None)
        else:
            side = min(w, h)
            self.on_done({"left": min(x0, x1), "top": min(y0, y1), "width": side, "height": side})

    def cancel(self, _e=None):
        self.top.destroy()
        self.on_done(None)


# ----------------------------------------------------------------------------- main app
class App:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("Chess Screen Assistant")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        self.q: "queue.Queue" = queue.Queue()
        self.cfg = self._load_cfg()
        self.recognizer = BoardRecognizer()
        if TPL_FILE.exists():
            try:
                self.recognizer.load(TPL_FILE)
            except Exception:
                pass
               saved_engine = self.cfg.get("engine")
        self.engine_path = saved_engine if saved_engine and os.path.exists(saved_engine) else find_stockfish()
        self.region = self.cfg.get("region")
        self.settings = {
            "flipped": bool(self.cfg.get("flipped", False)),
            "both": False,
            "time": float(self.cfg.get("time", 0.6)),
            "skill": int(self.cfg.get("skill", 20)),
        }
        self.overlay = Overlay(self.root)
        self.worker: Worker | None = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    # ---- config
    def _load_cfg(self) -> dict:
        try:
            return json.loads(CFG_FILE.read_text())
        except Exception:
            return {}
            
    def _engine_to_save(self):
        p = self.engine_path
        if p and not str(p).startswith(getattr(sys, "_MEIPASS", "\0")):
            return p
        return None

    def _save_cfg(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        data = {"region": self.region, "flipped": self.settings["flipped"], "time": self.settings["time"],
                                "skill": self.settings["skill"], "engine": self._engine_to_save()}
        CFG_FILE.write_text(json.dumps(data))

    # ---- UI
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        f = ttk.Frame(self.root, padding=8)
        f.pack()

        self.best_var = tk.StringVar(value="-")
        self.eval_var = tk.StringVar(value="")
        self.line_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value=self._initial_status())

        ttk.Label(f, textvariable=self.best_var, font=("Segoe UI", 28, "bold")).pack(**pad)
        ttk.Label(f, textvariable=self.eval_var, font=("Segoe UI", 12)).pack()
        ttk.Label(f, textvariable=self.line_var, wraplength=360, foreground="#555").pack(**pad)
        ttk.Label(f, textvariable=self.status_var, wraplength=360, foreground="#0a6").pack(**pad)
        ttk.Separator(f).pack(fill="x", pady=6)

        ttk.Button(f, text="1. Select board area", command=self.select_region).pack(fill="x", **pad)
        ttk.Button(f, text="2. Calibrate (start position visible)", command=self.calibrate).pack(fill="x", **pad)
        self.start_btn = ttk.Button(f, text="3. Start", command=self.toggle)
        self.start_btn.pack(fill="x", **pad)

        self.black_var = tk.BooleanVar(value=self.settings["flipped"])
        ttk.Checkbutton(f, text="I play Black (board is flipped)", variable=self.black_var,
                        command=self._opts_changed).pack(anchor="w", **pad)
        self.both_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Also suggest moves for the opponent's turn", variable=self.both_var,
                        command=self._opts_changed).pack(anchor="w", **pad)

        row = ttk.Frame(f)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Think time (s):").pack(side="left")
        self.time_var = tk.StringVar(value=str(self.settings["time"]))
        cb = ttk.Combobox(row, textvariable=self.time_var, values=["0.3", "0.6", "1", "2", "5"], width=5)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._opts_changed())
        cb.bind("<FocusOut>", lambda _e: self._opts_changed())

        row2 = ttk.Frame(f)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Strength (0-20):").pack(side="left")
        self.skill = tk.Scale(row2, from_=0, to=20, orient="horizontal", length=170,
                              command=lambda v: self.settings.__setitem__("skill", int(float(v))))
        self.skill.set(self.settings["skill"])
        self.skill.pack(side="left", padx=6)

        ttk.Button(f, text="Choose Stockfish .exe...", command=self.choose_engine).pack(fill="x", **pad)

    def _initial_status(self) -> str:
        if not self.engine_path:
            return "Stockfish not found - choose it below."
        if not self.region:
            return "Step 1: select the board area."
        if not self.recognizer.ready:
            return "Step 2: open a start position and calibrate."
        return "Ready - press Start."

    def _opts_changed(self) -> None:
        self.settings["flipped"] = self.black_var.get()
        self.settings["both"] = self.both_var.get()
        try:
            self.settings["time"] = max(0.1, float(self.time_var.get()))
        except ValueError:
            pass
        self._save_cfg()

    # ---- actions
    def select_region(self) -> None:
        self.root.withdraw()
        self.root.after(250, lambda: RegionSelector(self.root, self._region_done))

    def _region_done(self, region) -> None:
        self.root.deiconify()
        if region:
            self.region = region
            self._save_cfg()
            self.status_var.set("Board area saved. Now calibrate.")

    def _grab(self) -> np.ndarray:
        with mss.mss() as sct:
            return np.ascontiguousarray(np.array(sct.grab(self.region))[:, :, :3])

    def calibrate(self) -> None:
        if not self.region:
            messagebox.showinfo("Calibrate", "Select the board area first.")
            return
        self._opts_changed()
        ok, msg = self.recognizer.calibrate(self._grab(), self.settings["flipped"])
        if ok:
            self.recognizer.save(TPL_FILE)
        self.status_var.set(msg)
        messagebox.showinfo("Calibration", msg)

    def choose_engine(self) -> None:
        path = filedialog.askopenfilename(title="Select Stockfish executable",
                                          filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.engine_path = path
            self._save_cfg()
            self.status_var.set("Engine set.")

    def toggle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop()
            return
        if not self.engine_path:
            messagebox.showerror("Stockfish", "Stockfish not found. Use 'Choose Stockfish .exe...'.")
            return
        if not self.region or not self.recognizer.ready:
            messagebox.showinfo("Start", "Select the board area and calibrate first.")
            return
        self._opts_changed()
        self.overlay.place(self.region)
        self.worker = Worker(self.engine_path, self.region, self.recognizer, self.settings, self.q)
        self.worker.start()
        self.start_btn.config(text="Stop")
        self.status_var.set("Watching the board...")

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.overlay.hide()
        self.start_btn.config(text="3. Start")
        self.status_var.set("Stopped.")

    # ---- UI updates from worker
    def poll(self) -> None:
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "move":
                    d = rest[0]
                    self.best_var.set(f"{d['san']}   ({d['uci']})")
                    self.eval_var.set(f"{d['side']} to move  |  eval {d['eval']}")
                    self.line_var.set(d["line"])
                    self.status_var.set("Best move shown on the board.")
                    self.overlay.show_move(d["uci"], d["flipped"])
                elif kind == "status":
                    self.status_var.set(rest[0])
                    if rest[0] != "Thinking...":
                        self.overlay.clear()
                        self.best_var.set("-")
                        self.eval_var.set("")
                        self.line_var.set("")
                elif kind == "error":
                    self.stop()
                    messagebox.showerror("Error", rest[0])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self) -> None:
        self.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
