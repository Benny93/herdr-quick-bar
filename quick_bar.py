#!/usr/bin/env python3
"""Quick Bar: fzf over running herdr agents, enriched with Claude session data. Enter jumps to the pane."""
import glob, json, os, re, shlex, subprocess, sys, tempfile

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
CACHE = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR", tempfile.gettempdir()), "quick-bar.json")
DIM, BOLD, CYAN, YELLOW, OFF = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


def herdr(*args):
    out = subprocess.run([HERDR, *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)["result"]
    except (ValueError, KeyError):
        return {}


def one_line(s, n):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def claude_session(pane_id):
    """Pane -> foreground pid -> ~/.claude/sessions/<pid>.json -> transcript facts."""
    pid = herdr("pane", "process-info", "--pane", pane_id).get("process_info", {}).get("foreground_process_group_id")
    try:
        with open(f"{CLAUDE}/sessions/{pid}.json") as f:
            meta = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    info = {"name": meta.get("name", ""), "titles": [], "prompts": [], "branches": [], "prs": []}
    paths = glob.glob(f"{CLAUDE}/projects/*/{meta.get('sessionId')}.jsonl")
    if not paths:
        return info
    with open(paths[0], errors="replace") as f:
        for line in f:
            # ponytail: full transcript scan per open; fine for hundreds of MB total, cache by mtime if it gets slow
            if '"ai-title"' not in line and '"pr-link"' not in line and '"type":"user"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "ai-title":
                info["titles"].append(e.get("aiTitle", ""))
            elif t == "pr-link":
                info["prs"].append(e.get("prUrl", ""))
            elif t == "user" and not e.get("isMeta"):
                c = e.get("message", {}).get("content")
                if isinstance(c, str) and not c.startswith("<"):  # skip slash-command/system wrappers
                    info["prompts"].append(one_line(c, 300))
            b = e.get("gitBranch")
            if b:
                info["branches"].append(b)
    for k in ("titles", "prs", "branches"):
        info[k] = list(dict.fromkeys(info[k]))  # dedupe, keep order
    return info


def collect():
    workspaces = {w["workspace_id"]: w["label"] for w in herdr("workspace", "list").get("workspaces", [])}
    tabs = {t["tab_id"]: t["label"] for t in herdr("tab", "list").get("tabs", [])}
    rows = []
    for a in herdr("agent", "list").get("agents", []):
        s = claude_session(a["pane_id"]) if a.get("agent") == "claude" else {}
        rows.append({
            "pane": a["pane_id"],
            "agent": a.get("agent", ""),
            "status": a.get("agent_status", ""),
            "workspace": workspaces.get(a.get("workspace_id"), ""),
            "tab": tabs.get(a.get("tab_id"), ""),
            "cwd": (a.get("foreground_cwd") or a.get("cwd") or "").replace(os.path.expanduser("~"), "~"),
            "title": (s.get("titles") or [a.get("terminal_title_stripped", "")])[-1],
            **{k: s.get(k, []) for k in ("titles", "prompts", "branches", "prs")},
            "name": s.get("name", ""),
        })
    return rows


def fzf_line(r):
    shown = f"{BOLD}{r['workspace']}{OFF} › {r['tab']}  {CYAN}{r['title']}{OFF}  {DIM}{r['cwd']}  [{r['agent']}:{r['status']}]{OFF}"
    # hidden-ish tail: searchable, cut off by the screen width; the preview shows the matching part
    extra = " ".join([r["name"], *r["branches"], *r["prs"], *r["titles"][:-1], *r["prompts"][-20:]])
    return f"{r['pane']}\t{shown}\t{DIM}{one_line(extra, 4000)}{OFF}"


def preview(pane, query):
    with open(CACHE) as f:
        r = next((x for x in json.load(f) if x["pane"] == pane), None)
    if not r:
        return
    print(f"{BOLD}{r['title']}{OFF}\n{r['workspace']} › {r['tab']}   ({r['agent']}, {r['status']})\n{r['cwd']}")
    for label, key in (("session", "name"), ("branch", "branches"), ("PR", "prs")):
        v = r[key] if isinstance(r[key], str) else ", ".join(r[key])
        if v:
            print(f"{DIM}{label}:{OFF} {v}")
    if len(r["titles"]) > 1:
        print(f"{DIM}earlier titles:{OFF} " + " · ".join(r["titles"][:-1]))
    terms = [t.lstrip("'^!").rstrip("$").lower() for t in query.split() if t.lstrip("'^!").rstrip("$")]
    hits = [p for p in r["prompts"] if any(t in p.lower() for t in terms)] if terms else []
    print(f"\n{DIM}{'prompts matching query' if hits else 'recent prompts'}:{OFF}")
    for p in hits or r["prompts"][-8:]:
        for t in terms:
            p = re.sub(re.escape(t), lambda m: f"{YELLOW}{m.group(0)}{OFF}", p, flags=re.I)
        print(f"› {p}")


def main():
    if sys.argv[1:2] == ["--preview"]:
        return preview(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    rows = collect()
    if not rows:
        print("No running agents found. Press Enter.")
        return input()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(rows, f)
    me = os.path.abspath(__file__)
    res = subprocess.run(
        ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "2,3", "--accept-nth", "1",
         "--no-hscroll", "--layout", "reverse", "--prompt", "agent › ", "--info", "inline",
         "--header", "type to search titles, prompts, branch, PR, path · enter: jump · esc: close",
         "--preview", f"python3 {shlex.quote(me)} --preview {{1}} {{q}}", "--preview-window", "down,45%,wrap"],
        input="\n".join(fzf_line(r) for r in rows), capture_output=False, stdout=subprocess.PIPE, text=True,
    )
    pane = res.stdout.split("\t")[0].strip()
    if pane:
        subprocess.run([HERDR, "agent", "focus", pane], capture_output=True)


if __name__ == "__main__":
    main()
