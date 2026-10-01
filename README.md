# Quick Bar

A [herdr](https://herdr.dev) plugin that opens a fuzzy-search popup over all running agent sessions and jumps straight to the selected pane.

Lost the terminal where you added a new certificate strategy to the security library? Open Quick Bar, type `security`, hit Enter.

## What it searches

For every agent herdr knows about:

- workspace and tab name, cwd, agent status
- for agents other than Claude Code (Codex, opencode, Copilot, …): the text currently on their screen
- for Claude Code sessions, also: AI-generated session titles (current and earlier), session name, git branches, PR links, and your last 20 prompts

The preview pane shows the session details and highlights the prompts that match your query.

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
| Enter | focus the agent's pane |
| Esc | close |

## How it works

`herdr agent list` gives the panes. For each Claude pane, the pane's foreground process id maps to `~/.claude/sessions/<pid>.json`, which holds the session id; the transcript at `~/.claude/projects/*/<sessionId>.jsonl` supplies titles, prompts, branches and PR links. `CLAUDE_CONFIG_DIR` is respected.

Only running sessions are listed.
