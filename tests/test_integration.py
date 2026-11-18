"""Real SVN/Git integration tests. No external server or account is used.

Run from the project folder: python -m unittest discover -s tests -v
The SVN tests explicitly skip when svn, svnadmin or git-svn is unavailable.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration_core import (  # noqa: E402
    MigrationCancelled,
    MigrationConfig,
    MigrationError,
    Migrator,
    plan_revision_ranges,
)


def command(*args, cwd=None, text=True, check=True):
    environment = os.environ.copy()
    environment.update({
        "GIT_AUTHOR_NAME": "Fixture author",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture committer",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    })
    return subprocess.run(
        [str(a) for a in args], cwd=cwd, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=text, check=check, timeout=90,
    )


def git_svn_available():
    if not all(shutil.which(p) for p in ("git", "svn", "svnadmin")):
        return False
    try:
        return command("git", "svn", "--version", check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(git_svn_available(), "Requires svn, svnadmin and git-svn")
class RealMigrationTests(unittest.TestCase):
    """A branch copied from trunk, sparse commits, binaries, deletes and a cut-off."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="svn-git-integration-")
        self.root = Path(self.temporary.name)
        self.svn_repo = self.root / "source"
        self.wc = self.root / "checkout"
        self.target = self.root / "destination.git"
        command("svnadmin", "create", self.svn_repo)
        self.url = self.svn_repo.as_uri()
        command("git", "init", "--bare", self.target)
        self._svn("mkdir", self.url + "/trunk", self.url + "/branches",
                  self.url + "/unrelated", "-m", "layout", "--username", "layout")
        self._svn("checkout", self.url, self.wc)
        trunk = self.wc / "trunk"
        (trunk / "folder").mkdir()
        (trunk / "keep.txt").write_text("contenuto originale\n", encoding="utf-8")
        (trunk / "changing.txt").write_text("prima\n", encoding="utf-8")
        (trunk / "folder/nested.txt").write_text("sottoalbero\n", encoding="utf-8")
        (trunk / "blob.bin").write_bytes(bytes(range(256)))
        (trunk / "caffè-東京.txt").write_text("Accenti e Unicode: caffè 東京\n", encoding="utf-8")
        self._svn("add", trunk / "keep.txt", trunk / "changing.txt",
                  trunk / "folder", trunk / "blob.bin", trunk / "caffè-東京.txt")
        if os.name != "nt":
            os.symlink("changing.txt", trunk / "link.txt")
            self._svn("add", trunk / "link.txt")
        self._svn("commit", self.wc, "-m", "seed trunk", "--username", "alice")
        self._svn("copy", self.url + "/trunk", self.url + "/branches/target",
                  "-m", "crea branch", "--username", "creator")
        self._svn("update", self.wc)
        self.branch = self.wc / "branches/target"
        unrelated = self.wc / "unrelated/noise.txt"
        unrelated.write_text("rumore uno\n", encoding="utf-8")
        self._svn("add", unrelated)
        self._svn("commit", self.wc, "-m", "unrelated 1", "--username", "eve")
        (self.branch / "changing.txt").write_text("seconda\n", encoding="utf-8")
        self._svn("move", self.branch / "keep.txt", self.branch / "kept.txt")
        self._svn("commit", self.wc, "-m", "modifica e rinomina", "--username", "bob")
        unrelated.write_text("rumore due\n", encoding="utf-8")
        self._svn("commit", self.wc, "-m", "unrelated 2", "--username", "eve")
        self._svn("copy", self.url + "/trunk/folder",
                  self.url + "/branches/target/copied",
                  "-m", "copia directory esterna", "--username", "carol")
        self._svn("update", self.wc)
        self._svn("delete", self.branch / "folder")
        (self.branch / "blob.bin").write_bytes(b"\x00\xffmodificato\x00\x01")
        self._svn("commit", self.wc, "-m", "elimina e cambia binario", "--username", "dan")
        (self.branch / "changing.txt").write_text("FUORI DAL LIMITE\n", encoding="utf-8")
        self._svn("commit", self.wc, "-m", "after max", "--username", "later")
        self.config = MigrationConfig(
            self.url + "/branches/target", 8, str(self.target), "import/target",
        )
        self.events = []

    def tearDown(self):
        self.temporary.cleanup()

    def _svn(self, *args):
        return command("svn", "--non-interactive", *args)

    def migrator(self, cancel=None):
        return Migrator(self.config, self.root / "migration", self.events.append, cancel,
                        engine="git-svn", history_scope="path")

    def _assert_tree(self, repo_path, revision=8):
        expected = self.root / "expected"
        self._svn("export", "--ignore-externals", "-r", str(revision),
                  self.config.svn_url + "@" + str(revision), expected)
        paths = command("git", "-C", repo_path, "ls-tree", "-rz", "--name-only", "HEAD",
                        text=False).stdout.split(b"\0")
        actual_paths = {p.decode("utf-8") for p in paths if p}
        expected_paths = {p.relative_to(expected).as_posix() for p in expected.rglob("*")
                          if p.is_file() or p.is_symlink()}
        self.assertEqual(actual_paths, expected_paths)
        for relative in expected_paths:
            actual = command("git", "-C", repo_path, "show", f"HEAD:{relative}",
                             text=False).stdout
            target = expected / relative
            contents = os.readlink(target).encode("utf-8") if target.is_symlink() else target.read_bytes()
            self.assertEqual(actual, contents, relative)

    def test_history_tree_authors_dates_and_cutoff(self):
        migrator = self.migrator()
        analysis = migrator.analyze()
        self.assertEqual(tuple(analysis.revisions), (3, 5, 7, 8))
        self.assertEqual(analysis.start_revision, 3)
        self.assertEqual(analysis.last_revision, 8)
        self.assertEqual(set(analysis.authors), {"creator", "bob", "carol", "dan"})
        result = migrator.migrate()
        self.assertEqual(tuple(result.imported_revisions), (3, 5, 7, 8))
        self.assertEqual(result.commit_count, 4)
        self._assert_tree(result.repo_path)
        svn_entries = ET.fromstring(self._svn(
            "log", "--xml", "--stop-on-copy", "-r", "8:1",
            self.config.svn_url + "@8").stdout).findall("logentry")
        expected_metadata = {int(e.attrib["revision"]): e for e in svn_entries}
        commits = command("git", "-C", result.repo_path, "rev-list", "HEAD").stdout.splitlines()
        seen = set()
        for commit in commits:
            values = command("git", "-C", result.repo_path, "show", "-s",
                             "--format=%an%x00%aI%x00%B", commit).stdout.split("\x00", 2)
            name, timestamp, message = values
            match = re.search(r"git-svn-id: .+@(\d+) [\w-]+", message)
            self.assertIsNotNone(match, message)
            revision = int(match.group(1))
            seen.add(revision)
            entry = expected_metadata[revision]
            self.assertEqual(name, entry.findtext("author"))
            svn_date = dt.datetime.fromisoformat(entry.findtext("date").replace("Z", "+00:00"))
            git_date = dt.datetime.fromisoformat(timestamp)
            self.assertEqual(int(svn_date.timestamp()), int(git_date.timestamp()))
            self.assertTrue(message.startswith(entry.findtext("msg")))
        self.assertEqual(seen, {3, 5, 7, 8})

    def test_rerun_is_idempotent_and_publish_is_explicit(self):
        first = self.migrator().migrate()
        self.assertEqual(command("git", "--git-dir", self.target, "show-ref",
                                 check=False).returncode, 1)
        second = self.migrator().migrate()
        self.assertEqual(first.head_sha, second.head_sha)
        self.assertEqual(first.commit_count, second.commit_count)
        published = self.migrator().publish()
        self.assertFalse(published.already_present)
        remote_sha = command("git", "--git-dir", self.target, "rev-parse",
                             "refs/heads/import/target").stdout.strip()
        self.assertEqual(remote_sha, second.head_sha)
        self.assertTrue(self.migrator().publish().already_present)

    def test_existing_different_remote_branch_is_never_overwritten(self):
        result = self.migrator().migrate()
        other = self.root / "other"
        command("git", "init", other)
        (other / "file.txt").write_text("contenuto da preservare\n", encoding="utf-8")
        command("git", "-C", other, "add", ".")
        command("git", "-C", other, "commit", "-m", "existing unrelated history")
        command("git", "-C", other, "push", str(self.target), "HEAD:refs/heads/import/target")
        original = command("git", "--git-dir", self.target, "rev-parse",
                           "refs/heads/import/target").stdout.strip()
        self.assertNotEqual(original, result.head_sha)
        with self.assertRaises(MigrationError):
            self.migrator().publish()
        after = command("git", "--git-dir", self.target, "rev-parse",
                        "refs/heads/import/target").stdout.strip()
        self.assertEqual(original, after)

    def test_pre_cancelled_job_can_be_resumed(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(MigrationCancelled):
            self.migrator(cancelled).migrate()
        result = self.migrator().migrate()
        self.assertEqual(tuple(result.imported_revisions), (3, 5, 7, 8))

    def test_stop_after_first_imported_revision_and_resume(self):
        cancelled = threading.Event()
        stopped_at = []

        def stop_on_first_commit(event):
            self.events.append(event)
            if event.get("type") == "progress" and event.get("current", 0) >= 1 and not stopped_at:
                stopped_at.append(event["current"])
                cancelled.set()

        migrator = Migrator(self.config, self.root / "migration", stop_on_first_commit, cancelled,
                            engine="git-svn", history_scope="path")
        with self.assertRaises(MigrationCancelled):
            migrator.migrate()
        self.assertEqual(stopped_at, [1])
        result = self.migrator().migrate()
        self.assertEqual(tuple(result.imported_revisions), (3, 5, 7, 8))
        self.assertEqual(result.commit_count, 4)
        self._assert_tree(result.repo_path)

    def test_property_only_and_empty_directory_revisions(self):
        self._svn("update", self.wc)
        self._svn("propset", "custom:review", "approvato", self.branch)
        self._svn("commit", self.wc, "-m", "solo proprietà", "--username", "reviewer")
        self._svn("mkdir", self.branch / "empty-directory")
        self._svn("commit", self.wc, "-m", "directory vuota", "--username", "organizer")
        self.config = replace(self.config, revision_max=11)
        migrator = self.migrator()
        analysis = migrator.analyze()
        self.assertEqual(tuple(analysis.revisions), (3, 5, 7, 8, 9, 10, 11))
        result = migrator.migrate()
        self.assertEqual(tuple(result.imported_revisions), (3, 5, 7, 8, 9, 10, 11))
        self.assertEqual(result.commit_count, 7)
        self._assert_tree(result.repo_path, revision=11)


class SparsePlanningTests(unittest.TestCase):
    def test_sparse_plan_covers_every_relevant_revision_without_spanning_huge_gaps(self):
        revisions = (1, 2, 500, 500_000, 500_003, 8_000_000)
        ranges = plan_revision_ranges(revisions, max_window=100_000)
        for revision in revisions:
            self.assertTrue(any(start <= revision <= end for start, end in ranges))
        self.assertTrue(all(end - start < 100_000 for start, end in ranges))
        self.assertLess(sum(end - start + 1 for start, end in ranges), 100_000)
        self.assertEqual(list(ranges), sorted(ranges))


if __name__ == "__main__":
    unittest.main(verbosity=2)
