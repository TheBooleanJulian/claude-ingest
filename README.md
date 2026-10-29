# claude-ingest

Turn a claude.ai data export into project folders and GitHub repos. Standard library only; needs `git` and `gh` for the publish step.

## Workflow

1. **Export** your chats: claude.ai → Settings → Privacy → Export data. Keep the zip (or the extracted folder) out of git.
2. **Extract** (read-only, nothing is executed):
   ```bash
   python claude_ingest.py extract path/to/export.zip --out out --root ..
   ```
   For every chat that created files, it replays `create_file` / `str_replace` calls and artifact create/update/rewrite calls, then writes the final files to `out/chats/<date>_<slug>_<id>/files/` and a review sheet, `out/manifest.csv`.
3. **Review** `manifest.csv` in a spreadsheet. For each row set `action` to `new` or `skip` and optionally set `name`. Columns to check:
   - `presented`: files Claude handed back to you (a good sign the work is real)
   - `secrets`: files matching a key/token pattern (rows with this are held back by publish)
   - `existing_match`: a folder in `--root` with a similar name (probably merge, not new)
4. **Publish**:
   ```bash
   python claude_ingest.py publish --out out --root ..          # dry run
   python claude_ingest.py publish --out out --root .. --apply  # do it
   ```
   For each `new` row: creates `<root>/<name>`, copies the files in, adds `.gitignore` and a stub `README.md` if missing, runs `git init` on `main`, creates `dev`, commits, creates a **private** repo `TheBooleanJulian/<name>` and pushes both branches.

## Limits

- Only files Claude created through tools are recovered (code execution files and artifacts). Code shown in plain chat text and uploaded files are not.
- Edits are replayed in order; an edit whose target text no longer matches is skipped silently.
- Secret detection is pattern-based, not a guarantee. Review held and unheld rows before making anything public.

## Tests

```bash
python -m unittest discover -s tests
```
