"""Local Reddit cleanup. Stores identifiers and progress, never post bodies."""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

VERSION = "0.1.0"
SCOPES = "identity history read edit"
KINDS = {"comments": "t1", "posts": "t3"}
FULLNAME = re.compile(r"^t([13])_([a-z0-9]+)$", re.I)
BARE_ID = re.compile(r"^[a-z0-9]+$", re.I)
RANDOM_WORDS = (
    "astronauts", "binoculars", "gatehouses", "hourglasses", "instruments",
    "jellybeans", "kaleidoscopes", "microphones", "paintbrushes", "paperclips",
    "tangerines", "telescopes", "typewriters", "windchimes", "xylophones",
)


class ClearRedditError(Exception):
    pass


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def save_private_json(path: Path, value: dict) -> None:
    private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temp, path)
    os.chmod(path, 0o600)


def load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        raise ClearRedditError("No configuration found. Run 'init' first.")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def normalize_id(value: str, kind: str) -> str:
    value = value.strip()
    prefix = KINDS[kind]
    match = FULLNAME.fullmatch(value)
    if match:
        if "t" + match.group(1) != prefix:
            raise ValueError(f"Wrong ID type: {value}")
        return value.lower()
    if not BARE_ID.fullmatch(value):
        raise ValueError(f"Invalid ID: {value!r}")
    return f"{prefix}_{value.lower()}"


def listing_path_kind(kind: str) -> str:
    if kind == "comments":
        return "comments"
    if kind == "posts":
        return "submitted"
    raise ValueError(f"Unknown item type: {kind}")


def random_replacement() -> str:
    return " ".join(secrets.SystemRandom().sample(RANDOM_WORDS, 4 + secrets.randbelow(2)))


def database(root: Path) -> sqlite3.Connection:
    private_dir(root)
    path = root / "state.sqlite3"
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("""
        CREATE TABLE IF NOT EXISTS items (
            account TEXT NOT NULL COLLATE NOCASE,
            fullname TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('comments', 'posts')),
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'deleted', 'missing', 'failed', 'manual_done')),
            edited INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            url TEXT,
            PRIMARY KEY(account, fullname)
        )
    """)
    # Migrate state created by the first release without discarding progress.
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='items'").fetchone()[0]
    if "manual_done" not in schema:
        db.execute("""
            CREATE TABLE items_new (
                account TEXT NOT NULL COLLATE NOCASE,
                fullname TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('comments', 'posts')),
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'deleted', 'missing', 'failed', 'manual_done')),
                edited INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                url TEXT,
                PRIMARY KEY(account, fullname)
            )
        """)
        db.execute("INSERT INTO items_new(account, fullname, kind, source, status, edited, last_error) "
                   "SELECT account, fullname, kind, source, status, edited, last_error FROM items")
        db.execute("DROP TABLE items")
        db.execute("ALTER TABLE items_new RENAME TO items")
    db.commit()
    return db


def add_ids(db: sqlite3.Connection, account: str, kind: str, ids, source: str) -> int:
    count = 0
    for raw_id in ids:
        fullname = normalize_id(raw_id, kind)
        cur = db.execute(
            "INSERT OR IGNORE INTO items(account, fullname, kind, source) VALUES (?, ?, ?, ?)",
            (account, fullname, kind, source),
        )
        count += cur.rowcount
    db.commit()
    return count


def import_csv(db: sqlite3.Connection, account: str, filename: str, stream) -> tuple[int, int]:
    kind = filename.lower()
    if kind not in ("comments.csv", "posts.csv"):
        raise ClearRedditError("Only comments.csv and posts.csv can be imported.")
    kind = "comments" if kind == "comments.csv" else "posts"
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ClearRedditError(f"Empty CSV file: {filename}")
    columns = {name.strip().lower(): name for name in reader.fieldnames}
    id_column = next((columns[c] for c in ("id", "comment_id", "post_id") if c in columns), None)
    if not id_column:
        raise ClearRedditError(f"Missing ID column in {filename}: {reader.fieldnames}")
    permalink_column = columns.get("permalink")
    ids = []
    urls = []
    for row_number, row in enumerate(reader, start=2):
        value = (row.get(id_column) or "").strip()
        if not value:
            continue
        try:
            fullname = normalize_id(value, kind)
            ids.append(fullname)
            urls.append((fullname, reddit_url((row.get(permalink_column) or "") if permalink_column else "", fullname, kind)))
        except ValueError as exc:
            raise ClearRedditError(f"{filename}, row {row_number}: {exc}") from exc
    added = add_ids(db, account, kind, ids, "export")
    for fullname, url in urls:
        if url:
            db.execute("UPDATE items SET url=COALESCE(url, ?) WHERE account=? AND fullname=?",
                       (url, account, fullname))
    db.commit()
    return len(ids), added


def reddit_url(permalink: str, fullname: str, kind: str) -> str | None:
    """Accept only Reddit permalinks; never open arbitrary URLs from an export."""
    if not permalink:
        return f"https://www.reddit.com/comments/{fullname[3:]}/" if kind == "posts" else None
    parsed = urllib.parse.urlparse(permalink)
    if parsed.scheme and (parsed.scheme != "https" or parsed.netloc.lower() not in
                          ("reddit.com", "www.reddit.com", "old.reddit.com")):
        return None
    path = parsed.path
    if not path.startswith("/") or not (path.startswith("/r/") or path.startswith("/comments/")):
        return None
    return "https://www.reddit.com" + path


def import_path(db: sqlite3.Connection, account: str, path: Path) -> list[tuple[str, int, int]]:
    results = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if Path(n).name.lower() in ("comments.csv", "posts.csv")]
            if len({Path(n).name.lower() for n in names}) != len(names):
                raise ClearRedditError("ZIP archive contains duplicate comments.csv or posts.csv files.")
            for name in names:
                with archive.open(name) as raw:
                    with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                        total, added = import_csv(db, account, Path(name).name, stream)
                        results.append((Path(name).name, total, added))
    elif path.name.lower() in ("comments.csv", "posts.csv"):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            total, added = import_csv(db, account, path.name, stream)
            results.append((path.name, total, added))
    else:
        raise ClearRedditError("Provide a Reddit export ZIP, comments.csv, or posts.csv.")
    if not results:
        raise ClearRedditError("No comments.csv or posts.csv file found.")
    return results


class RedditClient:
    def __init__(self, root: Path, config: dict, account: str | None = None, *, delay: float = 1.5):
        self.root = root
        self.config = config
        self.account = account
        self.delay = delay
        self.last_request = 0.0
        self.rate_limit_until = 0.0
        self.token = None
        self.expires_at = 0.0

    def _wait_before_request(self) -> None:
        now = time.monotonic()
        rate_wait = self.rate_limit_until - now
        if rate_wait > 1:
            print(f"Reddit rate limit: waiting about {math.ceil(rate_wait)} more seconds. "
                  "Press Ctrl+C to stop and resume later.", file=sys.stderr, flush=True)
        while True:
            now = time.monotonic()
            remaining = max(self.rate_limit_until - now,
                            self.delay - (now - self.last_request))
            if remaining <= 0:
                return
            time.sleep(min(30, remaining))
            if self.rate_limit_until - time.monotonic() > 1:
                print(f"Reddit rate limit: about "
                      f"{math.ceil(self.rate_limit_until - time.monotonic())} seconds left.",
                      file=sys.stderr, flush=True)

    def _record_rate_limit(self, headers) -> None:
        remaining = headers.get("X-Ratelimit-Remaining")
        reset = headers.get("X-Ratelimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            if float(remaining) < 2:
                self.rate_limit_until = max(self.rate_limit_until,
                                            time.monotonic() + min(600, max(0, float(reset))))
        except ValueError:
            pass

    def _request(self, url: str, data: dict | None = None, *, oauth: bool = False, retry: bool = True):
        if oauth:
            if self.token is None or time.time() >= self.expires_at - 60:
                self.refresh()
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"User-Agent": self.config["user_agent"], "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if oauth:
            headers["Authorization"] = f"bearer {self.token}"
        else:
            basic = base64.b64encode((self.config["client_id"] + ":").encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        request = urllib.request.Request(url, data=body, headers=headers)
        for attempt in range(5):
            if oauth:
                self._wait_before_request()
            try:
                if oauth:
                    self.last_request = time.monotonic()
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    if oauth:
                        self._record_rate_limit(response.headers)
                    if not payload:
                        return None
                    return json.loads(payload)
            except urllib.error.HTTPError as exc:
                if oauth and exc.code == 401 and retry:
                    self.token = None
                    self.refresh()
                    return self._request(url, data, oauth=True, retry=False)
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < 4:
                        retry_after = exc.headers.get("Retry-After") or exc.headers.get("X-Ratelimit-Reset")
                        try:
                            pause = float(retry_after) if retry_after else 2 ** attempt
                        except ValueError:
                            pause = 2 ** attempt
                        if exc.code == 429:
                            self.rate_limit_until = max(self.rate_limit_until,
                                                        time.monotonic() + min(600, max(1, pause)))
                        else:
                            time.sleep(min(600, max(1, pause)))
                        continue
                detail = exc.read(300).decode("utf-8", "replace")
                raise ClearRedditError(f"Reddit HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ClearRedditError(f"Network error: {exc.reason}") from exc
        raise ClearRedditError("Reddit request failed after several attempts.")

    def refresh(self) -> None:
        if not self.account:
            raise ClearRedditError("No account selected for token refresh.")
        refresh_token = self.config.get("accounts", {}).get(self.account, {}).get("refresh_token")
        if not refresh_token:
            raise ClearRedditError(f"Account {self.account} is not authenticated. Run 'auth'.")
        token = self._request("https://www.reddit.com/api/v1/access_token", {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
        })
        if not token or "access_token" not in token:
            raise ClearRedditError(f"Token refresh failed: {token}")
        self.token = token["access_token"]
        self.expires_at = time.time() + int(token.get("expires_in", 3600))

    def api(self, path: str, data: dict | None = None):
        if not path.startswith("/"):
            raise ValueError("API path must start with /")
        return self._request("https://oauth.reddit.com" + path, data, oauth=True)

    def me(self) -> str:
        result = self.api("/api/v1/me")
        return result["name"]

    def listing(self, kind: str):
        after = None
        seen_cursors = set()
        total = 0
        while True:
            query = {"limit": "100", "raw_json": "1", "sort": "new"}
            if after:
                query["after"] = after
            path = f"/user/{urllib.parse.quote(self.account, safe='')}/{listing_path_kind(kind)}?{urllib.parse.urlencode(query)}"
            data = self.api(path)["data"]
            children = data.get("children", [])
            ids = [child["data"]["name"] for child in children]
            yield ids
            total += len(children)
            next_cursor = data.get("after")
            if not next_cursor or next_cursor in seen_cursors or not children:
                break
            seen_cursors.add(next_cursor)
            after = next_cursor
        return total

    def info(self, ids: list[str]) -> dict[str, dict]:
        query = urllib.parse.urlencode({"id": ",".join(ids), "raw_json": "1"})
        children = self.api("/api/info?" + query)["data"]["children"]
        return {child["data"]["name"]: child["data"] for child in children}

    def edit(self, fullname: str, replacement: str) -> None:
        result = self.api("/api/editusertext", {
            "api_type": "json", "thing_id": fullname, "text": replacement,
        })
        errors = (result or {}).get("json", {}).get("errors", [])
        if errors:
            raise ClearRedditError(f"Edit failed: {errors}")

    def delete(self, fullname: str) -> None:
        result = self.api("/api/del", {"id": fullname})
        if isinstance(result, dict):
            errors = result.get("json", {}).get("errors", [])
            if errors:
                raise ClearRedditError(f"Delete failed: {errors}")


class SessionClient(RedditClient):
    """Use an existing signed-in Reddit web session, without an OAuth app."""

    def __init__(self, root: Path, config: dict, account: str, cookie: str, *, delay: float = 1.5):
        super().__init__(root, config, account, delay=delay)
        self.cookie = cookie
        self.modhash = None
        self.opener = urllib.request.build_opener(self.NoRedirect())

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            # Do not forward an account cookie to a redirect destination.
            return None

    def _open(self, request):
        return self.opener.open(request, timeout=30)

    def _session_request(self, path: str, data: dict | None = None):
        if not path.startswith("/"):
            raise ValueError("API path must start with /")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {
            "User-Agent": f"desktop:cleareddit-local:{VERSION} (by /u/{self.account})",
            "Accept": "application/json",
            "Cookie": f"reddit_session={self.cookie}",
        }
        if body is not None:
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://old.reddit.com/",
                "Origin": "https://old.reddit.com",
            })
            if self.modhash:
                headers["X-Modhash"] = self.modhash
        request = urllib.request.Request("https://old.reddit.com" + path, data=body, headers=headers)
        for attempt in range(5):
            self._wait_before_request()
            try:
                self.last_request = time.monotonic()
                with self._open(request) as response:
                    payload = response.read()
                    self._record_rate_limit(response.headers)
                    if not payload:
                        return None
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise ClearRedditError("Reddit returned non-JSON data. The session may have expired or been blocked.") from exc
            except urllib.error.HTTPError as exc:
                if (exc.code == 429 or 500 <= exc.code < 600) and attempt < 4:
                    retry_after = exc.headers.get("Retry-After") or exc.headers.get("X-Ratelimit-Reset")
                    try:
                        pause = float(retry_after) if retry_after else 2 ** attempt
                    except ValueError:
                        pause = 2 ** attempt
                    if exc.code == 429:
                        self.rate_limit_until = max(self.rate_limit_until,
                                                    time.monotonic() + min(600, max(1, pause)))
                    else:
                        time.sleep(min(600, max(1, pause)))
                    continue
                if exc.code in (301, 302, 303, 307, 308):
                    raise ClearRedditError("Reddit redirected the request. The session may have expired.") from exc
                raise ClearRedditError(f"Reddit HTTP {exc.code}. Check the session and access.") from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ClearRedditError(f"Network error: {exc.reason}") from exc
        raise ClearRedditError("Reddit request failed after several attempts.")

    def me(self) -> str:
        result = self._session_request("/api/me.json?raw_json=1")
        data = (result or {}).get("data") or {}
        if not data.get("name") or not data.get("modhash"):
            raise ClearRedditError("Invalid session or missing modhash.")
        self.modhash = data["modhash"]
        return data["name"]

    def api(self, path: str, data: dict | None = None):
        if self.modhash is None:
            self.me()
        return self._session_request(path, data)

    def listing(self, kind: str):
        after = None
        seen = set()
        while True:
            query = {"limit": "100", "raw_json": "1", "sort": "new"}
            if after:
                query["after"] = after
            path = f"/user/{urllib.parse.quote(self.account, safe='')}/{listing_path_kind(kind)}/.json?{urllib.parse.urlencode(query)}"
            result = self.api(path)
            data = (result or {}).get("data") or {}
            children = data.get("children", [])
            yield [child["data"]["name"] for child in children]
            after = data.get("after")
            if not after or after in seen or not children:
                break
            seen.add(after)

    def info(self, ids: list[str]) -> dict[str, dict]:
        query = urllib.parse.urlencode({"id": ",".join(ids), "raw_json": "1"})
        result = self.api("/api/info.json?" + query)
        children = (result or {}).get("data", {}).get("children", [])
        return {child["data"]["name"]: child["data"] for child in children}


def callback_code(redirect_uri: str, expected_state: str, authorization_url: str) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname != "127.0.0.1" or parsed.scheme != "http" or not parsed.port:
        raise ClearRedditError("Redirect URI must be http://127.0.0.1:PORT/callback.")
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            incoming = urllib.parse.urlparse(self.path)
            if incoming.path != parsed.path:
                self.send_error(404)
                return
            params = urllib.parse.parse_qs(incoming.query)
            if params.get("state", [None])[0] != expected_state:
                result["error"] = "OAuth state does not match."
            elif "error" in params:
                result["error"] = params["error"][0]
            else:
                result["code"] = params.get("code", [None])[0]
            self.send_response(200 if result.get("code") else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Sign-in received. You can close this window." if result.get("code") else "Sign-in failed.").encode())

        def log_message(self, format, *args):
            pass  # Avoid logging one-time OAuth codes in callback URLs.

    with HTTPServer(("127.0.0.1", parsed.port), Handler) as server:
        server.timeout = 1
        print("Opening Reddit sign-in in your browser. If needed, open this URL yourself:")
        print(authorization_url)
        webbrowser.open(authorization_url)
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise ClearRedditError(result.get("error", "Sign-in timed out after 3 minutes."))
    return result["code"]


def require_approved(config: dict) -> None:
    if not config.get("api_approved"):
        raise ClearRedditError("Reddit API approval is required. After approval, run 'init --api-approved'.")


def authorized_client(root: Path, config: dict, account: str) -> RedditClient:
    session = next(((name, value) for name, value in config.get("sessions", {}).items()
                    if name.casefold() == account.casefold()), None)
    if session:
        client = SessionClient(root, config, session[0], session[1]["reddit_session"])
        actual = client.me()
        if actual.casefold() != account.casefold():
            raise ClearRedditError(f"Session belongs to u/{actual}, expected u/{account}.")
        return client
    require_approved(config)
    if account not in config.get("accounts", {}):
        raise ClearRedditError(f"Account {account} is not authenticated. Run 'auth'.")
    client = RedditClient(root, config, account)
    actual = client.me()
    if actual.casefold() != account.casefold():
        raise ClearRedditError(f"Token belongs to u/{actual}, expected u/{account}.")
    return client


def selected_accounts(config: dict, name: str) -> list[str]:
    if name == "all":
        accounts = sorted(set(config.get("accounts", {})) | set(config.get("sessions", {})))
        if not accounts:
            raise ClearRedditError("No accounts have been added yet.")
        return accounts
    return [name]


def known_accounts(db: sqlite3.Connection, config: dict) -> list[str]:
    names = {name.casefold(): name for name in selected_accounts(config, "all")} if (
        config.get("accounts") or config.get("sessions")) else {}
    for (name,) in db.execute("SELECT DISTINCT account FROM items"):
        names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def summarize(db: sqlite3.Connection, account: str) -> None:
    rows = db.execute(
        "SELECT kind, status, COUNT(*) FROM items WHERE account=? GROUP BY kind, status ORDER BY kind, status",
        (account,),
    ).fetchall()
    print(f"u/{account}:")
    if not rows:
        print("  No items found.")
    for kind, status, count in rows:
        print(f"  {kind:8} {status:8} {count}")


def print_queue(db: sqlite3.Connection, account: str, limit: int) -> None:
    rows = db.execute(
        "SELECT fullname, url FROM items WHERE account=? AND status IN ('pending', 'failed', 'missing') "
        "ORDER BY kind, fullname LIMIT ?", (account, limit),
    ).fetchall()
    if not rows:
        print(f"u/{account}: no pending items.")
    for fullname, url in rows:
        print(f"u/{account} {fullname}  {url or '(no permalink available)'}")


def item_content(item: dict, kind: str, *, full: bool = False) -> str | None:
    """Format live content for terminal output without retaining it in SQLite."""
    if kind == "comments":
        value = item.get("body")
    else:
        title = item.get("title") or ""
        body = item.get("selftext") or ""
        value = f"{title}\n{body}".strip() if body else title
    if not isinstance(value, str) or not value.strip():
        return None
    # Reddit text may contain terminal control characters; never print them raw.
    safe = "".join(char if char.isprintable() or char in "\n\t" else " " for char in value)
    if full:
        return safe
    compact = " ".join(safe.split())
    return compact[:180] + ("…" if len(compact) > 180 else "")


def run_account(db: sqlite3.Connection, client: RedditClient, account: str, *, mode: str,
                replacement: str | None, kinds: set[str], limit: int | None,
                full_content: bool = False) -> Counter:
    clauses = ",".join("?" for _ in kinds)
    params = [account, *sorted(kinds)]
    query = (f"SELECT fullname, kind, edited FROM items WHERE account=? AND kind IN ({clauses}) "
             "AND status IN ('pending', 'failed') ORDER BY kind, fullname")
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = db.execute(query, params).fetchall()
    result = Counter()
    for offset in range(0, len(rows), 100):
        batch = rows[offset:offset + 100]
        try:
            live = client.info([row[0] for row in batch])
        except ClearRedditError:
            raise  # Keep batch pending for a later run.
        for fullname, kind, edited in batch:
            item = live.get(fullname)
            if not item:
                status, error = "missing", "Not found by /api/info; deletion is unconfirmed."
            elif (item.get("author") or "").casefold() != account.casefold():
                status, error = "failed", "Author does not match the signed-in account."
            else:
                content = item_content(item, kind, full=full_content)
                if content:
                    if full_content:
                        print(f"u/{account} {fullname} Original content:")
                        for line in content.splitlines():
                            print(f"  {line}")
                    else:
                        print(f"u/{account} {fullname} Original content: {content}")
                edit_error = None
                if mode == "overwrite-delete" and not edited and (kind == "comments" or item.get("is_self")):
                    try:
                        client.edit(fullname, replacement if replacement is not None else random_replacement())
                        edited = 1
                        db.execute("UPDATE items SET edited=1 WHERE account=? AND fullname=?", (account, fullname))
                        db.commit()
                    except ClearRedditError as exc:
                        edit_error = str(exc)
                try:
                    client.delete(fullname)
                    status = "deleted"
                    error = f"Edit failed, but deletion succeeded: {edit_error}" if edit_error else None
                except ClearRedditError as exc:
                    status, error = "failed", str(exc)
                    if edit_error:
                        error = f"Edit: {edit_error}; delete: {error}"
            db.execute(
                "UPDATE items SET status=?, edited=?, last_error=? WHERE account=? AND fullname=?",
                (status, edited, error, account, fullname),
            )
            db.commit()
            result[status] += 1
            print(f"u/{account} {fullname}: {status}" + (f" ({error})" if error else ""))
    return result


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete your own Reddit comments and posts locally")
    parser.add_argument("--state-dir", type=Path, default=Path(".cleareddit"), help="Local directory for credentials and progress")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Configure an approved Reddit OAuth app")
    init.add_argument("--client-id", required=True)
    init.add_argument("--contact-user", required=True, help="Reddit username for the User-Agent header")
    init.add_argument("--port", type=int, default=8765)
    init.add_argument("--api-approved", action="store_true", help="Confirm that Reddit approved your API access")
    sub.add_parser("auth", help="Sign in to an account through a browser")
    session = sub.add_parser("session-add", help="Add an existing Reddit session for direct HTTP requests")
    session.add_argument("--account", required=True)
    session.add_argument("--cookie-file", type=Path, help="Read the reddit_session value from a file instead of a hidden prompt")
    scan = sub.add_parser("scan", help="Scan visible comment and post lists")
    scan.add_argument("--account", default="all")
    imp = sub.add_parser("import", help="Import a Reddit data export")
    imp.add_argument("--account", required=True)
    imp.add_argument("path", type=Path)
    plan = sub.add_parser("plan", help="Show collected items and progress")
    plan.add_argument("--account", default="all")
    queue = sub.add_parser("queue", help="List links for manual deletion")
    queue.add_argument("--account", required=True)
    queue.add_argument("--limit", type=int, default=50)
    mark = sub.add_parser("mark", help="Mark a manually deleted item as done")
    mark.add_argument("--account", required=True)
    mark.add_argument("fullname", help="ID with a t1_ or t3_ prefix")
    run = sub.add_parser("run", help="Delete collected items")
    run.add_argument("--account", default="all")
    run.add_argument("--mode", choices=("delete", "overwrite-delete"), default="overwrite-delete")
    run.add_argument("--replacement", help="Custom replacement text; default: 4 or 5 random words per item")
    run.add_argument("--full-content", action="store_true", help="Show full original content instead of an excerpt")
    run.add_argument("--kinds", choices=("all", "comments", "posts"), default="all")
    run.add_argument("--limit", type=int)
    run.add_argument("--yes", action="store_true", help="Confirm that items can be changed and deleted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    root = args.state_dir.expanduser().resolve()
    try:
        if args.command == "init":
            if not 1 <= args.port <= 65535:
                raise ClearRedditError("Port must be between 1 and 65535.")
            if not args.client_id.strip() or not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", args.contact_user.strip()):
                raise ClearRedditError("Invalid client ID or contact username.")
            existing = load_config(root) if (root / "config.json").exists() else {}
            if existing.get("client_id") != args.client_id and existing.get("accounts"):
                raise ClearRedditError("Client ID cannot change while accounts are authenticated.")
            config = {
                "client_id": args.client_id.strip(),
                "user_agent": f"desktop:cleareddit-local:{VERSION} (by /u/{args.contact_user.strip()})",
                "redirect_uri": f"http://127.0.0.1:{args.port}/callback",
                "api_approved": bool(args.api_approved or existing.get("api_approved")),
                "accounts": existing.get("accounts", {}),
                "sessions": existing.get("sessions", {}),
            }
            save_private_json(root / "config.json", config)
            print(f"Configuration saved. Redirect URI: {config['redirect_uri']}")
            if not config["api_approved"]:
                print("API access remains disabled until Reddit grants approval.")
            return 0

        config = load_config(root) if (root / "config.json").exists() else {}
        if args.command == "session-add":
            account = args.account.removeprefix("u/")
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", account):
                raise ClearRedditError("Invalid Reddit username.")
            cookie = (args.cookie_file.read_text(encoding="utf-8").strip() if args.cookie_file
                      else getpass.getpass("reddit_session value (input hidden): ").strip())
            cookie = cookie.removeprefix("reddit_session=")
            if not cookie or any(c in cookie for c in ";\r\n\t "):
                raise ClearRedditError("Invalid cookie. Enter only the reddit_session value.")
            client = SessionClient(root, config, account, cookie)
            actual = client.me()
            if actual.casefold() != account.casefold():
                raise ClearRedditError(f"Cookie belongs to u/{actual}, expected u/{account}.")
            config.setdefault("sessions", {})[actual] = {"reddit_session": cookie}
            save_private_json(root / "config.json", config)
            print(f"u/{actual}: session saved. 'scan' and 'run' will use it.")
            return 0
        if args.command == "auth":
            if not config.get("client_id"):
                raise ClearRedditError("No configuration found. Run 'init' first.")
            require_approved(config)
            state = secrets.token_urlsafe(24)
            query = urllib.parse.urlencode({
                "client_id": config["client_id"], "response_type": "code", "state": state,
                "redirect_uri": config["redirect_uri"], "duration": "permanent", "scope": SCOPES,
            })
            url = "https://www.reddit.com/api/v1/authorize?" + query
            code = callback_code(config["redirect_uri"], state, url)
            client = RedditClient(root, config)
            token = client._request("https://www.reddit.com/api/v1/access_token", {
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": config["redirect_uri"],
            })
            if not token or not token.get("refresh_token"):
                raise ClearRedditError("No refresh token received. Permanent authorization is required.")
            client.token = token["access_token"]
            client.expires_at = time.time() + int(token.get("expires_in", 3600))
            username = client.me()
            config.setdefault("accounts", {})[username] = {"refresh_token": token["refresh_token"]}
            save_private_json(root / "config.json", config)
            print(f"u/{username} authenticated. Run 'auth' again for more accounts.")
            return 0

        with closing(database(root)) as db:
            if args.command == "import":
                account = args.account.removeprefix("u/")
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", account):
                    raise ClearRedditError("Invalid Reddit username.")
                for filename, total, added in import_path(db, account, args.path):
                    print(f"{filename}: {total} IDs read, {added} new for u/{account}.")
                print("Ownership of imported items is checked before they are changed or deleted.")
            elif args.command == "scan":
                for account in selected_accounts(config, args.account):
                    client = authorized_client(root, config, account)
                    for kind in KINDS:
                        pages = 0
                        found = added = 0
                        for ids in client.listing(kind):
                            pages += 1
                            found += len(ids)
                            added += add_ids(db, account, kind, ids, "listing")
                        print(f"u/{account} {kind}: {found} visible, {added} new ({pages} pages).")
                        if found >= 1000:
                            print("  Note: Reddit lists can stop at about 1,000 items. Import a data export for older items.")
            elif args.command == "plan":
                accounts = known_accounts(db, config) if args.account == "all" else [args.account]
                if not accounts:
                    print("No items or accounts found yet.")
                for account in accounts:
                    summarize(db, account)
            elif args.command == "queue":
                if args.limit <= 0:
                    raise ClearRedditError("--limit must be positive.")
                print_queue(db, args.account, args.limit)
            elif args.command == "mark":
                if not FULLNAME.fullmatch(args.fullname):
                    raise ClearRedditError("ID must start with t1_ (comment) or t3_ (post).")
                cur = db.execute(
                    "UPDATE items SET status='manual_done', last_error=NULL WHERE account=? AND fullname=? "
                    "AND status IN ('pending', 'failed', 'missing')",
                    (args.account, args.fullname.lower()),
                )
                db.commit()
                if not cur.rowcount:
                    raise ClearRedditError("Item is not pending or was not imported for this account.")
                print(f"u/{args.account} {args.fullname.lower()}: marked done manually (not verified through Reddit).")
            elif args.command == "run":
                if not args.yes:
                    print("Dry run: nothing changed on Reddit. Use --yes to delete items.")
                    accounts = known_accounts(db, config) if args.account == "all" else [args.account]
                    for account in accounts:
                        summarize(db, account)
                    return 0
                if args.limit is not None and args.limit <= 0:
                    raise ClearRedditError("--limit must be positive.")
                if args.replacement is not None and not args.replacement.strip():
                    raise ClearRedditError("Replacement text cannot be empty.")
                kinds = set(KINDS) if args.kinds == "all" else {args.kinds}
                if args.account == "all":
                    authenticated = {name.casefold() for name in selected_accounts(config, "all")}
                    unauthenticated = [name for name in known_accounts(db, config)
                                       if name.casefold() not in authenticated]
                    if unauthenticated:
                        raise ClearRedditError("These imported accounts are not authenticated yet: "
                                               + ", ".join(unauthenticated))
                for account in selected_accounts(config, args.account):
                    client = authorized_client(root, config, account)
                    counts = run_account(db, client, account, mode=args.mode,
                                         replacement=args.replacement, kinds=kinds, limit=args.limit,
                                         full_content=args.full_content)
                    print(f"u/{account}: {dict(counts)}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; run 'run' again to resume.",
              file=sys.stderr)
        return 130
    except (ClearRedditError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
