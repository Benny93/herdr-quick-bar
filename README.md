# Quick Bar

A [herdr](https://herdr.dev) plugin that opens a fuzzy-search popup over all running agent sessions and jumps straight to the selected pane.

Lost the terminal where you added a new certificate strategy to the security library? Open Quick Bar, type `security`, hit Enter.

## What it searches

For every agent herdr knows about:

- workspace and tab name, cwd, agent status
- for agents other than Claude Code (Codex, opencode, Copilot, …): the text currently on their screen
- for Claude Code sessions, also: AI-generated session titles (current and earlier), session name, git branches, PR links, and your last 20 prompts

The preview pane shows the session details and highlights the prompts that match your query.

With an empty query, sessions that need you come first: **needs input** (waiting on a question or approval), then **done** (finished, not looked at yet), then idle, then working. Within each group the most recently changed comes first. Once you type, the list is ranked by match quality and this order only breaks ties.

## Requirements

- herdr ≥ 0.9.0
- `python3`
- [`fzf`](https://github.com/junegunn/fzf) ≥ 0.60 (uses `--accept-nth`)

## Install

```sh
herdr plugin link /path/to/herdr-quick-bar
```

Bind a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "benny93.quick-bar.open"
description = "find agent session"
```

Then `herdr server reload-config`.

Or open it without a key: `herdr plugin action invoke benny93.quick-bar.open`.

## Usage

| Key | Action |
| --- | --- |
| type | filter (fuzzy; `'word` for exact, space for AND) |
| ↑ / ↓ | move |
| Enter | focus the agent's pane (closed session: resume it in a new tab) |
| Ctrl-R | toggle closed Claude sessions in the list |
| Ctrl-T | type a prompt and send it to the selected agent without jumping |
| Ctrl-O | open the session's latest PR in the browser |
| Ctrl-X | close the selected pane (asks y/N first; stops the agent) |
| Esc | close |

## How it works

`herdr agent list` gives the panes. For each Claude pane, the pane's foreground process id maps to `~/.claude/sessions/<pid>.json`, which holds the session id; the transcript at `~/.claude/projects/*/<sessionId>.jsonl` supplies titles, prompts, branches and PR links. `CLAUDE_CONFIG_DIR` is respected.

Closed sessions (Ctrl-R) are every transcript under `~/.claude/projects` whose Claude process is no longer running. Enter opens a new tab in that session's folder, in the workspace that already has an agent there (else the current one), and runs `claude --resume <id>`.
