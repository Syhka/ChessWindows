"""Pure chess helpers (no GUI, no screen capture) so they can be unit-tested anywhere."""
from __future__ import annotations

import math

import chess
import chess.engine

from chessvision import START


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


def engine_safe(board: chess.Board) -> bool:
    """Stockfish can assert/crash on positions that python-chess still calls 'valid'.

    The classic one is two adjacent kings; also require exactly one king per side and a
    side-not-to-move that is not in check (otherwise the king could be 'captured').
    """
    if not board.is_valid():
        return False
    wk, bk = board.king(chess.WHITE), board.king(chess.BLACK)
    if wk is None or bk is None or chess.square_distance(wk, bk) <= 1:
        return False
    if board.is_game_over(claim_draw=False) and not board.legal_moves:
        return True  # checkmate/stalemate: harmless, the worker never sends it to the engine
    return True


def _diff(a: dict[int, chess.Piece], b: dict[int, chess.Piece]) -> int:
    keys = set(a) | set(b)
    return sum(1 for k in keys if a.get(k) != b.get(k))


def _continuation(prev: chess.Board, pm: dict[int, chess.Piece], max_diff: int) -> chess.Board | None:
    """Find the position reachable from `prev` (0, 1 or 2 plies) that best explains the reading.

    A noisy frame (a square misread, an animation in progress) then does not wreck the game state:
    we keep following legal moves instead of rebuilding from scratch.
    """
    best: tuple[int, int, chess.Board] | None = None  # (diff, plies, board)

    def consider(b: chess.Board, plies: int) -> None:
        nonlocal best
        d = _diff(b.piece_map(), pm)
        if d <= max_diff and (best is None or (d, plies) < (best[0], best[1])):
            best = (d, plies, b)

    consider(prev, 0)
    for m1 in list(prev.legal_moves):
        b1 = prev.copy(stack=False)
        b1.push(m1)
        consider(b1, 1)
        for m2 in list(b1.legal_moves):
            b2 = b1.copy(stack=False)
            b2.push(m2)
            consider(b2, 2)
    return best[2] if best else None


def build_board(pieces: dict[tuple[int, int], str], prev: chess.Board | None, default_turn: bool,
                tolerance: int = 1) -> chess.Board:
    """Prefer continuing the previous game (keeps turn / castling / en passant exact)."""
    pm = to_piece_map(pieces)
    if prev is not None:
        if pm == prev.piece_map():
            return prev
        for mv in prev.legal_moves:  # exact one-move continuation (fast path)
            nb = prev.copy(stack=False)
            nb.push(mv)
            if nb.piece_map() == pm:
                return nb
        if tolerance > 0:
            nb = _continuation(prev, pm, tolerance)
            if nb is not None:
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


def white_win_percent(score: chess.engine.PovScore) -> float:
    """White's winning chances 0..100 (same logistic curve Lichess uses). Drives the eval bar."""
    sc = score.white()
    if sc.is_mate():
        return 100.0 if (sc.mate() or 0) > 0 else 0.0
    cp = max(-2000, min(2000, sc.score()))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)
