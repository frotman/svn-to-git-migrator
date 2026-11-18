"""Prove reali del motore nativo SVN CLI + Git, senza dipendere da git-svn."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration_core import MigrationCancelled, MigrationError, Migrator
import test_integration as integration

command = integration.command


class NativeMigrationTests(integration.RealMigrationTests):
    __unittest_skip__ = not all(shutil.which(name) for name in ("git", "svn", "svnadmin"))
    __unittest_skip_why__ = "Requires git, svn and svnadmin"

    def migrator(self, cancel=None):
        return Migrator(self.config, self.root / "migration", self.events.append,
                        cancel, engine="svn-cli", history_scope="path")

    def test_stop_after_first_imported_revision_and_resume(self):
        cancelled = threading.Event()
        stopped_at = []

        def stop_on_first_commit(event):
            self.events.append(event)
            if event.get("type") == "progress" and event.get("current", 0) >= 1 and not stopped_at:
                stopped_at.append(event["current"])
                cancelled.set()

        migrator = Migrator(self.config, self.root / "migration", stop_on_first_commit,
                            cancelled, engine="svn-cli", history_scope="path")
        with self.assertRaises(MigrationCancelled):
            migrator.migrate()
        self.assertEqual(stopped_at, [1])
        result = self.migrator().migrate()
        self.assertEqual(result.imported_revisions, (3, 5, 7, 8))
        self._assert_tree(result.repo_path)

    def test_canonical_eol_keywords_modes_and_no_git_filters(self):
        self._svn("update", self.wc)
        (self.branch / "native.txt").write_bytes(b"first\n$Id$\n")
        (self.branch / "crlf.txt").write_bytes(b"first\r\nsecond\r\n")
        (self.branch / "run.sh").write_bytes(b"#!/bin/sh\necho hello\n")
        (self.branch / ".gitignore").write_text("important.txt\n", encoding="utf-8")
        (self.branch / ".gitattributes").write_text("*.txt text eol=crlf filter=migration-test\n", encoding="utf-8")
        (self.branch / "important.txt").write_bytes(b"must be imported\n")
        self._svn("add", self.branch / "native.txt", self.branch / "crlf.txt",
                  self.branch / "run.sh", self.branch / ".gitignore",
                  self.branch / ".gitattributes", self.branch / "important.txt")
        self._svn("propset", "svn:eol-style", "native", self.branch / "native.txt")
        self._svn("propset", "svn:keywords", "Id", self.branch / "native.txt")
        self._svn("propset", "svn:eol-style", "CRLF", self.branch / "crlf.txt")
        self._svn("propset", "svn:executable", "*", self.branch / "run.sh")
        self._svn("commit", self.wc, "-m", "canonical content", "--username", "tester")
        self.config = replace(self.config, revision_max=10)
        migrator = self.migrator()
        migrator.analyze()
        migrator._initialize_repository()
        command("git", "-C", migrator.repo_path, "config", "filter.migration-test.smudge", "false")
        command("git", "-C", migrator.repo_path, "config", "filter.migration-test.process", "false")
        command("git", "-C", migrator.repo_path, "config", "filter.migration-test.required", "true")
        result = migrator.migrate()
        self.assertEqual(command("git", "-C", result.repo_path, "config", "--get",
                                 "filter.migration-test.required").stdout.strip(), "true")
        for path, expected in {
            "native.txt": b"first\n$Id$\n", "crlf.txt": b"first\nsecond\n",
            "important.txt": b"must be imported\n",
        }.items():
            actual = command("git", "-C", result.repo_path, "show", "HEAD:" + path,
                             text=False).stdout
            self.assertEqual(actual, expected, path)
        mode = command("git", "-C", result.repo_path, "ls-tree", "HEAD", "run.sh").stdout
        self.assertTrue(mode.startswith("100755 "), mode)
        if os.name != "nt":
            link_mode = command("git", "-C", result.repo_path, "ls-tree", "HEAD", "link.txt").stdout
            self.assertTrue(link_mode.startswith("120000 "), link_mode)

    def test_directory_replacement_invalidates_same_revision_file_cache(self):
        self._svn("update", self.wc)
        for directory, contents in (("source-one", "ONE\n"), ("source-two", "TWO\n")):
            path = self.wc / "trunk" / directory
            path.mkdir()
            (path / "same.txt").write_text(contents, encoding="utf-8")
            self._svn("add", path)
        self._svn("commit", self.wc, "-m", "two distinct sources in one revision", "--username", "builder")
        self._svn("copy", self.url + "/trunk/source-one@10", self.url + "/branches/target/replaced",
                  "-m", "copy first source", "--username", "builder")
        self._svn("update", self.wc)
        self._svn("delete", self.branch / "replaced")
        self._svn("copy", self.url + "/trunk/source-two@10", self.branch / "replaced")
        self._svn("commit", self.wc, "-m", "replace with second source", "--username", "builder")
        self.config = replace(self.config, revision_max=12)
        result = self.migrator().migrate()
        contents = command("git", "-C", result.repo_path, "show", "HEAD:replaced/same.txt",
                           text=False).stdout
        self.assertEqual(contents, b"TWO\n")
        before = command("git", "-C", result.repo_path, "show", "HEAD^:replaced/same.txt",
                         text=False).stdout
        self.assertEqual(before, b"ONE\n")

    def test_private_working_copy_edits_are_not_silently_imported(self):
        cancel = threading.Event()

        def stop(event):
            if event.get("type") == "progress" and event.get("current", 0) == 1:
                cancel.set()

        with self.assertRaises(MigrationCancelled):
            Migrator(self.config, self.root / "migration", stop, cancel,
                     engine="svn-cli", history_scope="path").migrate()
        private_file = self.root / "migration/svn-working-copy/changing.txt"
        private_file.write_text("MODIFICA LOCALE DA CONSERVARE\n", encoding="utf-8")
        with self.assertRaises(MigrationError):
            self.migrator().migrate()
        self.assertIn("MODIFICA LOCALE DA CONSERVARE", private_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
