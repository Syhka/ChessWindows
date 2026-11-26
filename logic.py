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


def build_board(pieces: dict[tuple[int, int], str], prev: chess.Board | None, default_turn: bool) -> chess.Board:
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


def white_win_percent(score: chess.engine.PovScore) -> float:
    """White's winning chances 0..100 (same logistic curve Lichess uses). Drives the eval bar."""
    sc = score.white()
    if sc.is_mate():
        return 100.0 if (sc.mate() or 0) > 0 else 0.0
    cp = max(-2000, min(2000, sc.score()))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)
