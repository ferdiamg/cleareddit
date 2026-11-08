import io
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cleareddit import ClearRedditError, RANDOM_WORDS, SessionClient, add_ids, authorized_client, database, import_path, item_content, main, random_replacement, run_account


class FakeClient:
    def __init__(self, data, edit_fails=None):
        self.data = data
        self.edit_fails = set(edit_fails or [])
        self.calls = []

    def info(self, ids):
        self.calls.append(("info", tuple(ids)))
        return {key: self.data[key] for key in ids if key in self.data}

    def edit(self, fullname, replacement):
        self.calls.append(("edit", fullname, replacement))
        if fullname in self.edit_fails:
            raise ClearRedditError("archived")

    def delete(self, fullname):
        self.calls.append(("delete", fullname))


class ClearRedditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".cleareddit"
        self.db = database(self.root)
        self.addCleanup(self.db.close)

    def test_import_zip_deduplicates_and_separates_accounts(self):
        archive = Path(self.temp.name) / "export.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("reddit/comments.csv", "id,body\na1,secret\na1,other\n")
            z.writestr("reddit/posts.csv", "id,title\nb2,secret title\n")
        self.assertEqual(import_path(self.db, "alice", archive),
                         [("comments.csv", 2, 1), ("posts.csv", 1, 1)])
        self.assertEqual(import_path(self.db, "alice", archive),
                         [("comments.csv", 2, 0), ("posts.csv", 1, 0)])
        self.assertEqual(import_path(self.db, "bob", archive),
                         [("comments.csv", 2, 1), ("posts.csv", 1, 1)])
        self.assertEqual(self.db.execute("SELECT DISTINCT fullname FROM items ORDER BY fullname").fetchall(),
                         [("t1_a1",), ("t3_b2",)])
        self.assertNotIn("secret", Path(self.root / "state.sqlite3").read_bytes().decode("latin1"))

    def test_run_checks_author_and_resumes(self):
        add_ids(self.db, "alice", "comments", ["a1", "a2", "a3"], "listing")
        client = FakeClient({
            "t1_a1": {"name": "t1_a1", "author": "alice"},
            "t1_a2": {"name": "t1_a2", "author": "bob"},
        }, edit_fails={"t1_a1"})
        counts = run_account(self.db, client, "alice", mode="overwrite-delete",
                             replacement="test replacement", kinds={"comments"}, limit=None)
        self.assertEqual(counts["deleted"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["missing"], 1)
        self.assertEqual([c for c in client.calls if c[0] == "delete"], [("delete", "t1_a1")])
        client.data["t1_a2"]["author"] = "alice"
        again = run_account(self.db, client, "alice", mode="delete",
                            replacement="test replacement", kinds={"comments"}, limit=None)
        self.assertEqual(again["deleted"], 1)
        self.assertEqual([c for c in client.calls if c[0] == "delete"],
                         [("delete", "t1_a1"), ("delete", "t1_a2")])

    def test_link_post_is_deleted_without_edit(self):
        add_ids(self.db, "alice", "posts", ["abc"], "export")
        client = FakeClient({"t3_abc": {"name": "t3_abc", "author": "alice", "is_self": False}})
        run_account(self.db, client, "alice", mode="overwrite-delete", replacement="test replacement",
                    kinds={"posts"}, limit=None)
        self.assertEqual([c[0] for c in client.calls], ["info", "delete"])

    def test_default_replacement_is_random_per_item(self):
        for _ in range(20):
            replacement = random_replacement()
            words = replacement.split()
            self.assertIn(len(words), (4, 5))
            self.assertGreaterEqual(len(replacement), 30)
            self.assertEqual(len(words), len(set(words)))
            self.assertTrue(set(words) <= set(RANDOM_WORDS))

        add_ids(self.db, "alice", "comments", ["a1", "a2"], "listing")
        client = FakeClient({
            "t1_a1": {"name": "t1_a1", "author": "alice"},
            "t1_a2": {"name": "t1_a2", "author": "alice"},
        })
        with patch("cleareddit.random_replacement", side_effect=["example replacement text number one", "example replacement text number two"]):
            run_account(self.db, client, "alice", mode="overwrite-delete", replacement=None,
                        kinds={"comments"}, limit=None)
        self.assertEqual([call[2] for call in client.calls if call[0] == "edit"],
                         ["example replacement text number one", "example replacement text number two"])

    def test_run_displays_original_content_without_storing_it(self):
        add_ids(self.db, "alice", "comments", ["abc"], "listing")
        client = FakeClient({"t1_abc": {
            "name": "t1_abc", "author": "alice", "body": "First line\nsecond line\x1b[31m",
        }})
        output = io.StringIO()
        with redirect_stdout(output):
            run_account(self.db, client, "alice", mode="delete", replacement=None,
                        kinds={"comments"}, limit=None, full_content=True)
        self.assertIn("First line\n  second line", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("First line", Path(self.root / "state.sqlite3").read_bytes().decode("latin1"))
        self.assertEqual(item_content({"body": "A " * 200}, "comments")[-1], "…")

    def test_run_without_yes_does_not_authenticate(self):
        self.assertEqual(main(["--state-dir", str(self.root), "init", "--client-id", "test",
                               "--contact-user", "alice"]), 0)
        add_ids(self.db, "alice", "comments", ["abc"], "export")
        self.assertEqual(main(["--state-dir", str(self.root), "run", "--account", "alice"]), 0)
        self.assertEqual(main(["--state-dir", str(self.root), "run", "--yes"]), 1)

    def test_offline_import_and_manual_mark_without_init(self):
        export = Path(self.temp.name) / "comments.csv"
        export.write_text("id,permalink,body\nxyz,/r/test/comments/abc/title/xyz/,private\n", encoding="utf-8")
        self.assertEqual(main(["--state-dir", str(self.root), "import", "--account", "bob", str(export)]), 0)
        self.assertEqual(main(["--state-dir", str(self.root), "queue", "--account", "bob"]), 0)
        row = self.db.execute("SELECT url FROM items WHERE account='bob'").fetchone()
        self.assertEqual(row, ("https://www.reddit.com/r/test/comments/abc/title/xyz/",))
        self.assertEqual(main(["--state-dir", str(self.root), "mark", "--account", "bob", "t1_xyz"]), 0)
        self.assertEqual(self.db.execute("SELECT status FROM items WHERE account='bob'").fetchone(),
                         ("manual_done",))

    def test_session_client_uses_cookie_modhash_and_account_check(self):
        config = {"sessions": {"alice": {"reddit_session": "secret-token"}}}
        requests = []

        class Response:
            headers = {}

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        def open_request(client, request):
            requests.append(request)
            if "/api/me.json" in request.full_url:
                return Response(b'{"data":{"name":"alice","modhash":"csrf-token"}}')
            if "/api/del" in request.full_url:
                return Response(b"")
            raise AssertionError(request.full_url)

        with patch("cleareddit.SessionClient._open", open_request):
            client = authorized_client(self.root, config, "alice")
            client.delay = 0
            client.delete("t1_abc")
        self.assertIsInstance(client, SessionClient)
        self.assertEqual(requests[1].get_header("Cookie"), "reddit_session=secret-token")
        self.assertEqual(requests[1].get_header("X-modhash"), "csrf-token")
        self.assertEqual(requests[1].data, b"id=t1_abc")

    def test_session_scan_and_info_paths(self):
        responses = {
            "/api/me.json": b'{"data":{"name":"alice","modhash":"csrf"}}',
            "/user/alice/comments/.json": b'{"data":{"children":[{"data":{"name":"t1_abc"}}],"after":null}}',
            "/user/alice/submitted/.json": b'{"data":{"children":[{"data":{"name":"t3_def"}}],"after":null}}',
            "/api/info.json": b'{"data":{"children":[{"data":{"name":"t1_abc","author":"alice"}}]}}',
        }

        class Response:
            headers = {}

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        def open_request(client, request):
            path = next((path for path in responses if path in request.full_url), None)
            self.assertIsNotNone(path)
            return Response(responses[path])

        with patch("cleareddit.SessionClient._open", open_request):
            client = SessionClient(self.root, {}, "alice", "cookie", delay=0)
            self.assertEqual(list(client.listing("comments")), [["t1_abc"]])
            self.assertEqual(list(client.listing("posts")), [["t3_def"]])
            self.assertEqual(client.info(["t1_abc"])["t1_abc"]["author"], "alice")

    def test_rate_limit_response_returns_before_waiting(self):
        class Response:
            headers = {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "120"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b""

        client = SessionClient(self.root, {}, "alice", "cookie", delay=0)
        client.modhash = "csrf"
        with patch("cleareddit.SessionClient._open", return_value=Response()), \
             patch("cleareddit.time.sleep", side_effect=AssertionError("sleep after success")):
            client.delete("t1_abc")
        self.assertGreater(client.rate_limit_until, time.monotonic() + 100)


if __name__ == "__main__":
    unittest.main()
