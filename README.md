# Chess Screen Assistant

Reads a chessboard from your screen (any theme, it learns the pieces from the start position) and
shows Stockfish's best move(s) as arrows on top of the board. Windows only.

**Fair play:** use it for analysis, puzzles, practice against bots and reviewing games - not in rated
or online games where engine assistance is forbidden.

## Use
1. Download `ChessScreenAssistant-windows.zip` from the Releases page (or the Actions artifact), unzip, run the exe.
   (`ChessScreenAssistant-portable.exe` is a single file, but starts slower and is flagged by antivirus more often.)
2. **Select board area** - drag over exactly the 64 squares.
3. **Calibrate** with the starting position on screen (tick *I play Black* if the board is flipped).
4. **Start** (or press **F8** anywhere).

Extras: top 1-3 moves as coloured arrows, eval bar, *Copy FEN* for any analysis board, adjustable strength/think time.

## What changed in this version
- **Crash fixes:** engine runs with conservative threads/hash, a watchdog kills a hung engine, crashed engines restart
  automatically (fast build -> compatible build), and positions Stockfish can choke on (e.g. adjacent kings) are never sent to it.
- **Better board reading:** the board edges are found automatically inside your selection (no pixel-exact drag needed),
  piece matching tolerates small misalignment, and one or two unreadable squares are filled in from the game so far.
- **New dark UI** and an app icon (`assets/icon.ico`, regenerate with `python assets/make_icon.py`).

## Troubleshooting
Click **Open log folder**. `app.log` has the details, `crash.log` captures hard crashes, `selftest.txt` the engine check.
You can run `ChessScreenAssistant.exe --selftest` to test every bundled engine.

| Symptom | Cause / fix |
|---|---|
| "Worker crashed" / worker stops | See `app.log`. The engine is now restarted automatically and a slower, compatible Stockfish build is used as fallback if the AVX2 one cannot run on your CPU. |
| Engine disappears right after start | Antivirus removed the extracted engine. Use the zip (folder) version, or add `%USERPROFILE%\.chess_screen_assistant` to exclusions. |
| "Can't read the board" | Board area not exact, board partly covered, or wrong *I play Black* setting - reselect and recalibrate. |

## Develop
```
pip install -r requirements-dev.txt
python -m pytest
python app.py
```
CI (`.github/workflows/build.yml`): tests -> download Stockfish (AVX2 + compatible) -> benchmark both ->
build folder + portable exe -> run `--selftest` on the *built* exes -> publish (tags `v*` create a release).

## Licence note
The release bundles Stockfish (GPLv3, https://github.com/official-stockfish/Stockfish); its license text is
included as `engine/STOCKFISH-COPYING.txt`.
