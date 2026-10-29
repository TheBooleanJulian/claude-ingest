import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_ingest as ci


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


CONV = {
    "uuid": "abcdef12-0000", "name": "Busking Calendar Scraper", "created_at": "2026-01-02T10:00:00Z",
    "chat_messages": [{"content": [
        tool("create_file", path="/home/claude/app/main.py", file_text="print('a')\n"),
        tool("str_replace", path="/home/claude/app/main.py", old_str="'a'", new_str="'b'"),
        tool("str_replace", path="/home/claude/missing.py", old_str="x", new_str="y"),
        tool("present_files", filepaths="['/home/claude/app/main.py', '/mnt/user-data/outputs/gone.pdf']"),
        tool("artifacts", id="cal_ui", type="text/html", command="create", content="<p>one</p>"),
        tool("artifacts", id="cal_ui", command="update", old_str="one", new_str="two"),
        tool("artifacts", id="cfg", type="application/vnd.ant.code", language="python",
             command="create", content="KEY = '" + "AIza" + "x" * 35 + "'\n"),
        tool("bash_tool", command="ls"),
    ]}],
}
NO_FILES = {"uuid": "11111111-0", "name": "Just chatting", "chat_messages": [{"content": []}]}


class ReplayTests(unittest.TestCase):
    def test_replay_applies_edits_and_ignores_unknown_paths(self):
        files, presented = ci.replay_chat(CONV)
        self.assertEqual(files["app/main.py"], "print('b')\n")
        self.assertNotIn("missing.py", files)
        self.assertEqual(files["artifacts/cal-ui.html"], "<p>two</p>")
        self.assertEqual(files["artifacts/cfg.py"][:6], "KEY = ")
        self.assertEqual(presented, {"app/main.py"})

    def test_clean_path_blocks_traversal(self):
        self.assertEqual(ci.clean_path("/home/claude/../../etc/passwd"), "etc/passwd")

    def test_secret_detection(self):
        files, _ = ci.replay_chat(CONV)
        self.assertEqual(ci.find_secrets(files), ["artifacts/cfg.py"])


class ExtractTests(unittest.TestCase):
    def test_extract_writes_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            export = tmp / "export"
            export.mkdir()
            (export / "conversations.json").write_text(json.dumps([CONV, NO_FILES]), encoding="utf-8")
            root = tmp / "root"
            (root / "busking-calendar").mkdir(parents=True)
            rows = ci.extract(export, tmp / "out", root)
            self.assertEqual(len(rows), 1)  # chat with no files is skipped
            self.assertEqual(rows[0]["existing_match"], "busking-calendar")
            self.assertEqual(rows[0]["secrets"], "artifacts/cfg.py")
            with (tmp / "out" / "manifest.csv").open(encoding="utf-8-sig") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)
            chat_dir = tmp / "out" / "chats" / rows[0]["chat_id"]
            self.assertTrue((chat_dir / "files" / "app" / "main.py").exists())


if __name__ == "__main__":
    unittest.main()
