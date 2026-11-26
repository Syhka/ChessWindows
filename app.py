"""Chess Screen Assistant - reads a chessboard on your screen and shows Stockfish's best moves.

Use it for analysis, puzzles, practice against bots and reviewing your own games. Do not use engine
help where it is forbidden (rated/online games, tournaments) - that is cheating.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chess
import mss
import numpy as np

from chessvision import BoardRecognizer, cell_for
from common import APP_DIR, log, setup_logging
from engines import candidate_engines, run_selftest
from worker import Worker

CFG_FILE = APP_DIR / "config.json"
TPL_FILE = APP_DIR / "templates.npz"
HOTKEY_VK = 0x77  # F8 = start / stop (works even when another window has focus)
ARROW_COLORS = ["#22c55e", "#eab308", "#f97316"]  # best, 2nd, 3rd


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------- overlay
class Overlay:
    """Transparent click-through window drawn over the board.

    It is excluded from screen capture (Win10 2004+) so our own arrows never end up in the
    frames the recogniser reads - otherwise the arrows would corrupt the position it sees.
    """
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
            user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        except Exception:  # noqa: BLE001
            log.warning("Could not set overlay window styles (older Windows?)")

    def hide(self) -> None:
        self.canvas.delete("all")
        self.win.withdraw()

    def clear(self) -> None:
        self.canvas.delete("all")

    def show_moves(self, ucis: list[str], flipped: bool) -> None:
        if not self.region:
            return
        self.canvas.delete("all")
        cw = self.region["width"] / 8
        ch = self.region["height"] / 8

        def center(sq: int) -> tuple[float, float]:
            row, col = cell_for(chess.square_file(sq), chess.square_rank(sq), flipped)
            return (col + 0.5) * cw, (row + 0.5) * ch

        for rank in reversed(range(len(ucis))):  # draw the best move last so it is on top
            mv = chess.Move.from_uci(ucis[rank])
            (x0, y0), (x1, y1) = center(mv.from_square), center(mv.to_square)
            color = ARROW_COLORS[min(rank, len(ARROW_COLORS) - 1)]
            r = cw * (0.48 if rank == 0 else 0.40)
            width = max(6, int(cw * 0.14)) if rank == 0 else max(4, int(cw * 0.09))
            self.canvas.create_rectangle(x0 - r, y0 - r, x0 + r, y0 + r, outline=color, width=4 if rank == 0 else 3)
            self.canvas.create_rectangle(x1 - r, y1 - r, x1 + r, y1 + r, outline=color, width=4 if rank == 0 else 3)
            self.canvas.create_line(x0, y0, x1, y1, fill=color, width=width,
                                    arrow=tk.LAST, arrowshape=(26, 30, 12) if rank == 0 else (18, 22, 8),
                                    capstyle="round")
            if rank > 0:
                self.canvas.create_text(x1, y1, text=str(rank + 1), fill=color, font=("Segoe UI", int(cw * 0.3), "bold"))


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
        if self.origin is None:
            return self.cancel()
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
            except Exception:  # noqa: BLE001
                log.exception("Could not load saved calibration")
        saved = self.cfg.get("engine")
        self.user_engine: str | None = saved if saved and os.path.exists(saved) else None
        self.region = self.cfg.get("region")
        self.settings = {
            "flipped": bool(self.cfg.get("flipped", False)),
            "both": bool(self.cfg.get("both", False)),
            "time": float(self.cfg.get("time", 0.6)),
            "skill": int(self.cfg.get("skill", 20)),
            "lines": int(self.cfg.get("lines", 1)),
        }
        self.overlay = Overlay(self.root)
        self.worker: Worker | None = None
        self._f8_down = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    # ---- config
    def _load_cfg(self) -> dict:
        try:
            return json.loads(CFG_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_cfg(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            data = {"region": self.region, **{k: self.settings[k] for k in ("flipped", "both", "time", "skill", "lines")},
                    "engine": self.user_engine}
            CFG_FILE.write_text(json.dumps(data))
        except OSError:
            log.exception("Could not save config")

    # ---- UI
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        f = ttk.Frame(self.root, padding=8)
        f.pack()

        self.best_var = tk.StringVar(value="-")
        self.eval_var = tk.StringVar(value="")
        self.line_var = tk.StringVar(value="")
        self.alt_var = tk.StringVar(value="")
        self.fen_var = tk.StringVar(value="")
        self.engine_var = tk.StringVar(value="")
        self.bar_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value=self._initial_status())

        ttk.Label(f, textvariable=self.best_var, font=("Segoe UI", 28, "bold")).pack(**pad)
        ttk.Label(f, textvariable=self.eval_var, font=("Segoe UI", 12)).pack()
        self.bar = ttk.Progressbar(f, length=340, maximum=100, value=50)
        self.bar.pack(padx=10, pady=(6, 0))
        ttk.Label(f, textvariable=self.bar_var, foreground="#555", font=("Segoe UI", 8)).pack()
        ttk.Label(f, textvariable=self.line_var, wraplength=360, foreground="#555").pack(**pad)
        ttk.Label(f, textvariable=self.alt_var, wraplength=360, foreground="#a60").pack()
        ttk.Label(f, textvariable=self.status_var, wraplength=360, foreground="#0a6").pack(**pad)
        ttk.Separator(f).pack(fill="x", pady=6)

        ttk.Button(f, text="1. Select board area", command=self.select_region).pack(fill="x", **pad)
        ttk.Button(f, text="2. Calibrate (start position visible)", command=self.calibrate).pack(fill="x", **pad)
        self.start_btn = ttk.Button(f, text="3. Start  (F8)", command=self.toggle)
        self.start_btn.pack(fill="x", **pad)

        self.black_var = tk.BooleanVar(value=self.settings["flipped"])
        ttk.Checkbutton(f, text="I play Black (board is flipped)", variable=self.black_var,
                        command=self._opts_changed).pack(anchor="w", **pad)
        self.both_var = tk.BooleanVar(value=self.settings["both"])
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
        ttk.Label(row, text="Top moves:").pack(side="left", padx=(12, 0))
        self.lines_var = tk.StringVar(value=str(self.settings["lines"]))
        lb = ttk.Combobox(row, textvariable=self.lines_var, values=["1", "2", "3"], width=3, state="readonly")
        lb.pack(side="left", padx=6)
        lb.bind("<<ComboboxSelected>>", lambda _e: self._opts_changed())

        row2 = ttk.Frame(f)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Strength (0-20):").pack(side="left")
        self.skill = tk.Scale(row2, from_=0, to=20, orient="horizontal", length=170,
                              command=lambda v: self.settings.__setitem__("skill", int(float(v))))
        self.skill.set(self.settings["skill"])
        self.skill.bind("<ButtonRelease-1>", lambda _e: self._opts_changed())
        self.skill.pack(side="left", padx=6)

        row3 = ttk.Frame(f)
        row3.pack(fill="x", **pad)
        ttk.Button(row3, text="Copy FEN", command=self.copy_fen).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(row3, text="Open log folder", command=self.open_logs).pack(side="left", expand=True, fill="x", padx=(3, 0))
        ttk.Label(f, textvariable=self.fen_var, wraplength=360, foreground="#888", font=("Consolas", 8)).pack()
        ttk.Button(f, text="Choose Stockfish .exe...", command=self.choose_engine).pack(fill="x", **pad)
        ttk.Label(f, textvariable=self.engine_var, foreground="#888", font=("Segoe UI", 8)).pack()

    def _initial_status(self) -> str:
        if not candidate_engines(self.user_engine):
            return "Stockfish not found - choose it below."
        if not self.region:
            return "Step 1: select the board area."
        if not self.recognizer.ready:
            return "Step 2: open a start position and calibrate."
        return "Ready - press Start (or F8)."

    def _opts_changed(self) -> None:
        self.settings["flipped"] = self.black_var.get()
        self.settings["both"] = self.both_var.get()
        try:
            self.settings["time"] = min(30.0, max(0.1, float(self.time_var.get())))
        except ValueError:
            pass
        try:
            self.settings["lines"] = min(3, max(1, int(self.lines_var.get())))
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
        try:
            ok, msg = self.recognizer.calibrate(self._grab(), self.settings["flipped"])
        except Exception as e:  # noqa: BLE001
            log.exception("Calibration failed")
            ok, msg = False, f"Calibration error: {e!r}"
        if ok:
            self.recognizer.save(TPL_FILE)
        self.status_var.set(msg)
        messagebox.showinfo("Calibration", msg)

    def choose_engine(self) -> None:
        path = filedialog.askopenfilename(title="Select Stockfish executable",
                                          filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.user_engine = path
            self._save_cfg()
            self.status_var.set("Engine set.")

    def copy_fen(self) -> None:
        fen = self.fen_var.get()
        if fen:
            self.root.clipboard_clear()
            self.root.clipboard_append(fen)
            self.status_var.set("FEN copied - paste it into any analysis board.")

    def open_logs(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(APP_DIR)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            messagebox.showinfo("Logs", f"Log folder:\n{APP_DIR}")

    def toggle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop()
            return
        if not candidate_engines(self.user_engine):
            messagebox.showerror("Stockfish", "Stockfish not found. Use 'Choose Stockfish .exe...'.")
            return
        if not self.region or not self.recognizer.ready:
            messagebox.showinfo("Start", "Select the board area and calibrate first.")
            return
        self._opts_changed()
        self.overlay.place(self.region)
        self.worker = Worker(self.user_engine, self.region, self.recognizer, self.settings, self.q)
        self.worker.start()
        self.start_btn.config(text="Stop  (F8)")
        self.status_var.set("Starting engine...")

    def stop(self, wait: bool = False) -> None:
        w, self.worker = self.worker, None
        if w:
            w.stop()
            if wait:
                w.join(timeout=3)
        self.overlay.hide()
        self.start_btn.config(text="3. Start  (F8)")
        self.status_var.set("Stopped.")

    # ---- UI updates from worker
    def _reset_display(self) -> None:
        self.best_var.set("-")
        self.eval_var.set("")
        self.line_var.set("")
        self.alt_var.set("")
        self.bar.config(value=50)
        self.bar_var.set("")

    def poll(self) -> None:
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "move":
                    d = rest[0]
                    best = d["moves"][0]
                    self.best_var.set(f"{best['san']}   ({best['uci']})")
                    self.eval_var.set(f"{d['side']} to move  |  eval {best['eval']}")
                    self.line_var.set(best["line"])
                    self.alt_var.set("   ".join(f"{i}) {m['san']} ({m['eval']})"
                                                for i, m in enumerate(d["moves"][1:], start=2)))
                    self.bar.config(value=d["win"])
                    self.bar_var.set(f"White {d['win']:.0f}%  |  Black {100 - d['win']:.0f}%")
                    self.status_var.set("Best move shown on the board.")
                    self.overlay.show_moves([m["uci"] for m in d["moves"]], d["flipped"])
                elif kind == "status":
                    self.status_var.set(rest[0])
                    if rest[0] != "Thinking...":
                        self.overlay.clear()
                        self._reset_display()
                elif kind == "fen":
                    self.fen_var.set(rest[0])
                elif kind == "engine":
                    self.engine_var.set(f"Engine: {rest[0]}")
                    self.status_var.set("Watching the board...")
                elif kind == "error":
                    self.stop()
                    messagebox.showerror("Error", rest[0])
        except queue.Empty:
            pass
        self._poll_hotkey()
        self.root.after(100, self.poll)

    def _poll_hotkey(self) -> None:
        try:
            down = bool(ctypes.windll.user32.GetAsyncKeyState(HOTKEY_VK) & 0x8000)
        except Exception:  # noqa: BLE001
            return
        if down and not self._f8_down:
            self.toggle()
        self._f8_down = down

    def on_close(self) -> None:
        self.stop(wait=True)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    setup_logging()
    try:
        App().run()
    except Exception:  # noqa: BLE001
        log.exception("Fatal error")
        try:
            messagebox.showerror("Fatal error", f"The app crashed. Details are in:\n{APP_DIR}")
        except Exception:  # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    main()
