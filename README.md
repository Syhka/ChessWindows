<p align="center">
  <img src="assets/icon.png" width="120" alt="Chess Screen Assistant icon">
</p>

<h1 align="center">Chess Screen Assistant</h1>

<p align="center">
  See what Stockfish thinks of any chess position on your screen, drawn as arrows right on top of the board.<br>
  <b>Windows only · free · no account needed</b>
</p>

---

## What does it do?

You point it at a chessboard on your screen. It reads the position, asks **Stockfish** (one of the strongest chess engines in the world) for the best move, and draws green arrows on the board. It also shows:

- the best move in plain text (like `Nf3`) and how good it is for you,
- an **eval bar** showing who is winning,
- up to **3 candidate moves** (green, yellow, orange),
- a **Copy FEN** button so you can paste the position into any analysis board.

It works with any board theme, because it learns what your pieces look like when you set it up.

> ### Please play fair
> This is made for **analysis, puzzles, practice against bots, and reviewing your own games.**
> Using engine help in rated or online games against real people is cheating. It breaks the rules of every chess site and ruins the game for your opponent. Don't do that.

---

## Getting started (2 minutes)

1. **Download** `ChessScreenAssistant-windows.zip` from the **Releases** page, unzip it anywhere, and open `ChessScreenAssistant.exe`.
   *(There's also a single-file `ChessScreenAssistant-portable.exe`. It works the same, but starts slower and antivirus programs complain about it more often.)*
2. Open a chess board somewhere on your screen, with the **starting position** showing.
3. In the app, click the three steps in order:

| Step | What to do |
|---|---|
| **1. Select board area** | Your screen dims. Drag a box over the board, from the top-left corner to the bottom-right. It doesn't have to be perfect. The app finds the exact edges itself. |
| **2. Calibrate** | The app looks at the starting position and learns your pieces. You only do this once per board look. If you play Black and the board is flipped, tick **I play Black** first. |
| **3. Start** | Arrows appear on your board. Press **F8** any time, even in another window, to start or stop. |

That's it. Make a move, and the arrows update by themselves.

---

## Settings

| Setting | What it does |
|---|---|
| **I play Black** | Tick this if your side is at the top of the screen, so the board is flipped. |
| **Also suggest moves for the opponent's turn** | By default it only helps on *your* turn. Turn this on to see moves for both sides. Handy for analysis. |
| **Think time** | How long Stockfish thinks per move. Longer means stronger advice. `0.6` is a good start. |
| **Top moves** | Show 1, 2 or 3 arrows. |
| **Strength (0–20)** | 20 is full power. Lower it to play against something more human. |

---

## Something not working?

| What you see | What to try |
|---|---|
| **"Can't read the board"** | Make sure the whole board is visible and nothing (a popup, your mouse, a chat window) covers it. Check the **I play Black** box matches your board, then select the area and calibrate again. |
| **Calibration fails** | Calibrate only when the **starting position** is on screen, with all 32 pieces in place. |
| **Arrows are in the wrong place** | Re-do step 1 and step 2. If you moved or resized the browser window, the board area has changed. |
| **"Position looks illegal"** | A piece was misread, often during an animation. It usually fixes itself on the next move. If it keeps happening, recalibrate. |
| **Stockfish stops or the app says the engine crashed** | The app restarts it automatically and switches to a slower, more compatible Stockfish if your CPU is older. If it keeps happening, your antivirus may be deleting the engine. Use the zip (folder) version, or add `%USERPROFILE%\.chess_screen_assistant` to your antivirus exclusions. |

Still stuck? Click **Logs** in the app to open the log folder. The file `app.log` explains what went wrong. Include it if you report a problem.

You can also run `ChessScreenAssistant.exe --selftest` to check that every bundled engine works. The result is saved in `selftest.txt`.

---

## For developers

```
pip install -r requirements-dev.txt
python -m pytest
python app.py
```

How it works, briefly:

- `chessvision.py` finds the board in your selection, learns piece shapes from the starting position, and reads the board.
- `logic.py` turns the reading into a legal position and follows moves, forgiving a misread square or two.
- `engines.py` finds, starts and tests Stockfish, with fallbacks.
- `worker.py` is the background loop (screen → board → Stockfish → arrows) with a watchdog and automatic engine restarts.
- `app.py` is the window and the arrow overlay.

The GitHub workflow in `.github/workflows/build.yml` runs the tests, downloads a fast and a compatible Stockfish, builds the folder and portable exes, self-tests the built engines, and publishes a release when you push a `v*` tag.

To regenerate the icon: `python assets/make_icon.py`.

---

## License

This project bundles **Stockfish** (GPLv3, <https://github.com/official-stockfish/Stockfish>). Its license text is included as `engine/STOCKFISH-COPYING.txt`.
