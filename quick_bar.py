#!/usr/bin/env python3
"""Quick Bar: fzf over running herdr agents, enriched with Claude session data. Enter jumps to the pane."""
import glob, json, os, re, shlex, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
CACHE = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR", tempfile.gettempdir()), "quick-bar.json")
DIM, BOLD, CYAN, YELLOW, OFF = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"
# blocked = waiting on a question/approval, done = finished but not looked at yet; both need you
PRIORITY = {"blocked": 0, "done": 1, "idle": 2, "unknown": 3, "working": 4}
BADGE = {"blocked": "\033[1;31m● needs input\033[0m", "done": "\033[1;33m● done\033[0m",
         "working": "\033[32m… working\033[0m"}


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
    paths = glob.glob(f"{CLAUDE}/projects/*/{meta.get('sessionId')}.jsonl")
    info = parse_transcript(paths[0]) if paths else {"titles": [], "prompts": [], "branches": [], "prs": [], "cwd": ""}
    return {**info, "name": meta.get("name", ""), "session_id": meta.get("sessionId", "")}


def parse_transcript(path):
    """Titles, prompts, branches, PR links and cwd from one Claude transcript (.jsonl)."""
    info = {"titles": [], "prompts": [], "branches": [], "prs": [], "cwd": ""}
    with open(path, errors="replace") as f:
        for line in f:
            # ponytail: full transcript scan, ~5s for all ~800MB of history; cache by mtime if it gets slow
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
            info["cwd"] = info["cwd"] or e.get("cwd", "")
    for k in ("titles", "prs", "branches"):
        info[k] = list(dict.fromkeys(info[k]))  # dedupe, keep order
    info["prompts"] = info["prompts"][-100:]
    return info


def screen_lines(pane_id):
    """Visible terminal text of an agent pane, minus box-drawing noise."""
    out = subprocess.run([HERDR, "pane", "read", pane_id, "--source", "visible"],
                         capture_output=True, text=True).stdout
    lines = (re.sub(r"[─-╿▀-▟]+", " ", l) for l in out.splitlines())
    return [one_line(l, 300) for l in lines if l.strip()]


def collect():
    workspaces = {w["workspace_id"]: w["label"] for w in herdr("workspace", "list").get("workspaces", [])}
    tabs = {t["tab_id"]: t["label"] for t in herdr("tab", "list").get("tabs", [])}
    agents = herdr("agent", "list").get("agents", [])
    # herdr calls take ~10-80ms each; run per-pane work in parallel so open time stays flat
    # ponytail: "visible" screen only, scrollback ("recent") is ~40x slower
    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(lambda a: row(a, workspaces, tabs), agents))
    # needs-you first, then most recent state change; with a query fzf ranks by match and uses this order for ties
    return sorted(rows, key=lambda r: (PRIORITY.get(r["status"], 3), -r["seq"]))


def row(a, workspaces, tabs):
    s = claude_session(a["pane_id"]) if a.get("agent") == "claude" else {}
    return {
        "pane": a["pane_id"],
        "agent": a.get("agent", ""),
        "status": a.get("agent_status", ""),
        "seq": a.get("state_change_seq", 0),
        "workspace": workspaces.get(a.get("workspace_id"), ""),
        "tab": tabs.get(a.get("tab_id"), ""),
        "cwd": (a.get("foreground_cwd") or a.get("cwd") or "").replace(os.path.expanduser("~"), "~"),
        "title": (s.get("titles") or [a.get("terminal_title_stripped", "")])[-1],
        **{k: s.get(k, []) for k in ("titles", "prompts", "branches", "prs")},
        "name": s.get("name", ""),
        "session_id": s.get("session_id", ""),
        # Claude rows already carry titles/prompts; screen text there is mostly noise that drowns fuzzy matching
        "screen": [] if s else screen_lines(a["pane_id"]),
    }


def live_session_ids():
    """Session ids of Claude processes still running anywhere (herdr or not) - never offer those for resume."""
    ids = set()
    for p in glob.glob(f"{CLAUDE}/sessions/*.json"):
        try:
            with open(p) as f:
                meta = json.load(f)
            os.kill(meta["pid"], 0)
            ids.add(meta["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ids


def ago(ts):
    d = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"


def closed_rows(skip_ids):
    paths = sorted(glob.glob(f"{CLAUDE}/projects/*/*.jsonl"), key=os.path.getmtime, reverse=True)
    rows = []
    for path in paths:
        sid = os.path.basename(path)[:-len(".jsonl")]
        if sid in skip_ids:
            continue
        info = parse_transcript(path)
        if not info["prompts"]:
            continue  # empty or tool-only sessions are not worth resuming
        rows.append({
            "pane": f"closed:{sid}", "agent": "claude", "status": "closed", "workspace": "", "tab": "",
            "cwd": info["cwd"].replace(os.path.expanduser("~"), "~"),
            "title": (info["titles"] or info["prompts"][:1])[-1][:80],
            **{k: info[k] for k in ("titles", "prompts", "branches", "prs")},
            "name": "", "session_id": sid, "screen": [], "ago": ago(os.path.getmtime(path)), "real_cwd": info["cwd"],
        })
    return rows


def build(mode):
    """Rows for fzf ("all" adds closed Claude sessions); also written to CACHE for the preview."""
    rows = collect()
    if mode == "all":
        rows += closed_rows(live_session_ids() | {r["session_id"] for r in rows})
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(rows, f)
    return "\n".join(fzf_line(r) for r in rows)


def resume(sid):
    """Reopen a closed Claude session in a new tab, next to running agents in the same folder if any."""
    with open(CACHE) as f:
        r = next(x for x in json.load(f) if x["pane"] == f"closed:{sid}")
    cwd = r["real_cwd"]
    near = [a for a in herdr("agent", "list").get("agents", []) if cwd and a.get("cwd") == cwd]
    ws = near[0]["workspace_id"] if near else os.environ.get("HERDR_WORKSPACE_ID")
    args = ["tab", "create", "--focus", "--label", r["title"][:30]]
    args += ["--workspace", ws] if ws else []
    args += ["--cwd", cwd] if os.path.isdir(cwd) else []
    pane = herdr(*args).get("root_pane", {}).get("pane_id")
    if pane:
        subprocess.run([HERDR, "pane", "run", pane, f"claude --resume {shlex.quote(sid)}"], capture_output=True)


def fzf_line(r):
    if r["status"] == "closed":
        shown = f"{DIM}closed {r['ago']}{OFF} › {CYAN}{r['title']}{OFF}  {DIM}{r['cwd']}{OFF}"
        return f"{r['pane']}\t{shown}\t{DIM}{one_line(' '.join([*r['branches'], *r['prs'], *r['titles'][:-1], *r['prompts'][-20:]]), 4000)}{OFF}"
    shown = f"{BOLD}{r['workspace']}{OFF} › {r['tab']}  {CYAN}{r['title']}{OFF}  {DIM}{r['cwd']}  [{r['agent']}]{OFF} {BADGE.get(r['status'], DIM + r['status'] + OFF)}"
    # hidden-ish tail: searchable, cut off by the screen width; the preview shows the matching part
    extra = " ".join([r["name"], *r["branches"], *r["prs"], *r["titles"][:-1], *r["prompts"][-20:]])
    return f"{r['pane']}\t{shown}\t{DIM}{one_line(extra, 4000)} {one_line(' '.join(r['screen']), 8000)}{OFF}"


def preview(pane, query):
    with open(CACHE) as f:
        r = next((x for x in json.load(f) if x["pane"] == pane), None)
    if not r:
        return
    where = f"closed {r['ago']} · enter resumes it in a new tab" if r["status"] == "closed" else f"{r['workspace']} › {r['tab']}   ({r['agent']}, {r['status']})"
    print(f"{BOLD}{r['title']}{OFF}\n{where}\n{r['cwd']}")
    for label, key in (("session", "name"), ("branch", "branches"), ("PR", "prs")):
        v = r[key] if isinstance(r[key], str) else ", ".join(r[key])
        if v:
            print(f"{DIM}{label}:{OFF} {v}")
    if len(r["titles"]) > 1:
        print(f"{DIM}earlier titles:{OFF} " + " · ".join(r["titles"][:-1]))
    terms = [t.lstrip("'^!").rstrip("$").lower() for t in query.split() if t.lstrip("'^!").rstrip("$")]
    def show(label, items, fallback):
        hits = [p for p in items if any(t in p.lower() for t in terms)] if terms else []
        picked = hits[-10:] or fallback
        if not picked:
            return
        print(f"\n{DIM}{label + ' matching query' if hits else 'recent ' + label}:{OFF}")
        for p in picked:
            for t in terms:
                p = re.sub(re.escape(t), lambda m: f"{YELLOW}{m.group(0)}{OFF}", p, flags=re.I)
            print(f"› {p}")

    show("prompts", r["prompts"], r["prompts"][-8:])
    show("screen", r["screen"], r["screen"][-8:])


def main():
    me = f"python3 {shlex.quote(os.path.abspath(__file__))}"
    cmd, args = (sys.argv[1:2] or [""])[0], sys.argv[2:]
    if cmd == "--preview":
        return preview(args[0], args[1] if len(args) > 1 else "")
    if cmd == "--list":
        return print(build(args[0] if args else ""))
    if cmd == "--toggle":  # ctrl-r: flip between running agents and running + closed Claude sessions
        to_all = "all" not in os.environ.get("FZF_PROMPT", "")
        return print(f"change-prompt({'all' if to_all else 'agent'} › )+reload({me} --list {'all' if to_all else 'running'})")
    res = subprocess.run(
        ["fzf", "--ansi", "--tiebreak", "index", "--delimiter", "\t", "--with-nth", "2,3", "--accept-nth", "1",
         "--no-hscroll", "--layout", "reverse", "--prompt", "agent › ", "--info", "inline",
         "--header", "search titles, prompts, screen, branch, PR, path · enter: jump · ctrl-r: include closed sessions · esc: close",
         "--bind", f"ctrl-r:transform:{me} --toggle",
         "--preview", f"{me} --preview {{1}} {{q}}", "--preview-window", "down,45%,wrap"],
        input=build("running"), stdout=subprocess.PIPE, text=True,
    )
    pane = res.stdout.split("\t")[0].strip()
    if pane.startswith("closed:"):
        resume(pane[len("closed:"):])
    elif pane:
        subprocess.run([HERDR, "agent", "focus", pane], capture_output=True)


if __name__ == "__main__":
    main()
