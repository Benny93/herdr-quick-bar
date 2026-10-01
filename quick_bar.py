#!/usr/bin/env python3
"""Quick Bar: fzf over running herdr agents, enriched with Claude session data. Enter jumps to the pane."""
import glob, json, os, re, shlex, subprocess, sys, time

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
# imports needed only for building the list are done lazily: the preview re-runs this script on every cursor move
STATE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or __import__("tempfile").gettempdir()
CACHE = os.path.join(STATE, "quick-bar.jsonl")  # one row per line, "pane" first; read by preview and shortcuts
TRANSCRIPTS = os.path.join(STATE, "quick-bar-transcripts.json")  # parsed transcripts keyed by path + mtime/size
PIDS = os.path.join(STATE, "quick-bar-pids.json")  # pane id -> Claude pid, so process-info only runs for new panes
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


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_pids = None


def pane_pid(pane_id):
    """Claude pid running in the pane. herdr calls get slow while the popup opens (~100ms each, served one at a time),
    so reuse the last answer while that process is still alive; a process never changes panes (a moved pane gets a new id)."""
    pid = _pids.get(pane_id)
    if pid and os.path.exists(f"{CLAUDE}/sessions/{pid}.json"):
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    pid = herdr("pane", "process-info", "--pane", pane_id).get("process_info", {}).get("foreground_process_group_id")
    _pids[pane_id] = pid
    return pid


def claude_session(pane_id):
    """Pane -> foreground pid -> ~/.claude/sessions/<pid>.json -> transcript facts."""
    pid = pane_pid(pane_id)
    try:
        with open(f"{CLAUDE}/sessions/{pid}.json") as f:
            meta = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    paths = glob.glob(f"{CLAUDE}/projects/*/{meta.get('sessionId')}.jsonl")
    info = parse_transcript(paths[0]) if paths else {"titles": [], "prompts": [], "branches": [], "prs": [], "cwd": ""}
    return {**info, "name": meta.get("name", ""), "session_id": meta.get("sessionId", "")}


_parsed = None
_parsed_dirty = False


def parse_transcript(path):
    """Cached read_transcript. Transcripts are append-only, so a grown file is read from where we stopped."""
    global _parsed, _parsed_dirty
    if _parsed is None:
        try:
            with open(TRANSCRIPTS) as f:
                _parsed = json.load(f)
        except (OSError, ValueError):
            _parsed = {}
    st = os.stat(path)
    hit = _parsed.get(path)
    if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
        return hit["info"]
    if hit and hit.get("offset", st.st_size + 1) <= st.st_size:
        info, offset = read_transcript(path, hit["offset"], hit["info"])
    else:  # new, or shrunk/replaced: start over
        info, offset = read_transcript(path)
    _parsed[path] = {"mtime": st.st_mtime, "size": st.st_size, "offset": offset, "info": info}
    _parsed_dirty = True
    return info


def save_parsed(prune):
    """Write the transcript cache if anything was re-read. prune drops deleted files (needs a stat per entry,
    so only done when all transcripts were listed anyway)."""
    global _parsed, _parsed_dirty
    if prune and _parsed is not None:
        kept = {p: v for p, v in _parsed.items() if os.path.exists(p)}
        if len(kept) != len(_parsed):
            _parsed = kept
            _parsed_dirty = True
    if _parsed_dirty:
        with open(TRANSCRIPTS + ".tmp", "w") as f:
            json.dump(_parsed, f)
        os.replace(TRANSCRIPTS + ".tmp", TRANSCRIPTS)  # atomic, so a concurrent reader never sees half a file


def read_transcript(path, offset=0, info=None):
    """Titles, prompts, branches, PR links and cwd from a Claude transcript (.jsonl), starting at byte offset and
    extending info. Returns (info, offset after the last complete line); a half-written last line is read next time."""
    info = info or {"titles": [], "prompts": [], "branches": [], "prs": [], "cwd": ""}
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            line = raw.decode(errors="replace")
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
    return info, offset


def screen_lines(pane_id):
    """Visible terminal text of an agent pane, minus box-drawing noise."""
    out = subprocess.run([HERDR, "pane", "read", pane_id, "--source", "visible"],
                         capture_output=True, text=True).stdout
    lines = (re.sub(r"[─-╿▀-▟]+", " ", l) for l in out.splitlines())
    return [one_line(l, 300) for l in lines if l.strip()]


def collect():
    global _pids
    snap = herdr("api", "snapshot").get("snapshot", {})  # agents, workspaces and tabs in one call
    workspaces = {w["workspace_id"]: w["label"] for w in snap.get("workspaces", [])}
    tabs = {t["tab_id"]: t["label"] for t in snap.get("tabs", [])}
    agents = snap.get("agents", [])
    _pids = load_json(PIDS)
    before = dict(_pids)
    # per-pane work (transcripts, screen reads for non-Claude agents) runs in parallel so open time stays flat
    # ponytail: "visible" screen only, scrollback ("recent") is ~40x slower
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(lambda a: row(a, workspaces, tabs), agents))
    live = {a["pane_id"] for a in agents}
    _pids = {p: pid for p, pid in _pids.items() if p in live}  # drop closed panes
    if _pids != before:
        os.makedirs(STATE, exist_ok=True)
        with open(PIDS, "w") as f:
            json.dump(_pids, f)
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
    os.makedirs(STATE, exist_ok=True)
    save_parsed(prune=mode == "all")
    with open(CACHE + ".tmp", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    os.replace(CACHE + ".tmp", CACHE)
    return "\n".join(fzf_line(r) for r in rows)


def cached_row(pane):
    """Decode only the matching line; rows are dumped with "pane" as their first key."""
    prefix = '{"pane": ' + json.dumps(pane) + ","
    try:
        with open(CACHE) as f:
            return next((json.loads(l) for l in f if l.startswith(prefix)), None)
    except OSError:
        return None


def send_prompt(pane):
    """ctrl-t: type a prompt and submit it to the agent without leaving the bar."""
    r = cached_row(pane)
    if not r or r["status"] == "closed":
        return input("Closed sessions can't take prompts; press Enter to resume it instead. ")
    text = input(f"{BOLD}{r['title']}{OFF}\nprompt (empty cancels) › ").strip()
    if text:
        res = subprocess.run([HERDR, "agent", "prompt", pane, text], capture_output=True, text=True)
        if res.returncode:
            input(f"failed: {one_line(res.stderr, 300)}\nEnter to continue ")


def open_pr(pane):
    """ctrl-o: open the session's most recent PR link."""
    r = cached_row(pane)
    if r and r["prs"]:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", r["prs"][-1]], capture_output=True)


def close_pane(pane):
    """ctrl-x: close the agent's pane after confirmation (kills the agent)."""
    r = cached_row(pane)
    if not r or r["status"] == "closed":
        return
    if input(f"Close {r['workspace']} › {r['tab']} ({r['title']})? This stops the agent. [y/N] ").strip().lower() == "y":
        subprocess.run([HERDR, "pane", "close", pane], capture_output=True)


def resume(sid):
    """Reopen a closed Claude session in a new tab, next to running agents in the same folder if any."""
    r = cached_row(f"closed:{sid}")
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
    r = cached_row(pane)
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
    if cmd == "--list":  # no arg: keep the current mode, read from the prompt fzf exports
        return print(build(args[0] if args else "all" if "all" in os.environ.get("FZF_PROMPT", "") else "running"))
    if cmd in ("--send", "--open-pr", "--close"):
        return {"--send": send_prompt, "--open-pr": open_pr, "--close": close_pane}[cmd](args[0])
    if cmd == "--toggle":  # ctrl-r: flip between running agents and running + closed Claude sessions
        to_all = "all" not in os.environ.get("FZF_PROMPT", "")
        return print(f"change-prompt({'all' if to_all else 'agent'} › )+reload({me} --list {'all' if to_all else 'running'})")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr-plugin.toml")) as f:
        version = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
    res = subprocess.run(
        ["fzf", "--border", "top", "--border-label", f" Quick Bar v{version} ", "--border-label-pos", "-2", "--ansi", "--tiebreak", "index", "--delimiter", "\t", "--with-nth", "2,3", "--accept-nth", "1",
         "--no-hscroll", "--layout", "reverse", "--prompt", "agent › ", "--info", "inline",
         "--header-first", "--header",
         "enter jump · ctrl-r closed sessions · ctrl-t send prompt · ctrl-o open PR · ctrl-x close pane · esc quit",
         "--bind", f"ctrl-r:transform:{me} --toggle",
         "--bind", f"ctrl-t:execute({me} --send {{1}})+reload({me} --list)",
         "--bind", f"ctrl-o:execute-silent({me} --open-pr {{1}})",
         "--bind", f"ctrl-x:execute({me} --close {{1}})+reload({me} --list)",
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
