"""Storia ereditata: prove reali locali, copie storiche e ripresa nativa."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration_core import MigrationCancelled, MigrationConfig, Migrator
import test_integration as integration

command = integration.command


@unittest.skipUnless(all(shutil.which(name) for name in ("git", "svn", "svnadmin")),
                     "Requires git, svn and svnadmin")
class NativeLineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="svn-git-lineage-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.wc = self.root / "checkout"
        self.target = self.root / "destination.git"
        command("svnadmin", "create", self.source)
        command("git", "init", "--bare", self.target)
        self.url = self.source.as_uri()
        self.events = []

    def tearDown(self):
        self.temporary.cleanup()

    def _svn(self, *args):
        return command("svn", "--non-interactive", *args)

    def _commit(self, message, author):
        self._svn("commit", self.wc, "-m", message, "--username", author)

    def _migrator(self, callback=None, cancel=None):
        # Le nuove migrazioni, anche fuori da Windows, seguono le copie.
        return Migrator(self.config, self.root / "migration",
                        callback or self.events.append, cancel)

    def _historical_chain(self):
        self._svn("mkdir", self.url + "/original", self.url + "/branches",
                  self.url + "/unrelated", "-m", "layout", "--username", "layout")  # r1
        self._svn("checkout", self.url, self.wc)
        nested = self.wc / "original/nested"
        nested.mkdir()
        (nested / "value.txt").write_text("origine\n", encoding="utf-8")
        (nested / "keep.txt").write_text("da rinominare\n", encoding="utf-8")
        (nested / "caffè-東京.txt").write_text("storia ereditata\n", encoding="utf-8")
        (nested / "data.bin").write_bytes(bytes(range(256)))
        self._svn("add", nested)
        self._commit("prima storia del sottopercorso", "origin")  # r2
        (nested / "value.txt").write_text("modifica esclusa 3\n", encoding="utf-8")
        self._commit("cambiamento della sorgente escluso", "excluded-three")  # r3
        (nested / "excluded.txt").write_text("non deve entrare in Git\n", encoding="utf-8")
        self._svn("add", nested / "excluded.txt")
        self._commit("altra modifica sorgente esclusa", "excluded-four")  # r4
        self._svn("copy", self.url + "/original@2", self.url + "/branches/old",
                  "-m", "copia da revisione vecchia", "--username", "copy-old")  # r5
        self._svn("update", self.wc)
        old = self.wc / "branches/old/nested"
        (old / "value.txt").write_text("ramo precedente\n", encoding="utf-8")
        self._svn("move", old / "keep.txt", old / "renamed.txt")
        self._commit("modifica e rinomina sul ramo precedente", "branch-author")  # r6
        self._svn("copy", self.url + "/branches/old@6", self.url + "/branches/middle",
                  "-m", "seconda copia del contenitore", "--username", "copy-middle")  # r7
        self._svn("update", self.wc)
        sibling = self.wc / "branches/middle/sibling.txt"
        sibling.write_text("fratello estraneo al sottopercorso\n", encoding="utf-8")
        self._svn("add", sibling)
        self._commit("modifica fuori dal sottopercorso", "excluded-sibling")  # r8
        self._svn("move", self.url + "/branches/middle", self.url + "/branches/final",
                  "-m", "rinomina del contenitore", "--username", "rename-author")  # r9
        self._svn("update", self.wc)
        final = self.wc / "branches/final/nested"
        (final / "value.txt").write_text("ramo finale\n", encoding="utf-8")
        self._svn("delete", final / "data.bin")
        self._commit("ultima modifica richiesta", "final-author")  # r10
        self._svn("delete", self.url + "/original", self.url + "/branches/old",
                  self.url + "/branches/final", "-m", "eliminati dopo il limite",
                  "--username", "after-max")  # r11
        self.config = MigrationConfig(self.url + "/branches/final/nested", 10,
                                      str(self.target), "import/full-history")

    def _assert_all_commits(self, result, analysis):
        commits = command("git", "-C", result.repo_path, "rev-list", "--reverse", "HEAD").stdout.splitlines()
        self.assertEqual(len(commits), len(analysis.revisions))
        expected_log = ET.fromstring(self._svn(
            "log", "--xml", "-r", f"{self.config.revision_max}:1",
            self.config.svn_url + "@" + str(self.config.revision_max)).stdout)
        expected_metadata = {int(entry.attrib["revision"]): entry
                             for entry in expected_log.findall("logentry")}
        for commit, location in zip(commits, analysis.locations):
            revision = location.revision
            name, timestamp, message = command("git", "-C", result.repo_path, "show", "-s",
                "--format=%an%x00%aI%x00%B", commit).stdout.split("\x00", 2)
            entry = expected_metadata[revision]
            self.assertEqual(name, entry.findtext("author"))
            source_date = dt.datetime.fromisoformat(entry.findtext("date").replace("Z", "+00:00"))
            self.assertEqual(int(dt.datetime.fromisoformat(timestamp).timestamp()), int(source_date.timestamp()))
            self.assertTrue(message.startswith(entry.findtext("msg")))
            self.assertRegex(message, re.escape(f"git-svn-id: {location.url}@{revision} {analysis.source_uuid}") + r"\s*$")

            expected = self.root / f"expected-{revision}"
            self._svn("export", "--ignore-externals", "-r", str(revision),
                      location.url + "@" + str(revision), expected)
            expected_paths = {path.relative_to(expected).as_posix()
                              for path in expected.rglob("*") if path.is_file()}
            actual_paths = command("git", "-C", result.repo_path, "ls-tree", "-rz", "--name-only",
                                   commit, text=False).stdout.split(b"\0")
            self.assertEqual({path.decode("utf-8") for path in actual_paths if path}, expected_paths)
            for relative in expected_paths:
                actual = command("git", "-C", result.repo_path, "show", f"{commit}:{relative}", text=False).stdout
                self.assertEqual(actual, (expected / relative).read_bytes(), f"r{revision}: {relative}")

    def test_parent_copies_old_source_revision_rename_and_deleted_head(self):
        self._historical_chain()
        migrator = self._migrator()
        analysis = migrator.analyze()
        self.assertEqual(analysis.history_scope, "lineage")
        self.assertEqual(analysis.revisions, (2, 5, 6, 7, 9, 10))
        self.assertEqual(analysis.start_revision, 2)
        self.assertEqual(analysis.branch_start_revision, 9)
        self.assertEqual(analysis.copy_boundaries, 3)
        self.assertEqual(tuple(location.repository_path for location in analysis.locations), (
            "/original/nested", "/branches/old/nested", "/branches/old/nested",
            "/branches/middle/nested", "/branches/final/nested", "/branches/final/nested"))
        self.assertEqual(set(analysis.authors), {
            "origin", "copy-old", "branch-author", "copy-middle", "rename-author", "final-author"})
        result = migrator.migrate()
        self.assertEqual(result.imported_revisions, analysis.revisions)
        self._assert_all_commits(result, analysis)
        self.assertEqual(self._migrator().migrate().head_sha, result.head_sha)
        self.assertFalse(self._migrator().publish().already_present)
        self.assertTrue(self._migrator().publish().already_present)

    def test_resume_between_copy_segments(self):
        self._historical_chain()
        cancelled = threading.Event()

        def stop_after_middle_copy(event):
            self.events.append(event)
            if event.get("type") == "progress" and event.get("current") == 4:
                cancelled.set()

        with self.assertRaises(MigrationCancelled):
            self._migrator(stop_after_middle_copy, cancelled).migrate()
        partial = self.root / "migration/repository"
        self.assertEqual(command("git", "-C", partial, "rev-list", "--count", "refs/remotes/svn-source").stdout.strip(), "4")
        resumed = self._migrator()
        result = resumed.migrate()
        analysis = resumed._load_analysis()
        self.assertEqual(result.imported_revisions, (2, 5, 6, 7, 9, 10))
        self._assert_all_commits(result, analysis)

    def test_same_path_replacement_from_old_revision(self):
        self._svn("mkdir", self.url + "/trunk", "-m", "create trunk", "--username", "creator")  # r1
        self._svn("checkout", self.url, self.wc)
        trunk = self.wc / "trunk"
        (trunk / "file.txt").write_text("versione iniziale\n", encoding="utf-8")
        self._svn("add", trunk / "file.txt")
        self._commit("seed", "alice")  # r2
        (trunk / "file.txt").write_text("versione scartata\n", encoding="utf-8")
        self._commit("later omitted change", "bob")  # r3
        (trunk / "omitted.txt").write_text("escluso\n", encoding="utf-8")
        self._svn("add", trunk / "omitted.txt")
        self._commit("later omitted file", "carol")  # r4
        self._svn("update", self.wc)
        self._svn("delete", trunk)
        self._svn("copy", self.url + "/trunk@2", trunk)
        self._commit("replace with old incarnation", "restorer")  # r5
        self.config = MigrationConfig(self.url + "/trunk", 5, str(self.target), "import/replaced")
        migrator = self._migrator()
        analysis = migrator.analyze()
        self.assertEqual(analysis.revisions, (1, 2, 5))
        self.assertEqual(tuple(location.segment for location in analysis.locations), (1, 1, 0))
        self.assertEqual(len({location.url for location in analysis.locations}), 1)
        result = migrator.migrate()
        self._assert_all_commits(result, analysis)
        self.assertEqual(result.imported_revisions, (1, 2, 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
