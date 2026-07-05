# Claude Session Tracker

A tiny local web UI over your Claude Code session transcripts
(`~/.claude/projects/*/*.jsonl`). Shows **session ID · folder · scope**
plus last-activity time and message counts. No dependencies (Python stdlib
only), read-only w.r.t. Claude's data, indexed into a local SQLite file.

## Run

```bash
cd ~/q/session_tracker
python3 session_tracker.py            # http://127.0.0.1:8765
```

Then open the URL. Search box filters label/scope/id/folder/first-prompt; the
folder dropdown narrows to one project; column headers sort; **↻ Rescan**
re-indexes new or changed sessions on demand.

- **Click a row** to expand a short details panel: resume command, full
  session ID, folder, start/last time + duration, message counts, model, and
  the first/last prompt.
- **✎ Rename** sets a custom label that overrides the (sometimes misleading)
  auto scope. Labels are stored separately and survive rescans; blank clears
  it. The original scope stays visible underneath.
- **★ Star** — the leftmost column toggles a favorite. Starred sessions also
  collect under the **Favorites** tab (same rows, resume, and transcript view).
  Stars live in a separate table, so a rescan never clears them.
- **Resume** — the details panel shows `cd <folder> && claude --resume <id>`
  with a ⧉ copy button; paste it in a terminal to continue that session.
- **View full conversation** — opens a modal reader that reconstructs the whole
  transcript: your prompts and Claude's replies as bubbles, tool calls collapsed
  into expandable `⚙` blocks (each showing its input and result), and Claude's
  internal thinking hidden behind a **thinking** toggle. Close with ✕, the
  backdrop, or Esc. Deep-linkable via `#t/<session-id>`.

## Memory tab

The **Memory** toggle (top of the page) switches to a browser over Claude's
per-project memory (`~/.claude/projects/<project>/memory/*.md`). Pick a project
from the dropdown to see its memories as cards (title, type badge, description,
size/date):

- **⧉ Copy** puts a memory's full markdown on your clipboard, ready to paste
  into a chat.
- **View** expands the memory inline.
- **Import to…** copies that memory file into another project's `memory/` dir
  and appends its line to that project's `MEMORY.md`. It won't overwrite an
  existing file of the same name, import the `MEMORY.md` index itself, or
  import into the same project. After a successful import a toast offers
  **Undo**, which deletes the copied file and removes the line it added. Import
  is the only place the tool writes to Claude's data — everything else is
  read-only.

## Insights tab

A third tab with a few interactive charts computed from the session index
(pure inline SVG, no libraries):

- **Stat tiles** — total sessions, messages, active days, projects.
- **Sessions per day** — column chart across your active date range.
- **Top projects** — horizontal bars ranked by session count.
- **Activity** — a weekday × hour heatmap of when sessions start (local time).

Hover any mark for a tooltip. Charts re-fit on window resize. Tabs are
deep-linkable via the URL hash (`#sessions`, `#favorites`, `#memory`, `#insights`).

## Theme

The UI uses Atomize's dark palette (from `atomize/general_modules/gui_style.py`):
indigo background `#2a2a40`, lavender text `#c1cae3`, gold accent `#d3c24e`. All
shades derive from those base colors via CSS `color-mix`, so re-skinning is just
editing the `--bg` / `--base` / `--fg` / `--accent` variables in the `:root`
block near the top of the embedded stylesheet.

## Options

```
--port 9000            # change port (default 8765)
--host 0.0.0.0         # bind address (default 127.0.0.1, localhost only)
--projects DIR         # transcript root (default ~/.claude/projects)
--db PATH              # sqlite index location (default ./sessions.db)
--scan-only            # rebuild the index and exit (no server)
```

## Windows

Works the same — it's pure Python stdlib. On Windows:

```
py session_tracker.py            # or: python session_tracker.py
```

Transcripts are read at `%USERPROFILE%\.claude\projects` (i.e. `~/.claude`),
the same default as macOS/Linux; pass `--projects` if yours live elsewhere.
Files are read as UTF-8 so non-ASCII (Cyrillic, emoji, …) is safe. The resume
command is emitted in cmd.exe form — `cd /d "<folder>" && claude --resume <id>`
— which handles drive changes and spaces. In **PowerShell**, `&&` works in
PowerShell 7+; on Windows PowerShell 5.1 run the two parts on separate lines
(`cd "<folder>"` then `claude --resume <id>`).

## What "scope" means

Per session it's the model's own AI title for the session (`ai-title`
record). If a session has none yet, it falls back to the first real user
prompt, then to the last prompt.

## How indexing works

On startup (and on Rescan) it walks every `*.jsonl` transcript and stores one
row per session in `sessions.db`. Re-scans are incremental: a transcript is
only re-parsed if its size or modification time changed, so refreshes are
fast even with large histories.
