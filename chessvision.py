"""Chessboard recognition from a screenshot.

Theme independent: instead of shipping piece images, the recognizer *learns* the
piece silhouettes from the starting position currently displayed on screen
(calibration). Afterwards every square is classified by:
  * silhouette shape  -> piece type   (IoU against the 12 learned templates)
  * fill brightness   -> piece colour (white pieces are brighter than black ones)
The square background is estimated from the square's own border, so board
colours, last-move highlights and different themes do not matter.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

BOARD_PX = 512
SQ = BOARD_PX // 8
CROP = 40
EMPTY_AREA = 0.08      # silhouette area (fraction of the crop) below which a square is "empty"
MIN_IOU = 0.45         # below this the square is "unknown"

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


class BoardRecognizer:
    def __init__(self) -> None:
        self.templates: dict[str, np.ndarray] = {}
        self.threshold: float | None = None

    @property
    def ready(self) -> bool:
        return len(self.templates) == 12 and self.threshold is not None

    # ------------------------------------------------------------------ features
    @staticmethod
    def _prepare(frame: np.ndarray) -> np.ndarray:
        return cv2.resize(frame, (BOARD_PX, BOARD_PX), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _features(board: np.ndarray, r: int, c: int) -> tuple[np.ndarray, float]:
        m = int(SQ * 0.10)
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

    # --------------------------------------------------------------- calibration
    def calibrate(self, frame: np.ndarray, flipped: bool) -> tuple[bool, str]:
        """Learn piece templates from a screenshot showing the START position."""
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

        if any(t.mean() < 0.05 for t in templates.values()) or white - black < 40:
            return False, ("Could not see the pieces clearly. Make sure the board area is selected "
                           "exactly (only the 64 squares), the starting position is shown, and the "
                           "'I play Black' box matches the board orientation.")

        self.templates, self.threshold = templates, (white + black) / 2
        pieces, _ = self.recognize(frame, flipped)
        correct = sum(1 for k, v in START.items() if pieces.get(k) == v)
        extras = sum(1 for k in pieces if k not in START)
        if correct < 28:
            self.templates, self.threshold = {}, None
            return False, f"Calibration check failed ({correct}/32 pieces recognised)."
        return True, f"Calibrated. Self-test: {correct}/32 pieces recognised, {extras} false pieces."

    # --------------------------------------------------------------- recognition
    def recognize(self, frame: np.ndarray, flipped: bool) -> tuple[dict[tuple[int, int], str], int]:
        """Return ({(file, rank): symbol}, number_of_unreadable_squares)."""
        board = self._prepare(frame)
        pieces: dict[tuple[int, int], str] = {}
        unknown = 0
        for r in range(8):
            for c in range(8):
                sil, g = self._features(board, r, c)
                if sil.mean() < EMPTY_AREA:
                    continue
                best_sym, best_iou = "", 0.0
                for sym, t in self.templates.items():
                    union = np.logical_or(sil, t).sum()
                    iou = np.logical_and(sil, t).sum() / union if union else 0.0
                    if iou > best_iou:
                        best_sym, best_iou = sym, iou
                if best_iou < MIN_IOU:
                    unknown += 1
                    continue
                letter = best_sym.lower()
                pieces[square_for(r, c, flipped)] = letter.upper() if g > self.threshold else letter
        return pieces, unknown

    # --------------------------------------------------------------- persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {("w_" if s.isupper() else "b_") + s.lower(): t for s, t in self.templates.items()}
        np.savez_compressed(path, threshold=np.array(self.threshold), **data)

    def load(self, path: Path) -> None:
        z = np.load(path)
        self.threshold = float(z["threshold"])
        self.templates = {}
        for key in z.files:
            if key.startswith("w_"):
                self.templates[key[2:].upper()] = z[key]
            elif key.startswith("b_"):
                self.templates[key[2:]] = z[key]
