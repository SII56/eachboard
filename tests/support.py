import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]


class BoardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eachboard-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "shared"
        self.workspace.mkdir()
        self.cwd = self.workspace
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("EACHBOARD_")}
        self.admin_password = "test-admin"
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(APP))
        self.run_eachboard("init")
        self.root = self.workspace / ".eachboard"
        opened = self.run_eachboard("open", "--participants", "writer,reviewer,observer")
        self.cid, self.keys = opened["conv_id"], opened["keys"]

    def identity(self, who):
        return dict(self.env, EACHBOARD_ROOT=str(self.root), EACHBOARD_CONV=self.cid,
                    EACHBOARD_KEY=self.keys[who])

    def admin_env(self, **changes):
        return dict(self.env, EACHBOARD_KEY=self.admin_password, **changes)

    def run_eachboard(self, *args, who=None, error=None, env=None, stdin=None):
        if env is None and who is None and args and args[0] in ("open", "init"):
            env = self.admin_env()
        p = subprocess.run([sys.executable, "-m", "eachboard", "--json", *args],
                           env=env or (self.identity(who) if who else self.env),
                           input=stdin, capture_output=True, text=True, timeout=20, cwd=getattr(self, 'cwd', APP))
        self.assertNotIn("Traceback", p.stderr)
        result = json.loads(p.stdout)
        if error:
            self.assertEqual(p.returncode, 1, result)
            self.assertEqual(result["code"], error, result)
        else:
            self.assertEqual(p.returncode, 0, result)
            self.assertTrue(result["ok"])
        return result

    def send(self, content="hello", **kwargs):
        return self.run_eachboard("send", "--content", content, who="writer", **kwargs)

    def _conversation_path(self, name):
        return self.root / "conversations" / self.cid / name
