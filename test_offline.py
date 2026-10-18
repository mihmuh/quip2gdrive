"""Offline tests with a fake Quip service: no network, no Keychain.

    .venv/bin/python -m unittest test_offline -v
"""
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import quip_export
import secrets_setup
from quip_export import QuipClient, QuipError


def meta(tid, kind, updated=1):
    return {"thread": {"id": tid, "title": f"Title {tid}", "type": kind, "updated_usec": updated}}


class FakeQuip:
    def __init__(self):
        self.updated = {"t1": 1, "t2": 1, "t3": 1, "t4": 1}
        self.kinds = {"t1": "document", "t2": "spreadsheet", "t3": "slides", "t4": "document"}
        self.calls = []

    def get(self, path, params=None, raw=False):
        self.calls.append(path)
        folders = {
            "/1/folders/P": {"folder": {"title": "Private"},
                             "children": [{"thread_id": "t1"}, {"folder_id": "F1"}, {"folder_id": "X", "restricted": True}]},
            "/1/folders/F1": {"folder": {"title": "Projects"}, "children": [{"thread_id": "t2"}, {"thread_id": "t3"}]},
            "/1/folders/D": {"folder": {"title": "Desktop"}, "children": [{"folder_id": "P"}, {"thread_id": "t1"}]},
        }
        if path == "/1/users/current":
            return {"private_folder_id": "P", "desktop_folder_id": "D", "shared_folder_ids": ["S"],
                    "group_folder_ids": []}
        if path in folders:
            return folders[path]
        if path == "/1/folders/S":
            raise QuipError(403, path)
        if path == "/1/threads/recent":
            return {"t4": meta("t4", "document", self.updated["t4"])}
        if path == "/1/threads/":
            return {t: meta(t, self.kinds[t], self.updated[t]) for t in params["ids"].split(",")}
        if path == "/1/threads/t4/export/docx":
            raise QuipError(500, path)
        if path == "/1/threads/t4":
            return {"html": "<h1>fallback</h1>"}
        if path.startswith("/1/threads/") and "/export/" in path:
            return f"bytes of {path}".encode()
        raise AssertionError(f"unexpected call {path}")


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def tree_files(top):
    return sorted(os.path.relpath(os.path.join(r, n), top) for r, _, ns in os.walk(top) for n in ns)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = os.path.join(self.tmp.name, "staging")
        self.quip = FakeQuip()
        self.old_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask)
        self.tmp.cleanup()

    def export(self):
        return quiet(quip_export.run, self.quip, self.staging, top_name="Top")

    def test_export_builds_upload_tree(self):
        counts = self.export()
        self.assertEqual(counts, {"exported": 3, "unchanged": 0, "unsupported": 1, "failed": 0})
        top = os.path.join(self.staging, "upload", "Top")
        # t1 sits in Private and Desktop: first folder wins. t4 is in no folder; its docx export
        # failed, so it falls back to HTML. t3 (slides) is skipped.
        self.assertEqual(tree_files(top), ["Private/Projects/Title t2.xlsx",
                                           "Private/Title t1.docx",
                                           "_Unfiled/Title t4.html"])
        self.assertNotIn("/1/folders/X", self.quip.calls)  # restricted folders are skipped
        self.assertEqual(stat.S_IMODE(os.stat(self.staging).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(top, "Private", "Title t1.docx")).st_mode), 0o600)

    def test_rerun_skips_unchanged_and_refetches_edited(self):
        self.export()
        self.quip.updated["t1"] = 2
        counts = self.export()
        self.assertEqual((counts["exported"], counts["unchanged"]), (1, 2))


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = self.tmp.name
        os.makedirs(os.path.join(self.staging, "files"))

    def tearDown(self):
        self.tmp.cleanup()

    def thread(self, tid, title, folder, fmt="docx", status="exported"):
        rel = os.path.join("files", f"{tid}.{fmt}")
        with open(os.path.join(self.staging, rel), "w") as f:
            f.write(tid)
        return {"title": title, "folder": folder, "file": rel, "format": fmt, "status": status}

    def test_names_are_safe_and_unique(self):
        manifest = {
            "folders": {"P": {"title": "Private", "parent": None},
                        "F": {"title": "Q1/Q2: plans", "parent": "P"},
                        "G": {"title": "private", "parent": None}},  # clashes with "Private" ignoring case
            "threads": {"a": self.thread("a", "Roadmap", "F"),
                        "b": self.thread("b", "roadmap", "F"),
                        "c": self.thread("c", "Budget", "F", fmt="xlsx"),
                        "d": self.thread("d", ".hidden/name", None),
                        "e": self.thread("e", "Deck", "P", status="unsupported"),
                        "f": self.thread("f", "x" * 300, "G")},
        }
        top, count = quip_export.write_tree(self.staging, manifest, "Top")
        files = tree_files(top)
        self.assertEqual(count, 5)
        self.assertEqual(files[:4], ["Private/Q1-Q2- plans/Budget.xlsx",
                                     "Private/Q1-Q2- plans/Roadmap.docx",
                                     "Private/Q1-Q2- plans/roadmap (2).docx",
                                     "_Unfiled/hidden-name.docx"])
        self.assertTrue(files[4].startswith("private (2)/xxx"))
        self.assertLessEqual(len(os.path.basename(files[4]).encode()), 255)
        src = os.path.join(self.staging, "files", "a.docx")
        self.assertTrue(os.path.samefile(src, os.path.join(top, "Private", "Q1-Q2- plans", "Roadmap.docx")))

    def test_rebuild_replaces_previous_layout(self):
        manifest = {"folders": {}, "threads": {"a": self.thread("a", "Old title", None)}}
        quip_export.write_tree(self.staging, manifest, "Top")
        manifest["threads"]["a"]["title"] = "New title"
        top, _ = quip_export.write_tree(self.staging, manifest, "Top")
        self.assertEqual(os.listdir(os.path.join(top, "_Unfiled")), ["New title.docx"])
        self.assertTrue(os.path.isfile(os.path.join(self.staging, "files", "a.docx")))
        self.assertFalse(os.path.exists(os.path.join(self.staging, "upload.partial")))


class SecretsTest(unittest.TestCase):
    def test_client_retries_429_and_errors_never_contain_token(self):
        def response(status, body=None):
            r = mock.Mock(status_code=status, headers={}, content=b"x")
            r.json.return_value = body or {}
            return r

        client = QuipClient("SECRET-TOKEN-123", max_attempts=3)
        client.session.get = mock.Mock(side_effect=[response(429), response(404, {"error_description": "Not found"})])
        err = io.StringIO()
        with mock.patch("quip_export.time.sleep"), redirect_stderr(err):
            with self.assertRaises(QuipError) as ctx:
                client.get("/1/threads/abc")
        self.assertEqual(client.session.get.call_count, 2)
        self.assertNotIn("SECRET-TOKEN-123", str(ctx.exception) + err.getvalue())

    def test_set_prints_instructions_and_never_echoes_token(self):
        out = io.StringIO()
        with mock.patch("secrets_setup.getpass.getpass", return_value=" tok-SECRET \n"), \
                mock.patch("secrets_setup.keyring.set_password") as store, redirect_stdout(out):
            secrets_setup.cmd_set()
        self.assertIn("https://quip.com/dev/token", out.getvalue())
        self.assertNotIn("tok-SECRET", out.getvalue())
        store.assert_called_once_with(secrets_setup.SERVICE, secrets_setup.QUIP_TOKEN, "tok-SECRET")


if __name__ == "__main__":
    unittest.main()
