"""Regression tests for binary transport and native input/auth handling."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration_core import MigrationConfig, MigrationError, _Runner, validate_config


class NativeSupportTests(unittest.TestCase):
    def test_gitlab_page_is_rejected_before_workspace_or_network(self):
        config = MigrationConfig(
            "https://example.invalid/gitlab/team/project/-/commits/release", 5000,
            "https://example.invalid/gitlab/team/project.git", "prova",
        )
        with self.assertRaisesRegex(MigrationError, "pagina web di GitLab"):
            validate_config(config)
        source = replace(config, svn_url="https://example.invalid/svn/repo/branches/release")
        self.assertEqual(validate_config(source), source)

    def test_binary_transport_is_exact_and_stderr_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input bytes.bin"
            destination = Path(directory) / "output bytes.bin"
            data = bytes(range(256)) * 1024 + b"\r\n\0\xff\n"
            source.write_bytes(data)
            runner = _Runner(lambda _: None, threading.Event())
            result = runner.run([
                sys.executable, "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
                "sys.stderr.write('diagnostic only\\n')",
            ], input_file=source, output_bytes_file=destination, timeout=10)
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(result.stdout, "diagnostic only\n")

    def test_native_runtime_preserves_selected_ssh_client(self):
        runner = _Runner(lambda _: None, threading.Event())
        environment = dict(os.environ)
        environment.pop("GIT_SSH_COMMAND", None)
        environment["GIT_SSH"] = "configured-plink.exe"
        environment["SVN_SSH"] = "configured-svn-ssh.exe"
        runner.runtime = replace(runner.runtime, windows=True, environment=environment)
        result = runner.run([
            sys.executable, "-c",
            "import json,os; print(json.dumps({k:os.environ.get(k) for k in "
            "['GIT_SSH','SVN_SSH','GIT_SSH_COMMAND']}))",
        ], timeout=10)
        actual = json.loads(result.stdout)
        self.assertEqual(actual, {"GIT_SSH": "configured-plink.exe",
                                  "SVN_SSH": "configured-svn-ssh.exe", "GIT_SSH_COMMAND": None})


if __name__ == "__main__":
    unittest.main()
