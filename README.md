# Chess Screen Assistant

Reads a chessboard on your screen, runs Stockfish, and draws the best move as an arrow over the board.

## Get the .exe
1. Create a GitHub repo and push this folder (keep `.github/workflows/build.yml`).
2. Open the **Actions** tab -> *Build Windows EXE* -> wait ~3 min.
3. Download `ChessScreenAssistant-windows` from the run's **Artifacts** (or tag a release: `git tag v1.0 && git push --tags` and it appears under Releases).

Stockfish is downloaded and bundled automatically, so the .exe is self-contained.
(Windows SmartScreen may warn because the exe is unsigned: More info -> Run anyway.)

## Use
1. Put a board on screen showing the **starting position** (analysis board, new game, etc.).
2. **Select board area** - drag over just the 64 squares (no coordinates/borders).
3. Tick **I play Black** if the board is flipped, then **Calibrate**. This learns the piece style from the screen, so any theme works. Done once per theme/board size.
4. **Start**. The best move appears in the window and as a green arrow over the board.

Needs Windows 10 2004+ so the arrow overlay is excluded from screen capture.

## Run from source
    pip install -r requirements.txt
    python app.py     # put stockfish.exe in ./engine/ or use "Choose Stockfish .exe"

## Fair play
Intended for analysis, studying, and playing bots/offline. Using an engine in rated games
against humans breaks the rules of Lichess, Chess.com and others and gets accounts banned.
