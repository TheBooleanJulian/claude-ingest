#!/usr/bin/env python3
"""
claude-ingest
-------------
Turn a claude.ai data export into project folders and GitHub repos.

  python claude_ingest.py extract EXPORT [--out out]
      Read conversations.json (from the export .zip or an extracted folder),
      replay every file Claude created or edited in each chat, and write the
      final state to out/chats/<date>_<slug>/files/. Also writes
      out/manifest.csv for you to review.

  python claude_ingest.py publish [--out out] [--root ..] [--apply]
      For every manifest row whose `action` is `new`: create <root>/<name>,
      copy the files in, git init (main + dev), commit, create a PRIVATE
      GitHub repo with gh, and push. Without --apply it only prints the plan.

Standard library only. Needs git and gh on PATH for `publish`.
Nothing in the export is ever executed.
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

MANIFEST_COLUMNS = [
    "chat_id", "date", "title", "files", "presented", "secrets",
    "suggested_name", "existing_match", "action", "name", "notes",
]

ARTIFACT_EXT = {
    "text/html": ".html",
    "application/vnd.ant.react": ".jsx",
    "image/svg+xml": ".svg",
    "text/markdown": ".md",
    "application/vnd.ant.mermaid": ".mmd",
    "application/vnd.ant.code": ".txt",
}
CODE_LANG_EXT = {
    "python": ".py", "javascript": ".js", "typescript": ".ts", "html": ".html",
    "css": ".css", "json": ".json", "bash": ".sh", "shell": ".sh", "sql": ".sql",
    "yaml": ".yml", "powershell": ".ps1", "java": ".java", "go": ".go", "rust": ".rs",
}
PATH_PREFIXES = ("/home/claude/", "/mnt/user-data/outputs/", "/mnt/user-data/", "/mnt/outputs/")

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"),
]


# ---------------------------------------------------------------- helpers

def slugify(text, fallback="untitled", limit=50):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-")
    return slug or fallback


def load_conversations(export):
    """Return the conversation list from an export .zip or folder."""
    export = Path(export)
    if export.is_dir():
        path = export / "conversations.json"
        return json.loads(path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(export) as zf:
        names = [n for n in zf.namelist() if n.endswith("conversations.json")]
        if not names:
            raise SystemExit(f"no conversations.json in {export}")
        with zf.open(names[0]) as f:  # read in memory; nothing is extracted to disk
            return json.loads(f.read().decode("utf-8"))


def clean_path(path):
    """Map a sandbox path like /home/claude/app/x.py to a safe relative path."""
    for prefix in PATH_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    parts = [p for p in re.split(r"[\\/]+", path) if p not in ("", ".", "..")]
    return "/".join(parts)


def artifact_filename(art_id, art_type, language):
    if art_type == "application/vnd.ant.code" and language:
        ext = CODE_LANG_EXT.get(language.lower(), ".txt")
    else:
        ext = ARTIFACT_EXT.get(art_type, ".txt")
    return f"artifacts/{slugify(art_id, 'artifact')}{ext}"


def find_secrets(files):
    """Return sorted file names whose text matches a secret pattern."""
    hits = []
    for name, text in files.items():
        if any(p.search(text) for p in SECRET_PATTERNS):
            hits.append(name)
    return sorted(hits)


# ---------------------------------------------------------------- replay

def replay_chat(conv):
    """Replay file tool calls in one chat.

    Returns (files, presented): files maps relative path -> final text,
    presented is the set of relative paths Claude handed back to the user.
    """
    files = {}
    artifacts = {}   # id -> [relative path, type, language]
    presented = set()

    for msg in conv.get("chat_messages", []):
        for block in msg.get("content", []):
            if block.get("type") != "tool_use":
                continue
            name, inp = block.get("name"), block.get("input") or {}

            if name == "create_file" and inp.get("path"):
                files[clean_path(inp["path"])] = inp.get("file_text", "")

            elif name == "str_replace" and inp.get("path"):
                rel = clean_path(inp["path"])
                if rel in files and inp.get("old_str") is not None:
                    files[rel] = files[rel].replace(inp["old_str"], inp.get("new_str", ""), 1)

            elif name == "present_files":
                raw = inp.get("filepaths") or []
                if isinstance(raw, str):
                    raw = re.findall(r"['\"]([^'\"]+)['\"]", raw)
                presented.update(clean_path(p) for p in raw)

            elif name == "artifacts" and inp.get("id"):
                art_id, cmd = inp["id"], inp.get("command")
                if cmd in ("create", "rewrite") and "content" in inp:
                    if art_id not in artifacts:
                        artifacts[art_id] = [
                            artifact_filename(art_id, inp.get("type"), inp.get("language")),
                            inp.get("type"), inp.get("language")]
                    files[artifacts[art_id][0]] = inp["content"]
                elif cmd == "update" and art_id in artifacts:
                    rel = artifacts[art_id][0]
                    if rel in files and inp.get("old_str") is not None:
                        files[rel] = files[rel].replace(inp["old_str"], inp.get("new_str", ""), 1)

    presented = {p for p in presented if p in files}
    return files, presented


# ---------------------------------------------------------------- extract

def suggest_existing(slug, existing_names):
    """Name of a local folder whose words overlap the chat title, else ''."""
    words = {w for w in slug.split("-") if len(w) > 2}
    best, best_score = "", 0
    for existing in existing_names:
        ex_words = {w for w in existing.lower().split("-") if len(w) > 2}
        score = len(words & ex_words)
        if existing.lower() == slug:
            return existing
        if score >= 2 and score > best_score:
            best, best_score = existing, score
    return best


def extract(export, out, root):
    convs = load_conversations(export)
    out = Path(out)
    chats_dir = out / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    existing = [p.name for p in Path(root).iterdir()
                if p.is_dir() and not p.name.startswith((".", "_"))]

    rows = []
    for conv in convs:
        files, presented = replay_chat(conv)
        if not files:
            continue
        date = (conv.get("created_at") or "")[:10] or "undated"
        title = conv.get("name") or "untitled"
        slug = slugify(title)
        chat_dir = chats_dir / f"{date}_{slug}_{conv['uuid'][:8]}"
        for rel, text in files.items():
            dest = chat_dir / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        (chat_dir / "chat.json").write_text(json.dumps({
            "uuid": conv["uuid"], "name": title, "created_at": conv.get("created_at"),
            "summary": conv.get("summary", ""), "files": sorted(files),
            "presented": sorted(presented)}, indent=2), encoding="utf-8")

        secrets = find_secrets(files)
        rows.append({
            "chat_id": chat_dir.name, "date": date, "title": title,
            "files": len(files), "presented": len(presented),
            "secrets": ";".join(secrets), "suggested_name": slug,
            "existing_match": suggest_existing(slug, existing),
            "action": "", "name": "", "notes": ""})

    rows.sort(key=lambda r: r["date"])
    manifest = out / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(convs)} chats read, {len(rows)} with files -> {manifest}")
    print("Edit `action` (new | skip) and `name` for each row, then run publish.")
    return rows


# ---------------------------------------------------------------- publish

def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True)


def publish(out, root, apply):
    out, root = Path(out), Path(root)
    with (out / "manifest.csv").open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    todo = []
    for r in rows:
        if r["action"].strip().lower() != "new":
            continue
        name = slugify(r["name"] or r["suggested_name"])
        if r["secrets"]:
            print(f"HOLD  {name}: possible secrets in {r['secrets']} (clear the cell once reviewed)")
            continue
        if (root / name).exists():
            print(f"SKIP  {name}: folder already exists in {root}")
            continue
        todo.append((name, r))

    for name, r in todo:
        print(f"PLAN  {name}  <- {r['chat_id']}  ({r['files']} files)")
    if not apply:
        print("Dry run. Re-run with --apply to create folders and private repos.")
        return

    for name, r in todo:
        src = out / "chats" / r["chat_id"] / "files"
        dest = root / name
        shutil.copytree(src, dest)
        (dest / ".gitignore").write_text(".env\n__pycache__/\n*.pyc\nnode_modules/\n", encoding="utf-8")
        if not (dest / "README.md").exists():
            (dest / "README.md").write_text(
                f"# {r['title']}\n\nImported from a Claude chat on {r['date']}.\n", encoding="utf-8")
        run(["git", "init", "-q", "-b", "main"], dest)
        run(["git", "add", "-A"], dest)
        run(["git", "commit", "-q", "-m",
             f"feat: initial import from Claude chat ({r['date']})\n\n"
             "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"], dest)
        run(["git", "branch", "dev"], dest)
        run(["gh", "repo", "create", f"TheBooleanJulian/{name}", "--private",
             "--source", ".", "--remote", "origin", "--push"], dest)
        run(["git", "push", "-q", "origin", "dev"], dest)
        print(f"DONE  {name}")


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="rebuild files from an export and write the manifest")
    e.add_argument("export", help="export .zip or extracted folder")
    e.add_argument("--out", default="out")
    e.add_argument("--root", default="..", help="folder holding your project folders")

    p = sub.add_parser("publish", help="create folders and private repos for rows marked `new`")
    p.add_argument("--out", default="out")
    p.add_argument("--root", default="..")
    p.add_argument("--apply", action="store_true", help="actually do it (default is a dry run)")

    args = ap.parse_args(argv)
    if args.cmd == "extract":
        extract(args.export, args.out, args.root)
    else:
        publish(args.out, args.root, args.apply)


if __name__ == "__main__":
    main()
