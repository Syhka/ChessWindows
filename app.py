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

# modern dark palette
BG, CARD, CARD2 = "#12151c", "#1b2030", "#232a3d"
FG, MUTED, ACCENT, ACCENT_HOT, WARN = "#e6e9f0", "#8b94a7", "#22c55e", "#16a34a", "#f59e0b"
FONT = "Segoe UI"


def resource_path(name: str) -> str:
    """Find a bundled asset both when running from source and from a PyInstaller exe."""
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))):
        if base:
            p = os.path.join(base, "assets", name)
            if os.path.exists(p):
                return p
    return ""


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
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            setp = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get.restype, get.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int]
            setp.restype, setp.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            style = get(hwnd, -20)
            setp(hwnd, -20, style | 0x80000 | 0x20 | 0x80)  # layered, click-through, toolwindow
            user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
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
        self.root.configure(bg=BG)
        self._icon_img = None
        try:
            ico = resource_path("icon.ico")
            if ico:
                self.root.iconbitmap(ico)
            png = resource_path("icon.png")
            if png:
                self._icon_img = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            log.warning("Could not load the window icon")
        self._style()

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
    def _style(self) -> None:
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, font=(FONT, 10), borderwidth=0, focuscolor=BG)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        st.configure("TCheckbutton", background=BG, foreground=FG, font=(FONT, 10))
        st.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#06240f", font=(FONT, 11, "bold"),
                     padding=(12, 9), borderwidth=0)
        st.map("Accent.TButton", background=[("active", ACCENT_HOT), ("pressed", ACCENT_HOT)])
        st.configure("Step.TButton", background=CARD2, foreground=FG, font=(FONT, 10), padding=(12, 8))
        st.map("Step.TButton", background=[("active", "#2d3650"), ("pressed", "#2d3650")])
        st.configure("Ghost.TButton", background=BG, foreground=MUTED, font=(FONT, 9), padding=(8, 5))
        st.map("Ghost.TButton", foreground=[("active", FG)], background=[("active", CARD)])
        st.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG,
                     arrowcolor=FG, selectbackground=CARD2, selectforeground=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", CARD2)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", CARD2)], selectforeground=[("readonly", FG)])
        st.configure("Horizontal.TScale", background=BG, troughcolor=CARD2)
        self.root.option_add("*TCombobox*Listbox.background", CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _card(self, parent) -> ttk.Frame:
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        outer.pack(fill="x", pady=(0, 10))
        return outer

    def _build_ui(self) -> None:
        W = 360
        f = ttk.Frame(self.root, padding=14)
        f.pack(fill="both")

        self.best_var = tk.StringVar(value="-")
        self.eval_var = tk.StringVar(value="")
        self.line_var = tk.StringVar(value="")
        self.alt_var = tk.StringVar(value="")
        self.fen_var = tk.StringVar(value="")
        self.engine_var = tk.StringVar(value="")
        self.bar_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value=self._initial_status())

        # header
        head = ttk.Frame(f)
        head.pack(fill="x", pady=(0, 10))
        if self._icon_img is not None:
            small = self._icon_img.subsample(6, 6)  # 256 -> ~43 px
            self._icon_small = small
            ttk.Label(head, image=small).pack(side="left", padx=(0, 10))
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="Chess Screen Assistant", font=(FONT, 14, "bold")).pack(anchor="w")
        ttk.Label(titles, text="Stockfish on your screen", foreground=MUTED, font=(FONT, 9)).pack(anchor="w")

        # best move card
        card = self._card(f)
        ttk.Label(card, textvariable=self.best_var, style="Card.TLabel", font=(FONT, 26, "bold"),
                  foreground=ACCENT).pack(anchor="w")
        ttk.Label(card, textvariable=self.eval_var, style="Card.TLabel", font=(FONT, 10)).pack(anchor="w")
        self.bar = tk.Canvas(card, width=W - 28, height=12, bg=CARD2, highlightthickness=0)
        self.bar.pack(fill="x", pady=(8, 2))
        ttk.Label(card, textvariable=self.bar_var, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.line_var, style="Muted.TLabel", wraplength=W - 28).pack(anchor="w", pady=(6, 0))
        ttk.Label(card, textvariable=self.alt_var, style="Card.TLabel", foreground=WARN, font=(FONT, 9),
                  wraplength=W - 28).pack(anchor="w")
        self._set_bar(None)

        # status
        self.status_lbl = ttk.Label(f, textvariable=self.status_var, wraplength=W, foreground=ACCENT,
                                    font=(FONT, 9))
        self.status_lbl.pack(anchor="w", pady=(0, 10))

        # steps
        ttk.Button(f, text="1   Select board area", style="Step.TButton", command=self.select_region).pack(fill="x", pady=2)
        ttk.Button(f, text="2   Calibrate  (start position visible)", style="Step.TButton",
                   command=self.calibrate).pack(fill="x", pady=2)
        self.start_btn = ttk.Button(f, text="3   Start   (F8)", style="Accent.TButton", command=self.toggle)
        self.start_btn.pack(fill="x", pady=(8, 10))

        # options
        self.black_var = tk.BooleanVar(value=self.settings["flipped"])
        ttk.Checkbutton(f, text="I play Black (board is flipped)", variable=self.black_var,
                        command=self._opts_changed).pack(anchor="w", pady=2)
        self.both_var = tk.BooleanVar(value=self.settings["both"])
        ttk.Checkbutton(f, text="Also suggest moves for the opponent's turn", variable=self.both_var,
                        command=self._opts_changed).pack(anchor="w", pady=2)

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text="Think time (s)").pack(side="left")
        self.time_var = tk.StringVar(value=str(self.settings["time"]))
        cb = ttk.Combobox(row, textvariable=self.time_var, values=["0.3", "0.6", "1", "2", "5"], width=5)
        cb.pack(side="left", padx=(6, 14))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._opts_changed())
        cb.bind("<FocusOut>", lambda _e: self._opts_changed())
        ttk.Label(row, text="Top moves").pack(side="left")
        self.lines_var = tk.StringVar(value=str(self.settings["lines"]))
        lb = ttk.Combobox(row, textvariable=self.lines_var, values=["1", "2", "3"], width=3, state="readonly")
        lb.pack(side="left", padx=6)
        lb.bind("<<ComboboxSelected>>", lambda _e: self._opts_changed())

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 2))
        ttk.Label(row2, text="Strength").pack(side="left")
        self.skill_val = tk.StringVar(value=str(self.settings["skill"]))
        self.skill = ttk.Scale(row2, from_=0, to=20, orient="horizontal", length=190,
                               command=self._skill_moved)
        self.skill.set(self.settings["skill"])
        self.skill.bind("<ButtonRelease-1>", lambda _e: self._opts_changed())
        self.skill.pack(side="left", padx=8)
        ttk.Label(row2, textvariable=self.skill_val, width=3).pack(side="left")

        # footer
        row3 = ttk.Frame(f)
        row3.pack(fill="x", pady=(12, 0))
        ttk.Button(row3, text="Copy FEN", style="Ghost.TButton", command=self.copy_fen).pack(side="left")
        ttk.Button(row3, text="Choose engine...", style="Ghost.TButton", command=self.choose_engine).pack(side="left")
        ttk.Button(row3, text="Logs", style="Ghost.TButton", command=self.open_logs).pack(side="left")
        ttk.Label(f, textvariable=self.fen_var, wraplength=W, foreground="#5b6478", font=("Consolas", 8)).pack(anchor="w")
        ttk.Label(f, textvariable=self.engine_var, foreground="#5b6478", font=(FONT, 8)).pack(anchor="w")

    def _skill_moved(self, v: str) -> None:
        val = int(round(float(v)))
        self.settings["skill"] = val
        self.skill_val.set(str(val))

    def _set_bar(self, white_pct: float | None) -> None:
        """Eval bar: white share on the left, dark share on the right."""
        c = self.bar
        c.update_idletasks()
        w, h = max(c.winfo_width(), 100), 12
        c.delete("all")
        pct = 50.0 if white_pct is None else max(0.0, min(100.0, white_pct))
        x = w * pct / 100.0
        c.create_rectangle(0, 0, x, h, fill="#f1f5f9", width=0)
        c.create_rectangle(x, 0, w, h, fill="#0b0e14", width=0)
        c.create_line(w / 2, 0, w / 2, h, fill="#64748b")

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
        self.start_btn.config(text="Stop   (F8)")
        self.status_var.set("Starting engine...")

    def stop(self, wait: bool = False) -> None:
        w, self.worker = self.worker, None
        if w:
            w.stop()
            if wait:
                w.join(timeout=3)
        self.overlay.hide()
        self.start_btn.config(text="3   Start   (F8)")
        self.status_var.set("Stopped.")

    # ---- UI updates from worker
    def _reset_display(self) -> None:
        self.best_var.set("-")
        self.eval_var.set("")
        self.line_var.set("")
        self.alt_var.set("")
        self._set_bar(None)
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
                    self._set_bar(d["win"])
                    self.bar_var.set(f"White {d['win']:.0f}%  |  Black {100 - d['win']:.0f}%")
                    self.status_var.set("Best move shown on the board.")
                    self.overlay.show_moves([m["uci"] for m in d["moves"]], d["flipped"])
                elif kind == "status":
                    self.status_var.set(rest[0])
                    bad = any(w in rest[0] for w in ("Can't", "illegal", "Hiccup", "stopped unexpectedly"))
                    self.status_lbl.config(foreground=WARN if bad else ACCENT)
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
