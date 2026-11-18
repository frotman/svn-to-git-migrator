"""Lineage planning and a real comparison with SVN's own peg resolution."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history_lineage import (  # noqa: E402
    HistoryLineageError,
    analyze_lineage,
    historical_url,
)


def changed(path, action="M", source=None, source_revision=None):
    attrs = {"action": action, "kind": "dir"}
    if source is not None:
        attrs["copyfrom-path"] = source
    if source_revision is not None:
        attrs["copyfrom-rev"] = str(source_revision)
    return path, attrs


class HistoryLineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="svn-lineage-plan-")
        self.addCleanup(self.temporary.cleanup)
        self.log = Path(self.temporary.name) / "history.xml"

    def plan(self, entries, branch="/branches/new", maximum=None):
        root = ET.Element("log")
        for revision, changes in entries:
            entry = ET.SubElement(root, "logentry", revision=str(revision))
            if changes is not None:
                paths = ET.SubElement(entry, "paths")
                for path, attrs in changes:
                    ET.SubElement(paths, "path", **attrs).text = path
        ET.ElementTree(root).write(self.log, encoding="utf-8", xml_declaration=True)
        return analyze_lineage(self.log, branch, "https://example.test/repo%20root",
                               revision_max=maximum)

    def test_ancestry_chain_uses_copy_revision_and_destination_at_boundary(self):
        plan = self.plan([
            (12, [changed("/branches/new/file")]),
            (10, [changed("/branches/new", "A", "/branches/old", 7)]),
            (7, [changed("/branches/old", "A", "/trunk", 5)]),
            (5, [changed("/trunk/file")]),
            (1, [changed("/trunk", "A")]),
        ], maximum=13)
        self.assertEqual(plan.revisions, (1, 5, 7, 10, 12))
        self.assertEqual([entry.repository_path for entry in plan.locations],
                         ["/trunk", "/trunk", "/branches/old",
                          "/branches/new", "/branches/new"])
        self.assertEqual([entry.segment for entry in plan.locations], [2, 2, 1, 0, 0])
        self.assertEqual(plan.branch_start_revision, 10)
        self.assertEqual(plan.copy_from_path, "/branches/old")
        self.assertEqual(plan.copy_from_revision, 7)
        self.assertEqual(plan.copy_boundaries, 2)

    def test_parent_copy_preserves_suffix_and_encodes_actual_names(self):
        plan = self.plan([
            (8, [changed("/new parent", "A", "/old parent", 4)]),
            (3, [changed("/old parent/sub%name/é@1", "A")]),
        ], branch="/new parent/sub%name/é@1")
        self.assertEqual(plan.locations[0].repository_path, "/old parent/sub%name/é@1")
        self.assertEqual(plan.locations[0].url,
                         "https://example.test/repo%20root/old%20parent/sub%25name/%C3%A9%401")
        self.assertEqual(plan.copy_from_path, "/old parent/sub%name/é@1")
        self.assertEqual(plan.copy_from_revision, 4)

    def test_child_origin_overrides_copied_parent_in_same_commit(self):
        plan = self.plan([
            (8, [changed("/new", "A", "/old", 7),
                 changed("/new/child", "A")]),
        ], branch="/new/child")
        self.assertEqual(plan.revisions, (8,))
        self.assertEqual(plan.branch_start_revision, 8)
        self.assertEqual(plan.copy_boundaries, 0)
        self.assertIsNone(plan.copy_from_path)

    def test_same_path_replacement_has_a_distinct_segment(self):
        plan = self.plan([
            (10, [changed("/same", "R", "/same", 3)]),
            (3, [changed("/same/file")]),
            (1, [changed("/same", "A")]),
        ], branch="/same")
        self.assertEqual([entry.repository_path for entry in plan.locations], ["/same"] * 3)
        self.assertEqual([entry.segment for entry in plan.locations], [1, 1, 0])
        self.assertEqual(plan.copy_boundaries, 1)

    def test_copying_a_child_does_not_redirect_root_history(self):
        plan = self.plan([
            (7, [changed("/branch/child", "A", "/other/project", 5)]),
            (3, [changed("/branch", "A")]),
        ], branch="/branch")
        self.assertEqual(plan.copy_boundaries, 0)
        self.assertEqual([entry.repository_path for entry in plan.locations], ["/branch"] * 2)

    def test_repository_root_has_implicit_origin_at_zero(self):
        plan = self.plan([
            (3, [changed("/folder/file")]),
            (1, [changed("/folder", "A")]),
        ], branch="/")
        self.assertEqual(plan.branch_start_revision, 0)
        self.assertEqual(plan.revisions, (1, 3))
        self.assertEqual(plan.copy_boundaries, 0)

    def test_uninherited_revisions_newer_than_copy_source_are_rejected(self):
        with self.assertRaisesRegex(HistoryLineageError, "supera"):
            self.plan([
                (10, [changed("/branches/new", "A", "/old", 5)]),
                (8, [changed("/old/file")]),
                (1, [changed("/old", "A")]),
            ])

    def test_incomplete_or_inconsistent_history_is_rejected(self):
        cases = {
            "missing_creation": [(3, [changed("/branches/new/file")])],
            "missing_paths": [(3, None)],
            "empty_paths": [(3, [])],
            "partial_copy_path": [(3, [changed("/branches/new", "A", "/old")])],
            "partial_copy_revision": [(3, [changed("/branches/new", "A", source_revision=2)])],
            "copy_from_future": [(3, [changed("/branches/new", "A", "/old", 4)])],
            "copy_from_itself": [(3, [changed("/branches/new", "A", "/old", 3)])],
            "unrelated_only": [(3, [changed("/elsewhere", "A")])],
            "deleted_ancestor": [(3, [changed("/branches", "D")])],
            "older_than_origin": [(3, [changed("/branches/new", "A")]),
                                  (2, [changed("/branches/new/file")])],
            "duplicate_revision": [(3, [changed("/branches/new/file")]),
                                   (3, [changed("/branches/new", "A")])],
            "ascending_order": [(2, [changed("/branches/new/file")]),
                                (3, [changed("/branches/new", "A")])],
        }
        for name, entries in cases.items():
            with self.subTest(name=name), self.assertRaises(HistoryLineageError):
                self.plan(entries)

    def test_revision_max_is_enforced(self):
        with self.assertRaises(HistoryLineageError):
            self.plan([(3, [changed("/branches/new", "A")])], maximum=2)

    def test_wrong_document_and_nested_merge_history_are_rejected(self):
        documents = (
            '<info><logentry revision="3"><paths><path action="A">'
            '/branch</path></paths></logentry></info>',
            '<log><logentry revision="3"><paths><path action="A">'
            '/branch</path></paths><logentry revision="2"><paths>'
            '<path action="A">/other</path></paths></logentry></logentry></log>',
        )
        for document in documents:
            with self.subTest(document=document), self.assertRaises(HistoryLineageError):
                self.log.write_text(document, encoding="utf-8")
                analyze_lineage(self.log, "/branch", "https://example.test/repo")

    def test_url_composition_handles_file_scheme_and_literal_percent(self):
        self.assertEqual(historical_url("file:///", "/project a/100%"),
                         "file:///project%20a/100%25")
        self.assertEqual(historical_url("file:///tmp/repo%20a/", "/"),
                         "file:///tmp/repo%20a")
        for bad in ("relative", "/with//double", "/with/../parent", "/ending/"):
            with self.subTest(path=bad), self.assertRaises(HistoryLineageError):
                historical_url("https://example.test/repo", bad)


def command(*args):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C.UTF-8"
    return subprocess.run([str(arg) for arg in args], check=True, env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=60)


@unittest.skipUnless(all(shutil.which(name) for name in ("svn", "svnadmin")),
                     "Requires svn and svnadmin")
class RealHistoryLocationTests(unittest.TestCase):
    def test_copies_of_parent_directories_match_svn_peg_resolution(self):
        with tempfile.TemporaryDirectory(prefix="svn-lineage-real-") as temporary:
            root = Path(temporary)
            repository = root / "source repo"
            checkout = root / "wc"
            command("svnadmin", "create", repository)
            url = repository.as_uri()

            def svn(*args):
                return command("svn", "--non-interactive", *args)

            svn("mkdir", url + "/trunk", url + "/branches", url + "/unrelated",
                "-m", "r1 layout")
            svn("checkout", url, checkout)
            project = checkout / "trunk/project a"
            project.mkdir()
            document = project / "file.txt"
            document.write_text("version one\n", encoding="utf-8")
            svn("add", project)
            svn("commit", checkout, "-m", "r2 project origin")
            document.write_text("version two\n", encoding="utf-8")
            svn("commit", checkout, "-m", "r3 inherited edit")
            noise = checkout / "unrelated/noise.txt"
            noise.write_text("irrelevant\n", encoding="utf-8")
            svn("add", noise)
            svn("commit", checkout, "-m", "r4 unrelated")
            svn("copy", url + "/trunk@3", url + "/branches/first", "-r", "3",
                "-m", "r5 copy parent at an older revision")
            document.write_text("not inherited\n", encoding="utf-8")
            svn("commit", checkout, "-m", "r6 later source change")
            svn("copy", url + "/branches/first@5", url + "/branches/final", "-r", "5",
                "-m", "r7 second parent copy")
            svn("update", checkout)
            final_document = checkout / "branches/final/project a/file.txt"
            final_document.write_text("final content\n", encoding="utf-8")
            svn("commit", checkout, "-m", "r8 final edit")
            svn("move", url + "/branches/final", url + "/branches/final%20renamed",
                "-m", "r9 move parent")

            branch = "/branches/final renamed/project a"
            final_url = historical_url(url, branch)
            log = root / "history.xml"
            log.write_text(svn("log", "--xml", "--verbose", "-r", "9:1",
                               final_url + "@9").stdout, encoding="utf-8")
            plan = analyze_lineage(log, branch, url, revision_max=9)
            self.assertEqual(plan.revisions, (2, 3, 5, 7, 8, 9))
            self.assertEqual(plan.branch_start_revision, 9)
            self.assertEqual(plan.copy_boundaries, 3)
            for location in plan.locations:
                with self.subTest(revision=location.revision):
                    entry = ET.fromstring(svn("info", "--xml", "-r", str(location.revision),
                                             final_url + "@9").stdout).find("entry")
                    self.assertIsNotNone(entry)
                    self.assertEqual(location.repository_path,
                                     unquote(entry.findtext("relative-url"))[1:])
                    self.assertEqual(unquote(location.url), unquote(entry.findtext("url")))


if __name__ == "__main__":
    unittest.main()
