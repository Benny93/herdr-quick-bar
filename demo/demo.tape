# Records demo/quick-bar.gif with mock data only (demo/herdr). Run from the repo root: vhs demo/demo.tape
Output demo/quick-bar.gif
Require python3
Require fzf

Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 720
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 120ms

Hide
Type `export D=$(mktemp -d) HERDR_BIN_PATH=$PWD/demo/herdr; export CLAUDE_CONFIG_DIR=$D/claude HERDR_PLUGIN_STATE_DIR=$D/state; demo/herdr --fixtures $D/claude; clear` Enter
Type "python3 quick_bar.py" Enter
Sleep 1.5s
Show

# sessions that need you come first; walk the list
Sleep 2s
Down
Sleep 1s
Down
Sleep 1.5s
Up 2

# find the session by what you remember
Type "certificate"
Sleep 3s

# Ctrl-R adds closed sessions to the same search
Ctrl+R
Sleep 2.5s
Down
Sleep 3s

Hide
Escape
