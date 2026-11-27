"""Chessboard recognition from a screenshot.

Theme independent: instead of shipping piece images, the recognizer *learns* the
piece silhouettes from the starting position currently displayed on screen
(calibration). Afterwards every square is classified by:
  * silhouette shape  -> piece type   (IoU against the 12 learned templates, with a small
                                       shift tolerance so a slightly misaligned selection still works)
  * fill brightness   -> piece colour (white pieces are brighter than black ones)
The square background is estimated from the square's own border, so board colours,
last-move highlights and different themes do not matter.

New in this version
  * the board edges are located automatically inside the selected area (checkerboard search),
    so the selection no longer has to be pixel-exact;
  * shift-tolerant matching and a lower, margin-aware acceptance threshold;
  * unreadable squares are reported individually so the caller can patch them from the game history.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

BOARD_PX = 512
SQ = BOARD_PX // 8
CROP = 40
EMPTY_AREA = 0.075     # silhouette area (fraction of the crop) below which a square is "empty"
MIN_IOU = 0.38         # below this the square is "unknown"
SHIFTS = ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2))

# (file, rank) -> piece symbol for the starting position. file/rank are 0..7 (a1 = (0,0)).
START: dict[tuple[int, int], str] = {}
for _f, _p in enumerate("RNBQKBNR"):
    START[(_f, 0)] = _p
    START[(_f, 7)] = _p.lower()
for _f in range(8):
    START[(_f, 1)] = "P"
    START[(_f, 6)] = "p"


def square_for(row: int, col: int, flipped: bool) -> tuple[int, int]:
    """Image cell -> (file, rank)."""
    return (7 - col, row) if flipped else (col, 7 - row)


def cell_for(file: int, rank: int, flipped: bool) -> tuple[int, int]:
    """(file, rank) -> image (row, col)."""
    return (rank, 7 - file) if flipped else (7 - rank, file)


# ---------------------------------------------------------------------------- board location
def locate_board(frame: np.ndarray, max_trim: float = 0.07) -> tuple[float, float, float, float]:
    """Find the tight board rectangle inside `frame` as fractions (x0, y0, x1, y1).

    Looks for the 8x8 alternating light/dark pattern by comparing the border ring of every cell.
    Only small shrinks/shifts are searched, so it can never lock on to a pattern that is a whole
    square off. Returns the full frame if nothing convincing is found.
    """
    n = 256
    gray = cv2.cvtColor(cv2.resize(frame, (n, n), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
    ii = cv2.integral(gray)

    def mean(x0: int, y0: int, x1: int, y1: int) -> float:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(n, x1), min(n, y1)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        s = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]
        return float(s) / ((x1 - x0) * (y1 - y0))

    cands: list[tuple[float, int, int, int]] = []   # (score, ox, oy, size)
    lim = int(n * max_trim)
    for size in range(n - lim, n + 1, 4):
        cell = size / 8.0
        inset = max(2, int(cell * 0.07))
        for oy in range(0, n - size + 1, 3):
            for ox in range(0, n - size + 1, 3):
                light: list[float] = []
                dark: list[float] = []
                for r in range(8):
                    for c in range(8):
                        cx0, cy0 = ox + c * cell, oy + r * cell
                        # four thin strips along the cell's inner border (pieces rarely reach them)
                        a, b = int(cx0 + inset), int(cy0 + inset)
                        e, f = int(cx0 + cell - inset), int(cy0 + cell - inset)
                        v = (mean(a, b, e, b + 2) + mean(a, f - 2, e, f)
                             + mean(a, b, a + 2, f) + mean(e - 2, b, e, f)) / 4.0
                        (light if (r + c) % 2 == 0 else dark).append(v)
                lm, dm = np.mean(light), np.mean(dark)
                spread = (np.std(light) + np.std(dark)) / 2.0
                cands.append((abs(lm - dm) / (spread + 4.0), ox, oy, size))
    top = max(c[0] for c in cands)
    if top < 1.2:                        # no clear checkerboard: keep what the user selected
        return 0.0, 0.0, 1.0, 1.0
    # the score plateaus around the true edges: of the near-best candidates take the least-trimmed one
    near = [c for c in cands if c[0] >= 0.8 * top]
    _, ox, oy, size = max(near, key=lambda c: (c[3], -(c[1] + c[2])))
    return ox / n, oy / n, (ox + size) / n, (oy + size) / n


class BoardRecognizer:
    def __init__(self) -> None:
        self.templates: dict[str, np.ndarray] = {}
        self.threshold: float | None = None
        self.box: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
        self.last_unknown: list[tuple[int, int]] = []

    @property
    def ready(self) -> bool:
        return len(self.templates) == 12 and self.threshold is not None

    # ------------------------------------------------------------------ features
    def _prepare(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = self.box
        crop = frame[int(y0 * h):max(int(y0 * h) + 8, int(round(y1 * h))),
                     int(x0 * w):max(int(x0 * w) + 8, int(round(x1 * w)))]
        return cv2.resize(crop, (BOARD_PX, BOARD_PX), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _features(board: np.ndarray, r: int, c: int) -> tuple[np.ndarray, float]:
        m = int(SQ * 0.08)
        crop = board[r * SQ + m:(r + 1) * SQ - m, c * SQ + m:(c + 1) * SQ - m]
        crop = cv2.resize(crop, (CROP, CROP), interpolation=cv2.INTER_AREA)
        c32 = crop.astype(np.float32)

        ring = np.concatenate([
            c32[:3].reshape(-1, 3), c32[-3:].reshape(-1, 3),
            c32[:, :3].reshape(-1, 3), c32[:, -3:].reshape(-1, 3),
        ])
        bg = np.median(ring, axis=0)
        dist = np.sqrt(((c32 - bg) ** 2).sum(axis=2))

        mask = (dist > 35).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        sil = np.zeros_like(mask)
        if contours:
            biggest = max(contours, key=cv2.contourArea)
            cv2.drawContours(sil, [biggest], -1, 1, thickness=cv2.FILLED)

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        inner = cv2.erode(sil, np.ones((5, 5), np.uint8))
        sel = inner > 0 if inner.sum() > 20 else sil > 0
        g = float(np.median(gray[sel])) if sel.any() else 0.0
        return sil, g

    @staticmethod
    def _iou(sil: np.ndarray, t: np.ndarray) -> float:
        best = 0.0
        for dx, dy in SHIFTS:
            ts = t if (dx, dy) == (0, 0) else np.roll(np.roll(t, dy, axis=0), dx, axis=1)
            union = np.logical_or(sil, ts).sum()
            if union:
                best = max(best, float(np.logical_and(sil, ts).sum() / union))
        return best

    # --------------------------------------------------------------- calibration
    def _learn(self, frame: np.ndarray, flipped: bool) -> bool:
        """Learn templates + colour threshold for the current self.box. False if the pieces are not visible."""
        board = self._prepare(frame)
        masks: dict[str, list[np.ndarray]] = {}
        grays: dict[str, list[float]] = {}
        for r in range(8):
            for c in range(8):
                sym = START.get(square_for(r, c, flipped))
                if sym is None:
                    continue
                sil, g = self._features(board, r, c)
                masks.setdefault(sym, []).append(sil.astype(np.float32))
                grays.setdefault(sym, []).append(g)
        templates = {s: (np.mean(v, axis=0) > 0.5).astype(np.uint8) for s, v in masks.items()}
        white = float(np.mean([g for s, v in grays.items() if s.isupper() for g in v]))
        black = float(np.mean([g for s, v in grays.items() if s.islower() for g in v]))
        if len(templates) < 12 or any(t.mean() < 0.05 for t in templates.values()) or white - black < 40:
            return False
        self.templates, self.threshold = templates, (white + black) / 2
        return True

    def calibrate(self, frame: np.ndarray, flipped: bool) -> tuple[bool, str]:
        """Learn piece templates from a screenshot showing the START position.

        Tries the automatically located board edges and the raw selection, keeps whichever reads
        the start position better.
        """
        boxes = [locate_board(frame), (0.0, 0.0, 1.0, 1.0)]
        best: tuple[int, int, dict, float, tuple] | None = None
        for box in dict.fromkeys(boxes):
            self.box, self.templates, self.threshold = box, {}, None
            if not self._learn(frame, flipped):
                continue
            pieces, _ = self.recognize(frame, flipped)
            correct = sum(1 for k, v in START.items() if pieces.get(k) == v)
            extras = sum(1 for k in pieces if k not in START)
            if best is None or (correct, -extras) > (best[0], -best[1]):
                best = (correct, extras, self.templates, self.threshold, box)
        if best is None:
            self.templates, self.threshold, self.box = {}, None, (0.0, 0.0, 1.0, 1.0)
            return False, ("Could not see the pieces clearly. Make sure the board area is selected "
                           "(all 64 squares, nothing covering them), the starting position is shown, and the "
                           "'I play Black' box matches the board orientation.")
        correct, extras, self.templates, self.threshold, self.box = best
        if correct < 28:
            self.templates, self.threshold = {}, None
            return False, (f"Calibration check failed ({correct}/32 pieces recognised). "
                           "Check the 'I play Black' box and that nothing covers the board.")
        return True, f"Calibrated. Self-test: {correct}/32 pieces recognised, {extras} false pieces."

    # --------------------------------------------------------------- recognition
    def recognize(self, frame: np.ndarray, flipped: bool) -> tuple[dict[tuple[int, int], str], int]:
        """Return ({(file, rank): symbol}, number_of_unreadable_squares).

        The unreadable squares themselves are left in `self.last_unknown` as (file, rank).
        """
        board = self._prepare(frame)
        pieces: dict[tuple[int, int], str] = {}
        self.last_unknown = []
        for r in range(8):
            for c in range(8):
                sil, g = self._features(board, r, c)
                if sil.mean() < EMPTY_AREA:
                    continue
                best_sym, best_iou = "", 0.0
                for sym, t in self.templates.items():
                    iou = self._iou(sil, t)
                    if iou > best_iou:
                        best_sym, best_iou = sym, iou
                if best_iou < MIN_IOU:
                    self.last_unknown.append(square_for(r, c, flipped))
                    continue
                letter = best_sym.lower()
                pieces[square_for(r, c, flipped)] = letter.upper() if g > self.threshold else letter
        return pieces, len(self.last_unknown)

    # --------------------------------------------------------------- persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {("w_" if s.isupper() else "b_") + s.lower(): t for s, t in self.templates.items()}
        np.savez_compressed(path, threshold=np.array(self.threshold), box=np.array(self.box), **data)

    def load(self, path: Path) -> None:
        z = np.load(path)
        self.threshold = float(z["threshold"])
        self.box = tuple(float(v) for v in z["box"]) if "box" in z.files else (0.0, 0.0, 1.0, 1.0)
        self.templates = {}
        for key in z.files:
            if key.startswith("w_"):
                self.templates[key[2:].upper()] = z[key]
            elif key.startswith("b_"):
                self.templates[key[2:]] = z[key]
