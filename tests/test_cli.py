import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CLITests(unittest.TestCase):
    def test_file_stdin_restart_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-m", "minidb", "--data-dir", directory]
            result = subprocess.run(command + ["exec", "--file", "examples/demo.sql", "--format", "json"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            statements = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(statements), 8)
            self.assertEqual(statements[1]["row_count"], 3)
            result = subprocess.run(command + ["exec", "--stdin", "--format", "json"],
                                    input="SELECT COUNT(*) FROM users", capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["rows"], [{"count": 3}])
            result = subprocess.run(command + ["exec", "--sql", "SELECT * FROM users WHERE id = 2"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BPLUS_POINT_LOOKUP", result.stdout)
            result = subprocess.run(command + ["exec", "--sql", "INVALID"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("error:", result.stderr)

    def test_script_syntax_error_does_not_partially_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-m", "minidb", "--data-dir", directory, "exec", "--sql"]
            result = subprocess.run(command + ["INSERT INTO users VALUES ('x', 1, NULL); INVALID"], capture_output=True)
            self.assertEqual(result.returncode, 1)
            result = subprocess.run(command + ["SELECT COUNT(*) FROM users", "--format", "json"], capture_output=True)
            self.assertEqual(json.loads(result.stdout)["rows"], [{"count": 0}])
