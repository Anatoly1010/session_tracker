#!/usr/bin/env python3
"""
Claude session tracker — local web UI over ~/.claude/projects transcripts.

Scans the *.jsonl session transcripts Claude Code writes locally, extracts
session ID / folder / scope (+ timestamps, message counts, model), caches them
in a SQLite index, and serves a small searchable web interface.

Usage:
    python3 session_tracker.py                # serve on http://127.0.0.1:8765
    python3 session_tracker.py --port 9000
    python3 session_tracker.py --scan-only    # rebuild index and exit
    python3 session_tracker.py --projects DIR # override transcript root

No third-party dependencies. Read-only w.r.t. Claude's data.
"""

import argparse
import json
import os
import shlex
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
DEFAULT_PROJECTS = os.path.join(HOME, ".claude", "projects")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")


# --------------------------------------------------------------------------- #
# Transcript parsing
# --------------------------------------------------------------------------- #
def decode_project_dir(name):
    """Fallback folder from an encoded project dir name (lossy: '/','_','.'->'-')."""
    return "/" + name.lstrip("-").replace("-", "/")


def first_text(content):
    """Pull plain text out of a message 'content' (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    return None


def parse_transcript(path, project_dir_name):
    """Return a dict of session-level fields for one .jsonl transcript."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = None
    ai_title = None
    first_user = None
    last_prompt = None
    first_ts = None
    last_ts = None
    n_user = n_assistant = n_lines = 0
    models = set()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not cwd and d.get("cwd"):
                cwd = d["cwd"]

            t = d.get("type")
            if t == "ai-title" and not ai_title:
                ai_title = d.get("aiTitle")
            elif t == "last-prompt" and d.get("lastPrompt"):
                last_prompt = d["lastPrompt"]
            elif t == "user":
                n_user += 1
                if first_user is None:
                    txt = first_text((d.get("message") or {}).get("content"))
                    # skip tool-result / meta user records with no plain text
                    if txt and not txt.startswith("<"):
                        first_user = txt
            elif t == "assistant":
                n_assistant += 1
                m = d.get("message") or {}
                if m.get("model"):
                    models.add(m["model"])

            ts = d.get("timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

    scope = ai_title or (first_user.strip() if first_user else None) or last_prompt or ""
    scope = " ".join(scope.split())  # collapse whitespace/newlines
    folder = cwd or decode_project_dir(project_dir_name)

    st = os.stat(path)
    return {
        "session_id": session_id,
        "folder": folder,
        "scope": scope,
        "ai_title": ai_title or "",
        "first_user": " ".join((first_user or "").split())[:400],
        "last_prompt": " ".join((last_prompt or "").split())[:400],
        "first_ts": first_ts or "",
        "last_ts": last_ts or "",
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_lines": n_lines,
        "models": ",".join(sorted(models)),
        "mtime": st.st_mtime,
        "size": st.st_size,
        "path": path,
    }


# --------------------------------------------------------------------------- #
# SQLite index
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    folder       TEXT,
    scope        TEXT,
    ai_title     TEXT,
    first_user   TEXT,
    last_prompt  TEXT,
    first_ts     TEXT,
    last_ts      TEXT,
    n_user       INTEGER,
    n_assistant  INTEGER,
    n_lines      INTEGER,
    models       TEXT,
    mtime        REAL,
    size         INTEGER,
    path         TEXT
);
-- user-set labels live in their own table so a transcript rescan
-- (INSERT OR REPLACE into sessions) never wipes a rename.
CREATE TABLE IF NOT EXISTS labels (
    session_id   TEXT PRIMARY KEY,
    label        TEXT
);
-- starred sessions, likewise separate so rescans never clear them.
CREATE TABLE IF NOT EXISTS favorites (
    session_id   TEXT PRIMARY KEY,
    created      REAL
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # lightweight migration: add columns missing from an older sessions.db
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    added = False
    for col in ("first_user", "last_prompt"):
        if col not in have:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
            added = True
    if added:
        # new columns are NULL on existing rows and scan() skips unchanged
        # files — clear the session index (never labels) so it repopulates.
        conn.execute("DELETE FROM sessions")
    conn.commit()
    return conn


def scan(conn, projects_dir, verbose=True):
    """Incrementally (re)index transcripts whose mtime/size changed."""
    cur = conn.cursor()
    known = {r["session_id"]: (r["mtime"], r["size"])
             for r in cur.execute("SELECT session_id, mtime, size FROM sessions")}
    seen = set()
    added = updated = 0
    if not os.path.isdir(projects_dir):
        return {"added": 0, "updated": 0, "removed": 0, "total": 0,
                "error": f"projects dir not found: {projects_dir}"}

    for proj in os.listdir(projects_dir):
        pdir = os.path.join(projects_dir, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            sid = os.path.splitext(fn)[0]
            seen.add(sid)
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = known.get(sid)
            if prev and abs(prev[0] - st.st_mtime) < 1e-6 and prev[1] == st.st_size:
                continue  # unchanged
            rec = parse_transcript(path, proj)
            cols = ", ".join(rec.keys())
            ph = ", ".join("?" * len(rec))
            cur.execute(f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({ph})",
                        list(rec.values()))
            if prev:
                updated += 1
            else:
                added += 1

    # drop rows whose transcript disappeared
    removed = 0
    for sid in list(known):
        if sid not in seen:
            cur.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            removed += 1

    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if verbose:
        print(f"[scan] +{added} ~{updated} -{removed}  (total {total})")
    return {"added": added, "updated": updated, "removed": removed, "total": total}


def resume_command(folder, session_id):
    """A copy-paste 'cd + claude --resume' line for the host OS/shell."""
    if os.name == "nt":  # Windows cmd.exe: /d handles a drive change, quote path
        return f'cd /d "{folder}" && claude --resume {session_id}'
    return f"cd {shlex.quote(folder)} && claude --resume {session_id}"


def query_sessions(conn, q="", folder=""):
    sql = ("SELECT s.*, l.label AS label, "
           "(fv.session_id IS NOT NULL) AS favorite "
           "FROM sessions s LEFT JOIN labels l USING (session_id) "
           "LEFT JOIN favorites fv USING (session_id) WHERE 1=1")
    args = []
    if q:
        sql += (" AND (s.scope LIKE ? OR s.session_id LIKE ? OR s.folder LIKE ? "
                "OR l.label LIKE ? OR s.first_user LIKE ?)")
        like = f"%{q}%"
        args += [like] * 5
    if folder:
        sql += " AND s.folder = ?"
        args.append(folder)
    sql += " ORDER BY s.last_ts DESC"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["resume_cmd"] = resume_command(d["folder"], d["session_id"])
        out.append(d)
    return out


def set_label(conn, session_id, label):
    """Upsert a rename; an empty/blank label clears it."""
    label = (label or "").strip()
    if label:
        conn.execute("INSERT INTO labels (session_id, label) VALUES (?, ?) "
                     "ON CONFLICT(session_id) DO UPDATE SET label = excluded.label",
                     (session_id, label))
    else:
        conn.execute("DELETE FROM labels WHERE session_id = ?", (session_id,))
    conn.commit()
    return {"session_id": session_id, "label": label}


def set_favorite(conn, session_id, fav):
    """Star or unstar a session; kept apart from sessions so rescans persist it."""
    if fav:
        conn.execute("INSERT OR IGNORE INTO favorites (session_id, created) "
                     "VALUES (?, ?)", (session_id, time.time()))
    else:
        conn.execute("DELETE FROM favorites WHERE session_id = ?", (session_id,))
    conn.commit()
    return {"session_id": session_id, "favorite": bool(fav)}


def folder_list(conn):
    rows = conn.execute(
        "SELECT folder, COUNT(*) n FROM sessions GROUP BY folder ORDER BY n DESC")
    return [{"folder": r["folder"], "n": r["n"]} for r in rows]


# --------------------------------------------------------------------------- #
# Full transcript reconstruction
# --------------------------------------------------------------------------- #
_TXT_CAP = 12000     # per text/thinking block
_TOOL_CAP = 8000     # per tool input / result
_MAX_EVENTS = 6000   # safety cap on a single transcript


def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n… [+{len(s) - n} chars]"


def _result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        c = json.dumps(c, ensure_ascii=False)
    return {"text": _clip(c, _TOOL_CAP), "error": bool(block.get("is_error"))}


def load_transcript(path):
    """Reconstruct an ordered conversation from a .jsonl transcript."""
    records = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # first pass: map tool_use_id -> its result (results arrive as user records)
    results = {}
    for d in records:
        if d.get("type") == "user":
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = _result_text(b)

    events = []
    truncated = False
    for d in records:
        if len(events) >= _MAX_EVENTS:
            truncated = True
            break
        t = d.get("type")
        ts = d.get("timestamp", "")
        if t == "assistant":
            c = (d.get("message") or {}).get("content")
            blocks = []
            if isinstance(c, str) and c.strip():
                blocks.append({"t": "text", "text": _clip(c, _TXT_CAP)})
            elif isinstance(c, list):
                for b in c:
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        blocks.append({"t": "text", "text": _clip(b["text"], _TXT_CAP)})
                    elif bt == "thinking" and (b.get("thinking") or b.get("text")):
                        blocks.append({"t": "thinking",
                                       "text": _clip(b.get("thinking") or b.get("text"), _TXT_CAP)})
                    elif bt == "tool_use":
                        blocks.append({
                            "t": "tool", "name": b.get("name", "tool"),
                            "input": _clip(json.dumps(b.get("input", {}), indent=2,
                                                      ensure_ascii=False), _TOOL_CAP),
                            "result": results.get(b.get("id"))})
            if blocks:
                events.append({"role": "assistant", "ts": ts, "blocks": blocks})
        elif t == "user":
            c = (d.get("message") or {}).get("content")
            text = None
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                parts = [b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(p for p in parts if p)
            # skip injected system-reminder / tool-wrapper user records
            if text and text.strip() and not text.lstrip().startswith("<"):
                events.append({"role": "user", "ts": ts, "text": _clip(text, _TXT_CAP)})

    return {"events": events, "truncated": truncated}


# --------------------------------------------------------------------------- #
# Memory (~/.claude/projects/<enc>/memory/*.md)
# --------------------------------------------------------------------------- #
_FM_RE = __import__("re").compile(r"^---\s*\n(.*?)\n---\s*\n", __import__("re").S)


def _frontmatter(text):
    # Flat key: value scan (indentation ignored) so a nested `metadata: type:`
    # is captured alongside top-level name/description. Good enough for the
    # simple, hand-written memory frontmatter format.
    m = _FM_RE.match(text)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            s = line.strip()
            if ":" in s and not s.startswith("#"):
                k, v = s.split(":", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    fm.setdefault(k.strip(), v)
    return fm


def _safe_name(name):
    if (not name or "/" in name or "\\" in name or ".." in name
            or name.startswith(".") or not name.endswith(".md")):
        raise ValueError("bad memory file name")
    return name


def _mem_dir(projects_dir, enc, create=False):
    if not enc or "/" in enc or "\\" in enc or ".." in enc:
        raise ValueError("bad project id")
    base = os.path.join(projects_dir, enc)
    if not os.path.isdir(base):
        raise ValueError("unknown project")
    mdir = os.path.join(base, "memory")
    if create:
        os.makedirs(mdir, exist_ok=True)
    elif not os.path.isdir(mdir):
        raise ValueError("no memory in this project")
    return mdir


def memory_projects(conn, projects_dir):
    """Projects that have a memory dir, with their human folder name + count."""
    folder_of = {}
    for r in conn.execute("SELECT folder, path FROM sessions"):
        if r["path"]:
            folder_of[os.path.basename(os.path.dirname(r["path"]))] = r["folder"]
    out = []
    if not os.path.isdir(projects_dir):
        return out
    for enc in os.listdir(projects_dir):
        mdir = os.path.join(projects_dir, enc, "memory")
        if not os.path.isdir(mdir):
            continue
        n = sum(1 for f in os.listdir(mdir)
                if f.endswith(".md") and f != "MEMORY.md")
        out.append({"dir": enc,
                    "folder": folder_of.get(enc, decode_project_dir(enc)),
                    "count": n})
    return sorted(out, key=lambda x: x["folder"].lower())


def memory_list(projects_dir, enc):
    mdir = _mem_dir(projects_dir, enc)
    out = []
    for f in os.listdir(mdir):
        if not f.endswith(".md"):
            continue
        p = os.path.join(mdir, f)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        fm = _frontmatter(text)
        st = os.stat(p)
        out.append({"name": f, "is_index": f == "MEMORY.md",
                    "title": fm.get("name") or f[:-3].replace("-", " "),
                    "description": fm.get("description", ""),
                    "type": fm.get("type", ""),
                    "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: (not x["is_index"], x["title"].lower()))
    return out


def memory_read(projects_dir, enc, name):
    mdir = _mem_dir(projects_dir, enc)
    with open(os.path.join(mdir, _safe_name(name)), encoding="utf-8",
              errors="replace") as fh:
        return fh.read()


def memory_import(projects_dir, src_enc, name, dst_enc):
    """Copy one memory file into another project and add its MEMORY.md line."""
    name = _safe_name(name)
    if name == "MEMORY.md":
        raise ValueError("pick an individual memory, not the index")
    if src_enc == dst_enc:
        raise ValueError("source and target are the same project")
    src = os.path.join(_mem_dir(projects_dir, src_enc), name)
    if not os.path.isfile(src):
        raise ValueError("source memory not found")
    dstdir = _mem_dir(projects_dir, dst_enc, create=True)
    dst = os.path.join(dstdir, name)
    if os.path.isfile(dst):
        raise ValueError(f"'{name}' already exists in the target project")
    with open(src, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)
    # append an index line to the target MEMORY.md if not already present
    fm = _frontmatter(text)
    title = fm.get("name") or name[:-3].replace("-", " ")
    desc = fm.get("description", "")
    line = f"- [{title}]({name})" + (f" — {desc}" if desc else "")
    idx = os.path.join(dstdir, "MEMORY.md")
    existing = ""
    if os.path.isfile(idx):
        with open(idx, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    if f"]({name})" not in existing:
        with open(idx, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line + "\n")
    return {"ok": True, "name": name, "title": title, "dst": dst_enc}


def memory_undo_import(projects_dir, dst_enc, name):
    """Reverse an import: delete the copied file and drop its MEMORY.md line."""
    name = _safe_name(name)
    if name == "MEMORY.md":
        raise ValueError("refusing to remove the index")
    mdir = _mem_dir(projects_dir, dst_enc)
    fpath = os.path.join(mdir, name)
    removed = False
    if os.path.isfile(fpath):
        os.remove(fpath)
        removed = True
    idx = os.path.join(mdir, "MEMORY.md")
    if os.path.isfile(idx):
        with open(idx, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        kept = [ln for ln in lines if f"]({name})" not in ln]
        if len(kept) != len(lines):
            with open(idx, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
    return {"ok": True, "removed": removed, "name": name, "dst": dst_enc}


# --------------------------------------------------------------------------- #
# Web server
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Session Tracker</title>
<style>
  :root {
    /* Atomize dark theme (general_modules/gui_style.py -> Theme) */
    --bg:     #2a2a40;   /* window background            */
    --base:   #3f3f61;   /* input / button / tab surface */
    --fg:     #c1cae3;   /* primary text                 */
    --accent: #d3c24e;   /* gold selection / highlight   */
    --accent-fg: #2a2a40;/* text on the gold accent      */
    --line: color-mix(in srgb, var(--bg) 55%, var(--fg));
    --line-soft: color-mix(in srgb, var(--bg) 78%, var(--fg));
    --muted: color-mix(in srgb, var(--fg) 55%, var(--bg));
    --chip: var(--base);
    --hover: #49496b;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.45 system-ui, -apple-system, sans-serif;
         background: var(--bg); color: var(--fg); }
  header { position: sticky; top: 0; z-index: 5; padding: 12px 20px;
           background: color-mix(in srgb, var(--bg) 86%, transparent);
           backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); }
  .top { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  h1 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: -.01em; }
  .seg { display: inline-flex; border: 1px solid var(--line); border-radius: 9px;
         overflow: hidden; }
  .seg button { border: none; border-radius: 0; background: transparent;
         padding: 5px 14px; font-size: 13px; }
  .seg button.on { background: var(--accent); color: var(--accent-fg); font-weight: 600; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input, select, button { font: inherit; padding: 6px 10px; border-radius: 8px;
           border: 1px solid var(--line); background: var(--base); color: var(--fg); }
  input[type=search] { flex: 1 1 260px; min-width: 180px; }
  button { cursor: pointer; }
  button:hover { background: var(--hover); }
  .meta { font-size: 12px; color: var(--muted); margin-left: auto; }
  main { padding: 6px 20px 48px; }

  /* ---- sessions table ---- */
  table { border-collapse: separate; border-spacing: 0; width: 100%; }
  th { text-align: left; padding: 8px 12px; position: sticky; top: 92px;
       background: var(--bg); font-size: 11px; text-transform: uppercase;
       letter-spacing: .05em; color: var(--muted); cursor: pointer;
       user-select: none; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th:hover { color: var(--fg); }
  td { padding: 10px 12px; vertical-align: top; border-bottom: 1px solid var(--line-soft); }
  .srow { cursor: pointer; }
  .srow:hover td { background: var(--hover); }
  .srow.open td { background: color-mix(in srgb, var(--bg) 90%, var(--fg)); }
  .caret { display: inline-block; width: 14px; color: var(--muted);
           transition: transform .12s; }
  .srow.open .caret { transform: rotate(90deg); }
  .label { font-weight: 600; }
  .sub { font-size: 12px; color: var(--muted); margin-top: 1px; }
  .scope { max-width: 560px; }
  .scope .row1 { display: flex; align-items: baseline; gap: 4px; }
  .ren { border: none; background: none; padding: 0 4px; opacity: 0; cursor: pointer;
         color: var(--muted); font-size: 13px; }
  .srow:hover .ren { opacity: .8; }
  .ren:hover { color: var(--fg); }
  .chip { display: inline-block; max-width: 240px; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
          font-family: ui-monospace, monospace; font-size: 12px; padding: 1px 8px;
          border-radius: 6px; background: var(--chip); color: var(--muted); }
  .num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
  .when { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .when .abs { font-size: 11px; color: var(--muted); }
  .idchip { font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted);
            white-space: nowrap; }
  .copy { border: none; background: none; padding: 0 5px; opacity: .5; cursor: pointer;
          color: inherit; }
  .copy:hover { opacity: 1; }
  .star-th { width: 30px; padding-left: 14px; }
  .starcell { padding-left: 12px; padding-right: 0; width: 30px; }
  .star { border: none; background: none; padding: 0 2px; cursor: pointer; line-height: 1;
          font-size: 15px; color: var(--muted); transition: color .12s, transform .08s; }
  .star:hover { color: var(--accent); transform: scale(1.15); }
  .star.on { color: var(--accent); }
  .empty { padding: 48px; text-align: center; color: var(--muted); }

  .detail td { background: color-mix(in srgb, var(--bg) 95%, var(--fg)); padding: 0; }
  .detail-inner { padding: 14px 18px 18px 40px; display: grid;
                  grid-template-columns: max-content 1fr; gap: 6px 16px; font-size: 13px; }
  .detail-inner dt { color: var(--muted); }
  .detail-inner dd { margin: 0; }
  .cmd { font-family: ui-monospace, monospace; font-size: 12px; padding: 5px 9px;
         border-radius: 7px; background: color-mix(in srgb, var(--bg) 80%, var(--fg));
         display: inline-flex; gap: 8px; align-items: center; max-width: 100%; }
  .cmd code { white-space: pre-wrap; word-break: break-all; }
  .prompt { white-space: pre-wrap; max-height: 9em; overflow: auto; padding: 7px 9px;
            border-radius: 7px; background: color-mix(in srgb, var(--bg) 88%, var(--fg)); }

  /* ---- memory cards ---- */
  .cards { display: grid; gap: 12px; margin-top: 10px;
           grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
  .card { border: 1px solid var(--line); border-radius: 12px; padding: 13px 15px;
          display: flex; flex-direction: column; gap: 7px; background: var(--bg); }
  .card.index { border-color: var(--accent);
                background: color-mix(in srgb, var(--bg) 96%, var(--accent)); }
  .card h3 { margin: 0; font-size: 14px; font-weight: 620; display: flex;
             align-items: center; gap: 7px; }
  .badge { font-size: 10px; text-transform: uppercase; letter-spacing: .04em;
           padding: 1px 7px; border-radius: 999px; background: var(--chip);
           color: var(--muted); font-weight: 600; }
  .card p { margin: 0; font-size: 13px; color: color-mix(in srgb, var(--fg) 78%, var(--bg)); }
  .card .cmeta { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .card .actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: auto;
                   padding-top: 4px; align-items: center; }
  .card .actions button, .card .actions select { padding: 4px 9px; font-size: 12px;
                   border-radius: 7px; }
  .card .actions select { max-width: 150px; min-width: 0; }
  .memview { white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 12px;
             margin-top: 4px; padding: 9px; border-radius: 8px;
             background: color-mix(in srgb, var(--bg) 90%, var(--fg));
             max-height: 300px; overflow: auto; }

  .toast { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
           background: color-mix(in srgb, var(--bg) 15%, var(--fg)); color: var(--bg);
           padding: 9px 16px; border-radius: 9px; font-size: 13px; opacity: 0;
           transition: opacity .18s, transform .18s; pointer-events: none; z-index: 20; }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(-4px);
                pointer-events: auto; }
  .toast.err { background: #b3261e; color: #fff; }
  .toast-act { margin-left: 12px; padding: 2px 10px; font-size: 12px; font-weight: 600;
               border-radius: 6px; border: none; cursor: pointer;
               background: var(--accent); color: var(--accent-fg); }

  /* ---- themed scrollbars ---- */
  * { scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--base) 55%, var(--fg)) transparent; }
  ::-webkit-scrollbar { width: 11px; height: 11px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { border-radius: 8px; border: 3px solid var(--bg);
        background: color-mix(in srgb, var(--base) 55%, var(--fg)); background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background: var(--accent); background-clip: padding-box; }
  ::-webkit-scrollbar-corner { background: transparent; }

  /* ---- insights ---- */
  .tiles { display: grid; gap: 12px; margin: 12px 0 4px;
           grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .tile { border: 1px solid var(--line); border-radius: 12px; padding: 13px 16px;
          background: color-mix(in srgb, var(--bg) 92%, var(--fg)); }
  .tile .t-label { font-size: 12px; color: var(--muted); }
  .tile .t-value { font-size: 30px; font-weight: 650; letter-spacing: -.02em; margin-top: 2px; }
  .chart-block { margin: 24px 0; }
  .chart-block h2 { font-size: 14px; font-weight: 620; margin: 0 0 4px; }
  .chart-block .hint { font-weight: 400; color: var(--muted); font-size: 12px; }
  .chart svg { display: block; width: 100%; height: auto; overflow: visible; }
  .chart [data-tip] { transition: opacity .1s; }
  .chart [data-tip]:hover { opacity: .8; cursor: default; }
  .axis { fill: var(--muted); font-size: 10px; }
  .glab { fill: var(--fg); font-size: 11px; }
  .grid { stroke: var(--line-soft); stroke-width: 1; }
  .ctip { position: fixed; z-index: 30; pointer-events: none; white-space: nowrap;
          background: color-mix(in srgb, var(--bg) 12%, var(--fg)); color: var(--bg);
          font-size: 12px; padding: 5px 9px; border-radius: 7px;
          transform: translate(-50%, -130%); opacity: 0; transition: opacity .1s; }
  .ctip.show { opacity: 1; }

  /* ---- transcript modal ---- */
  .linkbtn { padding: 4px 11px; font-size: 12px; border-radius: 7px;
             background: var(--base); }
  .modal { position: fixed; inset: 0; z-index: 40; display: flex;
           align-items: center; justify-content: center; padding: 24px;
           background: color-mix(in srgb, #000 62%, transparent); }
  .modal-card { background: var(--bg); border: 1px solid var(--line);
                border-radius: 14px; width: min(900px, 100%); max-height: 90vh;
                display: flex; flex-direction: column; overflow: hidden; }
  .modal-head { display: flex; align-items: center; gap: 14px; padding: 12px 16px 10px;
                border-bottom: 1px solid var(--line); }
  .modal-title { font-weight: 620; flex: 1; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .modal-sub { padding: 6px 16px; font-size: 12px; color: var(--muted);
               border-bottom: 1px solid var(--line-soft); font-variant-numeric: tabular-nums; }
  .think-toggle { font-size: 12px; color: var(--muted); display: flex;
                  align-items: center; gap: 5px; cursor: pointer; white-space: nowrap; }
  .modal-x { border: none; background: none; font-size: 15px; color: var(--muted);
             cursor: pointer; padding: 2px 6px; }
  .modal-x:hover { color: var(--fg); }
  .modal-body { overflow: auto; padding: 16px 18px; display: flex;
                flex-direction: column; gap: 15px; }
  .msg { display: flex; flex-direction: column; gap: 5px; }
  .msg .who { font-size: 10px; text-transform: uppercase; letter-spacing: .06em;
              color: var(--muted); }
  .msg.user { align-items: flex-start; }
  .bubble { border-radius: 11px; padding: 9px 13px; white-space: pre-wrap;
            word-break: break-word; font-size: 13.5px; line-height: 1.55; }
  .msg.user .bubble { max-width: 85%;
            background: color-mix(in srgb, var(--bg) 74%, var(--accent));
            border: 1px solid color-mix(in srgb, var(--bg) 55%, var(--accent)); }
  .msg.assistant .bubble { background: color-mix(in srgb, var(--bg) 91%, var(--fg)); }
  .think { white-space: pre-wrap; word-break: break-word; font-size: 12.5px;
           color: var(--muted); font-style: italic; border-left: 2px solid var(--line);
           padding: 3px 11px; }
  .modal-body.hide-think .think { display: none; }
  .tool { margin: 1px 0; }
  .tool summary { cursor: pointer; font-size: 12px; color: var(--muted);
                  font-family: ui-monospace, monospace; padding: 2px 0; }
  .tool summary::marker { color: var(--accent); }
  .tool pre { white-space: pre-wrap; word-break: break-word; font-size: 12px;
              font-family: ui-monospace, monospace; margin: 6px 0 0; padding: 8px 10px;
              border-radius: 7px; max-height: 360px; overflow: auto;
              background: color-mix(in srgb, var(--bg) 84%, var(--fg)); }
  .tool pre.err { border-left: 3px solid #d9534f; }
  .tool .rlabel { font-size: 10px; color: var(--muted); margin-top: 6px;
                  text-transform: uppercase; letter-spacing: .05em; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>Claude Session Tracker</h1>
    <div class="seg">
      <button id="tabSessions" class="on" onclick="setView('sessions')">Sessions</button>
      <button id="tabFavorites" onclick="setView('favorites')">★ Favorites</button>
      <button id="tabMemory" onclick="setView('memory')">Memory</button>
      <button id="tabInsights" onclick="setView('insights')">Insights</button>
    </div>
  </div>
  <div class="controls" id="sessCtrls">
    <input id="q" type="search" placeholder="Search scope, label, id, folder…" autofocus>
    <select id="folder"><option value="">All folders</option></select>
    <button id="refresh">↻ Rescan</button>
    <span class="meta" id="meta"></span>
  </div>
  <div class="controls" id="memCtrls" hidden>
    <select id="memProj"></select>
    <input id="memQ" type="search" placeholder="Filter memories…">
    <span class="meta" id="memMeta"></span>
  </div>
  <div class="controls" id="favCtrls" hidden>
    <span class="meta" id="favMeta"></span>
  </div>
</header>

<main>
  <section id="sessionsView">
    <table id="tbl">
      <thead><tr>
        <th class="star-th"></th>
        <th data-k="last_ts">Last activity</th>
        <th data-k="scope">Scope</th>
        <th data-k="folder">Folder</th>
        <th data-k="n_user" class="num">Msgs</th>
        <th data-k="session_id">Session</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty" hidden>No sessions match.</div>
  </section>

  <section id="favoritesView" hidden>
    <table id="favTbl">
      <thead><tr>
        <th class="star-th"></th>
        <th data-k="last_ts">Last activity</th>
        <th data-k="scope">Scope</th>
        <th data-k="folder">Folder</th>
        <th data-k="n_user" class="num">Msgs</th>
        <th data-k="session_id">Session</th>
      </tr></thead>
      <tbody id="favRows"></tbody>
    </table>
    <div class="empty" id="favEmpty" hidden>No favorites yet — star a session with the ☆ on its row.</div>
  </section>

  <section id="memoryView" hidden>
    <div class="cards" id="memCards"></div>
    <div class="empty" id="memEmpty" hidden>No memories here.</div>
  </section>

  <section id="insightsView" hidden>
    <div class="tiles" id="statTiles"></div>
    <div class="chart-block"><h2>Sessions per day</h2>
      <div class="chart" id="chartDaily"></div></div>
    <div class="chart-block"><h2>Top projects <span class="hint">— by session count</span></h2>
      <div class="chart" id="chartProjects"></div></div>
    <div class="chart-block"><h2>Activity <span class="hint">— session starts by weekday × hour, local time</span></h2>
      <div class="chart" id="chartHeat"></div></div>
  </section>
</main>
<div class="toast" id="toast"></div>
<div class="ctip" id="chartTip"></div>

<div class="modal" id="modal" hidden>
  <div class="modal-card">
    <div class="modal-head">
      <div class="modal-title" id="mTitle"></div>
      <label class="think-toggle"><input type="checkbox" id="thinkChk"> thinking</label>
      <button class="modal-x" title="Close (Esc)" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-sub" id="mSub"></div>
    <div class="modal-body hide-think" id="mBody"></div>
  </div>
</div>

<script>
/* ---------- shared helpers ---------- */
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",
                       ">":"&gt;",'"':"&quot;"}[c]));}
function fmt(ts){ if(!ts) return "—"; const d=new Date(ts);
  return isNaN(d)?ts:d.toLocaleString([],{year:"2-digit",month:"short",day:"numeric",
                                          hour:"2-digit",minute:"2-digit"}); }
function rel(ts){ const d=new Date(ts); if(isNaN(d)) return ts||"—";
  const s=(Date.now()-d)/1000;
  if(s<60) return "just now";
  if(s<3600) return Math.round(s/60)+"m ago";
  if(s<86400) return Math.round(s/3600)+"h ago";
  if(s<604800) return Math.round(s/86400)+"d ago";
  return d.toLocaleDateString([],{month:"short",day:"numeric"}); }
function base(p){ const b=(p||"").replace(/\/+$/,"").split("/").pop(); return b||p||"—"; }
function kb(n){ return n<1024?n+" B":(n/1024).toFixed(n<10240?1:0)+" KB"; }
let _tt;
function showToast(msg, o){ o=o||{}; const t=document.getElementById("toast");
  t.innerHTML=""; const s=document.createElement("span"); s.textContent=msg; t.appendChild(s);
  if(o.action){ const b=document.createElement("button"); b.className="toast-act";
    b.textContent=o.action;
    b.onclick=()=>{ t.className="toast"; clearTimeout(_tt); o.onAction&&o.onAction(); };
    t.appendChild(b); }
  t.className="toast show"+(o.err?" err":"");
  clearTimeout(_tt); _tt=setTimeout(()=>t.className="toast", o.action?6500:1900); }
function toast(msg, err){ showToast(msg, {err}); }
function toastAction(msg, action, cb){ showToast(msg, {action, onAction:cb}); }
async function copyText(s){ try{ await navigator.clipboard.writeText(s);
  toast("Copied to clipboard"); }catch(e){ toast("Copy failed",1); } }

/* ---------- view switch ---------- */
function setView(v){
  const map={ sessions:[tabSessions,sessionsView,sessCtrls],
              favorites:[tabFavorites,favoritesView,favCtrls],
              memory:[tabMemory,memoryView,memCtrls],
              insights:[tabInsights,insightsView,null] };
  for(const k in map){ const [tab,view,ctrls]=map[k]; const on=k===v;
    tab.classList.toggle("on",on); view.hidden=!on; if(ctrls) ctrls.hidden=!on; }
  if(v==="favorites") renderFav();
  if(v==="memory" && !memLoaded) loadMemProjects();
  if(v==="insights") loadInsights();
  if(location.hash.slice(1)!==v) history.replaceState(null,"","#"+v);
}
window.addEventListener("hashchange",()=>{ const v=location.hash.slice(1);
  if(["sessions","favorites","memory","insights"].includes(v)) setView(v); });

/* ================= SESSIONS ================= */
let DATA=[], sortK="last_ts", sortAsc=false;
const openRows=new Set();

async function load(){
  const p=new URLSearchParams({q:q.value, folder:folder.value});
  const j=await (await fetch("/api/sessions?"+p)).json();
  DATA=j.sessions; meta.textContent=j.sessions.length+" sessions";
  fillFolders(j.folders); renderAll();
}
function fillFolders(folders){
  if(folder.dataset.filled) return;
  for(const f of folders){ const o=document.createElement("option");
    o.value=f.folder; o.textContent=`${base(f.folder)} (${f.n})`; folder.appendChild(o); }
  folder.dataset.filled="1";
}
function dur(a,b){ const t0=new Date(a),t1=new Date(b);
  if(isNaN(t0)||isNaN(t1)) return "—";
  let s=Math.max(0,(t1-t0)/1000); const h=Math.floor(s/3600); const m=Math.floor((s-h*3600)/60);
  return h?`${h}h ${m}m`:`${m}m`; }
function scopeCell(s){
  const title=s.label||s.scope;
  const main=title?esc(title):"<span style='opacity:.4'>(no scope)</span>";
  const sub=s.label&&s.scope&&s.label!==s.scope?`<div class="sub">${esc(s.scope)}</div>`:"";
  return `<div class="row1"><span class="caret">▶</span><span class="label">${main}</span>
          <button class="ren" title="Rename" onclick="rename(event,'${s.session_id}')">✎</button></div>${sub}`;
}
function detailRow(s){
  const cmd=s.resume_cmd||`cd ${s.folder} && claude --resume ${s.session_id}`;
  return `<tr class="detail"><td colspan="6"><div class="detail-inner">
    <dt>Transcript</dt><dd><button class="linkbtn"
      onclick="event.stopPropagation();openTranscript('${s.session_id}')">View full conversation →</button></dd>
    <dt>Resume</dt><dd><span class="cmd"><code>${esc(cmd)}</code>
      <button class="copy" title="Copy resume command"
        onclick="event.stopPropagation();copyText(${esc(JSON.stringify(cmd))})">⧉</button></span></dd>
    <dt>Session ID</dt><dd class="idchip">${s.session_id}
      <button class="copy" onclick="event.stopPropagation();copyText('${s.session_id}')">⧉</button></dd>
    <dt>Folder</dt><dd class="idchip">${esc(s.folder)}</dd>
    <dt>Started</dt><dd>${fmt(s.first_ts)}</dd>
    <dt>Last</dt><dd>${fmt(s.last_ts)} &nbsp;·&nbsp; ${dur(s.first_ts,s.last_ts)}</dd>
    <dt>Messages</dt><dd>${s.n_user} user / ${s.n_assistant} assistant · ${s.n_lines} records</dd>
    <dt>Model</dt><dd>${esc(s.models)||"—"}</dd>
    ${s.first_user?`<dt>First prompt</dt><dd class="prompt">${esc(s.first_user)}</dd>`:""}
    ${s.last_prompt&&s.last_prompt!==s.first_user?`<dt>Last prompt</dt><dd class="prompt">${esc(s.last_prompt)}</dd>`:""}
  </div></td></tr>`;
}
function rowHtml(s){ const o=openRows.has(s.session_id);
  return `<tr class="srow${o?" open":""}" data-id="${s.session_id}">
    <td class="starcell"><button class="star${s.favorite?" on":""}"
        title="${s.favorite?"Remove from favorites":"Add to favorites"}"
        onclick="event.stopPropagation();toggleFav('${s.session_id}')">${s.favorite?"★":"☆"}</button></td>
    <td class="when">${rel(s.last_ts)}<div class="abs">${fmt(s.last_ts)}</div></td>
    <td class="scope">${scopeCell(s)}</td>
    <td><span class="chip" title="${esc(s.folder)}">${esc(base(s.folder))}</span></td>
    <td class="num">${s.n_user}</td>
    <td class="idchip">${s.session_id.slice(0,8)}…
      <button class="copy" title="Copy full session id"
        onclick="event.stopPropagation();copyText('${s.session_id}')">⧉</button></td>
  </tr>${o?detailRow(s):""}`;
}
function renderRows(list, tbody, emptyEl){
  const d=[...list].sort((a,b)=>{ let x=a[sortK]||"",y=b[sortK]||"";
    if(typeof x!=="number"){x=(""+x).toLowerCase();y=(""+y).toLowerCase();}
    return (x<y?-1:x>y?1:0)*(sortAsc?1:-1); });
  emptyEl.hidden=d.length>0;
  tbody.innerHTML=d.map(rowHtml).join("");
  tbody.querySelectorAll(".srow").forEach(tr=>{ tr.onclick=()=>{
    const id=tr.dataset.id; openRows.has(id)?openRows.delete(id):openRows.add(id); renderAll(); }; });
}
function render(){ renderRows(DATA, rows, empty); }
function renderFav(){ const f=DATA.filter(s=>s.favorite);
  favMeta.textContent = f.length ? `${f.length} favorite${f.length>1?"s":""}` : "";
  renderRows(f, favRows, favEmpty); }
function renderAll(){ render(); renderFav(); }
async function toggleFav(id){
  const s=DATA.find(x=>x.session_id===id); if(!s) return;
  const nv=s.favorite?0:1; s.favorite=nv;
  renderAll();
  try{
    await fetch("/api/favorite",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session_id:id,favorite:nv})});
    toast(nv?"Added to favorites":"Removed from favorites");
  }catch(e){ s.favorite=nv?0:1; renderAll(); toast("Failed to save favorite",1); }
}
async function rename(ev,id){ ev.stopPropagation();
  const s=DATA.find(x=>x.session_id===id);
  const val=prompt("Label for this session (blank to clear):", s.label||"");
  if(val===null) return;
  await fetch("/api/label",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session_id:id,label:val})});
  s.label=val.trim(); renderAll(); toast(s.label?"Renamed":"Label cleared");
}
q.addEventListener("input",()=>{clearTimeout(window._t);window._t=setTimeout(load,180);});
folder.addEventListener("change",load);
refresh.addEventListener("click",async()=>{ refresh.disabled=true; refresh.textContent="…";
  const st=await (await fetch("/api/rescan",{method:"POST"})).json();
  refresh.textContent="↻ Rescan"; refresh.disabled=false;
  toast(`Indexed ${st.total} · +${st.added} ~${st.updated} -${st.removed}`); load(); });
document.querySelectorAll("th[data-k]").forEach(th=>{ th.onclick=()=>{ const k=th.dataset.k;
  if(sortK===k) sortAsc=!sortAsc; else {sortK=k; sortAsc=false;} renderAll(); }; });

/* ================= MEMORY ================= */
let memLoaded=false, MEMPROJ=[], MEMFILES=[], memViewOpen=new Set();

async function loadMemProjects(){
  MEMPROJ=await (await fetch("/api/memory/projects")).json();
  memProj.innerHTML=MEMPROJ.map(p=>
    `<option value="${p.dir}">${esc(base(p.folder))} — ${p.count} memories</option>`).join("");
  memLoaded=true;
  if(MEMPROJ.length) loadMem(MEMPROJ[0].dir);
  else { memMeta.textContent="no memory dirs found"; memCards.innerHTML=""; memEmpty.hidden=false; }
}
async function loadMem(dir){
  memViewOpen.clear();
  MEMFILES=await (await fetch("/api/memory/list?dir="+encodeURIComponent(dir))).json();
  MEMFILES.forEach(f=>f._dir=dir);
  renderMem();
}
function targetOptions(curDir){
  return MEMPROJ.filter(p=>p.dir!==curDir)
    .map(p=>`<option value="${p.dir}">${esc(base(p.folder))}</option>`).join("");
}
function renderMem(){
  const dir=memProj.value;
  const flt=(memQ.value||"").toLowerCase();
  const show=MEMFILES.filter(f=>!flt||
    (f.title+" "+f.description+" "+f.name).toLowerCase().includes(flt));
  memMeta.textContent = flt ? `${show.length} of ${MEMFILES.length} shown` : "";
  memEmpty.hidden=show.length>0;
  memCards.innerHTML=show.map(f=>{
    const opened=memViewOpen.has(f.name);
    const nm=esc(JSON.stringify(f.name)), dr=esc(JSON.stringify(dir));
    const importCtl=f.is_index?"":`<select title="Import into another project"
        onchange="importMem(${dr},${nm},this)">
        <option value="">Import to…</option>${targetOptions(dir)}</select>`;
    return `<div class="card${f.is_index?" index":""}">
      <h3>${esc(f.title)} ${f.is_index?'<span class="badge">index</span>':
             (f.type?`<span class="badge">${esc(f.type)}</span>`:"")}</h3>
      ${f.description?`<p>${esc(f.description)}</p>`:""}
      <div class="cmeta">${kb(f.size)} · ${fmt(new Date(f.mtime*1000).toISOString())} · ${esc(f.name)}</div>
      <div class="actions">
        <button onclick="copyMem(${dr},${nm})">⧉ Copy</button>
        <button onclick="toggleMem(${nm})">${opened?"Hide":"View"}</button>
        ${importCtl}
      </div>
      ${opened?`<div class="memview" id="mv-${cssId(f.name)}">loading…</div>`:""}
    </div>`;
  }).join("");
  show.forEach(f=>{ if(memViewOpen.has(f.name)) fillView(dir,f.name); });
}
function cssId(name){ return name.replace(/[^a-z0-9]/gi,"_"); }
async function memText(dir,name){
  const u=`/api/memory/file?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`;
  const j=await (await fetch(u)).json();
  return j.content!=null ? j.content : (j.error||"");
}
async function copyMem(dir,name){ copyText(await memText(dir,name)); }
async function fillView(dir,name){
  const el=document.getElementById("mv-"+cssId(name)); if(!el) return;
  el.textContent=await memText(dir,name);
}
function toggleMem(name){ memViewOpen.has(name)?memViewOpen.delete(name):memViewOpen.add(name); renderMem(); }
function updateProjCount(dir){
  const p=MEMPROJ.find(x=>x.dir===dir); if(!p) return;
  [...memProj.options].forEach(o=>{ if(o.value===dir)
    o.textContent=`${base(p.folder)} — ${p.count} memories`; });
}
async function importMem(srcDir,name,sel){
  const dstDir=sel.value; sel.selectedIndex=0; if(!dstDir) return;
  const r=await fetch("/api/memory/import",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({src:srcDir,name:name,dst:dstDir})});
  const j=await r.json();
  if(j.ok){ const p=MEMPROJ.find(x=>x.dir===dstDir); if(p)p.count++;
    updateProjCount(dstDir);
    if(memProj.value===dstDir) loadMem(dstDir);
    toastAction(`Imported “${j.title}” → ${base((p||{}).folder||dstDir)}`,
                "Undo", ()=>undoImport(dstDir, j.name)); }
  else toast(j.error||"Import failed",1);
}
async function undoImport(dstDir,name){
  const r=await fetch("/api/memory/undo_import",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dst:dstDir,name:name})});
  const j=await r.json();
  if(j.ok){ const p=MEMPROJ.find(x=>x.dir===dstDir); if(p&&p.count>0)p.count--;
    updateProjCount(dstDir);
    if(memProj.value===dstDir) loadMem(dstDir);
    toast("Import undone"); }
  else toast(j.error||"Undo failed",1);
}
memProj.addEventListener("change",()=>loadMem(memProj.value));
memQ.addEventListener("input",renderMem);

/* ================= INSIGHTS ================= */
let INS=null, _rsz;
const GOLD="#d3c24e";
function cnum(n){ return n>=10000?(n/1000).toFixed(n<100000?1:0)+"K":n.toLocaleString(); }
function barPath(x,y,w,h,r){ r=Math.max(0,Math.min(r,w/2,h));
  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y} `+
         `Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`; }
function hbar(x,y,w,h,r){ r=Math.max(0,Math.min(r,h/2,w));
  return `M${x},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h-r} `+
         `Q${x+w},${y+h} ${x+w-r},${y+h} L${x},${y+h} Z`; }
function dloc(k){ return new Date(k+"T00:00:00"); }
function md(k){ return dloc(k).toLocaleDateString([],{month:"short",day:"numeric"}); }
function mdy(k){ return dloc(k).toLocaleDateString([],{month:"short",day:"numeric",year:"2-digit"}); }

async function loadInsights(){
  if(!INS) INS=(await (await fetch("/api/sessions")).json()).sessions;
  renderInsights();
}
function renderInsights(){
  const S=INS||[];
  renderTiles(S);
  colDaily(document.getElementById("chartDaily"), S);
  barProjects(document.getElementById("chartProjects"), S);
  heat(document.getElementById("chartHeat"), S);
  attachTips(insightsView);
}
function renderTiles(S){
  const days=new Set(S.map(s=>(s.last_ts||"").slice(0,10)).filter(Boolean)).size;
  const msgs=S.reduce((a,s)=>a+(+s.n_user||0)+(+s.n_assistant||0),0);
  const projs=new Set(S.map(s=>s.folder)).size;
  const t=[["Sessions",S.length],["Messages",msgs],["Active days",days],["Projects",projs]];
  statTiles.innerHTML=t.map(([l,v])=>`<div class="tile"><div class="t-label">${l}</div>
    <div class="t-value">${cnum(v)}</div></div>`).join("");
}
function colDaily(el,S){
  const c={}; S.forEach(s=>{const d=(s.first_ts||s.last_ts||"").slice(0,10); if(d)c[d]=(c[d]||0)+1;});
  const keys=Object.keys(c).sort();
  if(!keys.length){ el.innerHTML="<p class='axis'>No data.</p>"; return; }
  const days=[]; let d=dloc(keys[0]); const end=dloc(keys[keys.length-1]);
  while(d<=end){ const k=d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+
    String(d.getDate()).padStart(2,"0"); days.push({k,n:c[k]||0}); d.setDate(d.getDate()+1); }
  const W=el.clientWidth||760, H=190, padL=30, padT=12, padB=26;
  const iw=W-padL-6, ih=H-padT-padB, max=Math.max(1,...days.map(x=>x.n));
  const step=iw/days.length, bw=Math.min(22, step*0.72);
  let bars="";
  days.forEach((x,i)=>{ if(!x.n) return; const h=x.n/max*ih, bx=padL+step*i+(step-bw)/2, by=padT+ih-h;
    bars+=`<path fill="${GOLD}" d="${barPath(bx,by,bw,h,3)}"
      data-tip="${mdy(x.k)} · ${x.n} session${x.n>1?'s':''}"></path>`; });
  const idxs=[...new Set([0,Math.floor(days.length/2),days.length-1])];
  const xlabs=idxs.map(i=>`<text class="axis" x="${padL+step*i+step/2}" y="${H-8}"
      text-anchor="middle">${md(days[i].k)}</text>`).join("");
  const grid=`<line class="grid" x1="${padL}" y1="${padT}" x2="${W-4}" y2="${padT}"></line>
    <text class="axis" x="${padL-6}" y="${padT+4}" text-anchor="end">${max}</text>
    <line class="grid" x1="${padL}" y1="${padT+ih}" x2="${W-4}" y2="${padT+ih}"></line>
    <text class="axis" x="${padL-6}" y="${padT+ih+4}" text-anchor="end">0</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}">${grid}${bars}${xlabs}</svg>`;
}
function barProjects(el,S){
  const c={}; S.forEach(s=>{const f=base(s.folder); c[f]=(c[f]||0)+1;});
  const arr=Object.entries(c).map(([k,n])=>({k,n})).sort((a,b)=>b.n-a.n).slice(0,10);
  if(!arr.length){ el.innerHTML=""; return; }
  const W=el.clientWidth||760, rowH=27, padT=6, padL=Math.min(190, Math.max(90,W*0.3)), padR=40;
  const H=padT*2+arr.length*rowH, iw=W-padL-padR, max=Math.max(1,...arr.map(x=>x.n)), bh=15;
  let rows=""; arr.forEach((x,i)=>{ const y=padT+i*rowH+(rowH-bh)/2, bw=Math.max(2,x.n/max*iw);
    rows+=`<text class="glab" x="${padL-8}" y="${y+bh/2+4}" text-anchor="end">${esc(x.k)}</text>
      <path fill="${GOLD}" d="${hbar(padL,y,bw,bh,3)}" data-tip="${esc(x.k)} · ${x.n} sessions"></path>
      <text class="axis" x="${padL+bw+6}" y="${y+bh/2+4}">${x.n}</text>`; });
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}">${rows}</svg>`;
}
function heat(el,S){
  const g=Array.from({length:7},()=>new Array(24).fill(0));
  S.forEach(s=>{ const t=s.first_ts||s.last_ts; if(!t) return; const d=new Date(t);
    if(isNaN(d)) return; g[(d.getDay()+6)%7][d.getHours()]++; });
  const max=Math.max(1,...g.flat()), wd=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
  const W=el.clientWidth||760, padL=34, padT=6, padB=18, gap=2;
  const cell=Math.max(10,(W-padL-6)/24), H=padT+padB+7*cell;
  let cells="";
  for(let r=0;r<7;r++){
    for(let h=0;h<24;h++){ const n=g[r][h], x=padL+h*cell, y=padT+r*cell;
      cells+=`<rect x="${x}" y="${y}" width="${cell-gap}" height="${cell-gap}" rx="2"
        fill="${n?GOLD:'#ffffff'}" fill-opacity="${n?(0.15+0.85*n/max).toFixed(2):0.05}"
        data-tip="${wd[r]} ${String(h).padStart(2,'0')}:00 · ${n} session${n===1?'':'s'}"></rect>`; }
    cells+=`<text class="axis" x="${padL-6}" y="${padT+r*cell+cell/2}" text-anchor="end"
      dominant-baseline="middle">${wd[r]}</text>`;
  }
  let hl=""; [0,6,12,18,23].forEach(h=>{ hl+=`<text class="axis" x="${padL+h*cell+(cell-gap)/2}"
    y="${H-6}" text-anchor="middle">${h}</text>`; });
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}">${cells}${hl}</svg>`;
}
function attachTips(root){
  const tip=document.getElementById("chartTip");
  root.querySelectorAll("[data-tip]").forEach(el=>{
    el.onmousemove=e=>{ tip.textContent=el.getAttribute("data-tip");
      tip.style.left=e.clientX+"px"; tip.style.top=e.clientY+"px"; tip.classList.add("show"); };
    el.onmouseleave=()=>tip.classList.remove("show");
  });
}
window.addEventListener("resize",()=>{ clearTimeout(_rsz);
  _rsz=setTimeout(()=>{ if(!insightsView.hidden && INS) renderInsights(); },150); });

/* ================= TRANSCRIPT MODAL ================= */
async function openTranscript(id){
  mTitle.textContent="Loading…"; mSub.textContent=""; mBody.innerHTML="";
  modal.hidden=false;
  let j;
  try{ j=await (await fetch("/api/transcript?id="+encodeURIComponent(id))).json(); }
  catch(e){ closeModal(); toast("Failed to load transcript",1); return; }
  if(j.error){ closeModal(); toast(j.error,1); return; }
  mTitle.textContent = j.label || j.scope || id;
  const nu=j.events.filter(e=>e.role==="user").length;
  const na=j.events.filter(e=>e.role==="assistant").length;
  mSub.textContent = `${base(j.folder)} · ${nu} prompts · ${na} replies`
      + (j.truncated?" · (truncated)":"");
  mBody.innerHTML = j.events.map(renderEvent).join("") || "<p class='axis'>Empty.</p>";
  mBody.scrollTop=0;
}
function renderEvent(e){
  if(e.role==="user")
    return `<div class="msg user"><div class="who">You</div>`+
           `<div class="bubble">${esc(e.text)}</div></div>`;
  const inner=e.blocks.map(b=>{
    if(b.t==="text") return `<div class="bubble">${esc(b.text)}</div>`;
    if(b.t==="thinking") return `<div class="think">${esc(b.text)}</div>`;
    if(b.t==="tool"){ const r=b.result;
      return `<details class="tool"><summary>⚙ ${esc(b.name)}</summary>`+
             `<pre>${esc(b.input)}</pre>`+
             (r?`<div class="rlabel">${r.error?"error":"result"}</div>`+
                `<pre class="${r.error?"err":""}">${esc(r.text)}</pre>`:"")+
             `</details>`; }
    return "";
  }).join("");
  return `<div class="msg assistant"><div class="who">Claude</div>${inner}</div>`;
}
function closeModal(){ modal.hidden=true; }
thinkChk.addEventListener("change",()=>
  mBody.classList.toggle("hide-think", !thinkChk.checked));
modal.addEventListener("click",e=>{ if(e.target===modal) closeModal(); });
window.addEventListener("keydown",e=>{ if(e.key==="Escape" && !modal.hidden) closeModal(); });

(function(){ const h=location.hash.slice(1);
  if(h.startsWith("t/")) openTranscript(h.slice(2));
  else if(["favorites","memory","insights"].includes(h)) setView(h); })();
load();
</script>
</body>
</html>
"""


def make_handler(db_path, projects_dir):
    # ThreadingHTTPServer runs each request in its own thread, and a single
    # sqlite3 connection cannot cross threads — so open a short-lived
    # connection per request. A lock serializes the (rare) rescan writes.
    write_lock = __import__("threading").Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            if u.path == "/api/sessions":
                qs = parse_qs(u.query)
                conn = connect(db_path)
                try:
                    sessions = query_sessions(conn,
                                              qs.get("q", [""])[0],
                                              qs.get("folder", [""])[0])
                    folders = folder_list(conn)
                finally:
                    conn.close()
                return self._send(200, {"sessions": sessions, "folders": folders})
            if u.path == "/api/transcript":
                qs = parse_qs(u.query)
                sid = qs.get("id", [""])[0]
                conn = connect(db_path)
                try:
                    row = conn.execute("SELECT path, folder, scope FROM sessions "
                                       "WHERE session_id = ?", (sid,)).fetchone()
                    lbl = conn.execute("SELECT label FROM labels WHERE session_id = ?",
                                       (sid,)).fetchone()
                finally:
                    conn.close()
                if not row or not row["path"] or not os.path.isfile(row["path"]):
                    return self._send(404, {"error": "transcript not found"})
                try:
                    data = load_transcript(row["path"])
                except OSError as e:
                    return self._send(400, {"error": str(e)})
                data.update({"session_id": sid, "folder": row["folder"],
                             "scope": row["scope"],
                             "label": lbl["label"] if lbl else ""})
                return self._send(200, data)
            if u.path == "/api/memory/projects":
                conn = connect(db_path)
                try:
                    return self._send(200, memory_projects(conn, projects_dir))
                finally:
                    conn.close()
            if u.path == "/api/memory/list":
                qs = parse_qs(u.query)
                try:
                    return self._send(200, memory_list(projects_dir,
                                                       qs.get("dir", [""])[0]))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/memory/file":
                qs = parse_qs(u.query)
                try:
                    content = memory_read(projects_dir, qs.get("dir", [""])[0],
                                          qs.get("name", [""])[0])
                    return self._send(200, {"content": content})
                except (ValueError, OSError) as e:
                    return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/rescan":
                with write_lock:
                    conn = connect(db_path)
                    try:
                        stats = scan(conn, projects_dir, verbose=False)
                    finally:
                        conn.close()
                return self._send(200, stats)
            if u.path == "/api/label":
                body = self._body()
                sid = body.get("session_id")
                if not sid:
                    return self._send(400, {"error": "session_id required"})
                with write_lock:
                    conn = connect(db_path)
                    try:
                        res = set_label(conn, sid, body.get("label", ""))
                    finally:
                        conn.close()
                return self._send(200, res)
            if u.path == "/api/favorite":
                body = self._body()
                sid = body.get("session_id")
                if not sid:
                    return self._send(400, {"error": "session_id required"})
                with write_lock:
                    conn = connect(db_path)
                    try:
                        res = set_favorite(conn, sid, bool(body.get("favorite")))
                    finally:
                        conn.close()
                return self._send(200, res)
            if u.path == "/api/memory/import":
                body = self._body()
                try:
                    with write_lock:
                        res = memory_import(projects_dir, body.get("src"),
                                            body.get("name"), body.get("dst"))
                    return self._send(200, res)
                except (ValueError, OSError) as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/memory/undo_import":
                body = self._body()
                try:
                    with write_lock:
                        res = memory_undo_import(projects_dir, body.get("dst"),
                                                 body.get("name"))
                    return self._send(200, res)
                except (ValueError, OSError) as e:
                    return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Claude session tracker")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--projects", default=DEFAULT_PROJECTS,
                    help="root of Claude transcript project dirs")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--scan-only", action="store_true",
                    help="rebuild index and exit")
    args = ap.parse_args()

    conn = connect(args.db)
    t0 = time.time()
    scan(conn, args.projects)
    if args.scan_only:
        return
    print(f"[index] {time.time()-t0:.2f}s  db={args.db}")

    conn.close()
    handler = make_handler(args.db, args.projects)
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Claude session tracker → {url}   (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
