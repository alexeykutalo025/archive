"""Archive Index. Run with Python 3.8 or newer."""
from video import VIDEO_EXTENSIONS, encode_video

import argparse
import json
import hashlib
import os
import errno
import shutil
import sqlite3
import threading
from contextlib import closing
import stat
import socket
import time
from urllib.error import HTTPError, URLError
import secrets
from http.client import HTTPException
import tempfile
import zipfile

import pyzipper
import re
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

ARCHIVE = "https://archive.org"
SORT = {"": "Relevance", "date desc": "Newest first", "date asc": "Oldest first",
        "downloads desc": "Most downloaded"}
PROJECT_ROOT = Path(__file__).resolve().parent
SAVE_DIR = PROJECT_ROOT / "save"
FORM_TOKEN = secrets.token_hex(32)
LIMIT = 20
ZIP_PASSWORD = (os.getenv("ARCHIVE_ZIP_PASSWORD") or "Nevergiveup&0429").encode("utf-8")


def esc(value):
    return escape(str(value), quote=True)


def link(path, **params):
    return esc(path + "?" + urlencode({k: v for k, v in params.items() if v != ""}))


def api(path):
    req = Request(ARCHIVE + path, headers={"User-Agent": "ArchiveIndex/1.0",
                                           "Accept": "application/json"})
    with urlopen(req, timeout=25) as response:
        data = json.load(response)
    if "error" in data:
        raise ValueError("Archive request failed")
    return data


def select(name, label, choices, value):
    options = "".join(f'<option value="{esc(k)}" {"selected" if k == value else ""}>{esc(v)}</option>'
                      for k, v in choices.items())
    return f'<label>{label}<select name="{name}">{options}</select></label>'


def layout(title, content):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{esc(title)} · Archive Index</title><link rel="stylesheet" href="/style.css">
    </head><body><header><a class="brand" href="/">Archive Index</a>
    <a href="/">Saved files</a><a href="/home">Search archive</a>
    <a href="https://archive.org/">Internet Archive ↗</a></header>
    <main>{content}</main><footer>Archive Index <span>Independent text-only interface for the Internet Archive</span></footer></body></html>"""


def page_number(params):
    raw = params.get("page", "1")
    return max(1, int(raw)) if re.fullmatch(r"[0-9]{1,7}", raw) else 1


def pager(path, params, page, total):
    def anchor(n, label):
        return f'<a href="{link(path, **dict(params, page=n))}">{label}</a>'
    prev = anchor(page - 1, "← Previous") if page > 1 else ""
    nex = anchor(page + 1, "Next →") if page * LIMIT < total else ""
    return f'<nav>{prev}<span>Page {page:,}</span>{nex}</nav>' if prev or nex else ""


def search(params):
    term = params.get("q", "").strip()
    sort = params.get("sort", "")
    year = params.get("year", "").strip()
    controls = f"""<h1>Search archive<span>.</span></h1>
    <form action="/search"><label>Search terms<input type="search" name="q" placeholder="Title, creator, or subject" value="{esc(term)}"></label>
    <div class="controls">
    <label>Year<input name="year" inputmode="numeric" pattern="[0-9]{{4}}" placeholder="Any year" value="{esc(year)}"></label>
    {select("sort","Sort by",SORT,sort)}<button>Search</button></div></form>"""
    if not params:
        topics = ["Space exploration", "Jazz", "Silent film", "Natural history"]
        items = "".join(f'<a class="topic" href="{link("/search", q=t)}">{t} →</a>' for t in topics)
        return layout("Search", controls + '<h2>Browse by topic</h2>' + items)
    if year and not re.fullmatch(r"[0-9]{4}", year):
        return layout("Search", controls + "<p>Enter a four-digit year.</p>")
    if sort not in SORT:
        return layout("Search", controls + "<p>Select a listed filter and search again.</p>")
    # Treat punctuation as text separators, keeping Archive query syntax internal.
    words = re.findall(r"[^\W_]+", term, re.UNICODE)
    if term and not words:
        return layout("Search", controls + "<p>Enter a word or number to search.</p>")
    clauses = ['"' + w + '"' for w in words]
    clauses.append("mediatype:movies")
    if year:
        clauses.append("year:" + year)
    page = page_number(params)
    args = {"q": " AND ".join(clauses), "fl[]": ["identifier","title","creator","year"],
            "rows": LIMIT, "page": page, "output": "json"}
    if sort:
        args["sort[]"] = sort
    result = api("/advancedsearch.php?" + urlencode(args, doseq=True))["response"]
    total = result["numFound"]
    rows = []
    for doc in result["docs"]:
        identifier = doc["identifier"]
        creator = doc.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(map(str, creator))
        meta = " · ".join(esc(v) for v in ["Video",doc.get("year"),creator] if v)
        rows.append(f'<article><a href="{link("/files", id=identifier)}">{esc(doc.get("title") or identifier)}</a></article>')
    body = "".join(rows) or "<p>No items on this page. Try fewer terms or a broader filter.</p>"
    return layout("Search", controls + f"<h2>{total:,} results</h2>" + body + pager("/search", params, page, total))


def file_size(raw):
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:,.0f} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1000


SAVE_TABLES = {"standard": "saved_files", "original": "saved_originals"}


def save_index():
    SAVE_DIR.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(str(SAVE_DIR / "saved.sqlite3"))
    try:
        for table in SAVE_TABLES.values():
            db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                item TEXT NOT NULL, filename TEXT NOT NULL, zip_name TEXT UNIQUE NOT NULL,
                PRIMARY KEY (item, filename))""")
    except Exception:
        db.close()
        raise
    return db


def reserve_zip_name(identifier, filename, mode):
    table = SAVE_TABLES[mode]
    # Persist the destination before downloading. Its atomic publication marks completion.
    with closing(save_index()) as db, db:
        db.execute(f"""INSERT INTO {table} VALUES (?, ?, ?)
                      ON CONFLICT(item, filename) DO NOTHING""",
                   (identifier, filename, f"archive-{secrets.token_hex(12)}.zip"))
        return db.execute(f"SELECT zip_name FROM {table} WHERE item=? AND filename=?",
                          (identifier, filename)).fetchone()[0]


active_saves = set()
active_lock = threading.Lock()


def files(params):
    ident = params.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", ident):
        return layout("Files", "<h1>Invalid item identifier</h1>")
    metadata = api("/metadata/" + quote(ident))
    if not metadata.get("metadata"):
        return layout("Files", "<h1>Item unavailable</h1>")
    title = metadata["metadata"].get("title") or ident
    entries = [f for f in metadata.get("files", []) if f.get("name") and str(f.get("private","")).lower() not in ("true","1")]
    formats = sorted({f["format"] for f in entries if f.get("format")})
    fmt = params.get("format", "")
    name = params.get("name", "")
    matches = [f for f in entries if (not fmt or f.get("format") == fmt) and name.casefold() in f["name"].casefold()]
    page = page_number(params)
    start = (page - 1) * LIMIT
    body = f"""<a href="/home">← Search</a><p>{esc(title)}</p><a href="{ARCHIVE}/details/{ident}">View on archive.org ↗</a>
    <form action="/files" class="controls"><input type="hidden" name="id" value="{ident}">
    <label>Filename<input name="name" value="{esc(name)}" placeholder="Filter by filename"></label>
    {select("format","File format",{"":"All formats", **{x:x for x in formats}},fmt)}<button>Filter files</button></form>
    <h2>{len(matches):,} {'file' if len(matches) == 1 else 'files'}</h2>"""
    if matches:
        body += "<p>Encrypted ZIPs are saved in the project’s <code>save/</code> folder.</p>"
    saved_files = {}
    if (SAVE_DIR / "saved.sqlite3").is_file():
        with closing(save_index()) as db:
            for mode, table in SAVE_TABLES.items():
                for filename, zip_name in db.execute(f"SELECT filename, zip_name FROM {table} WHERE item=?", (ident,)):
                    saved_files[filename, mode] = zip_name
    for f in matches[start:start + LIMIT]:
        filename = f["name"]
        if any(part in (".","..") for part in filename.split("/")):
            continue
        detail = " · ".join(esc(x) for x in [f.get("format"),file_size(f.get("size"))] if x)
        body += f"""<article class="file"><div>{esc(filename)}<small>{detail}</small></div><section class="save-options">"""
        for mode, label in (("standard", "Click"), ("original", "Here")):
            saved = saved_files.get((filename, mode))
            if saved and not saved_exists(item_folder(ident), saved):
                saved = None
            description = "Save original file" if mode == "original" else "Save file (encode videos)"
            body += f"""
        <form method="post" action="/save" class="save-file">
        <input type="hidden" name="mode" value="{mode}">
        <input type="hidden" name="token" value="{FORM_TOKEN}">
        <input type="hidden" name="id" value="{esc(ident)}">
        <input type="hidden" name="name" value="{esc(filename)}">
        <button {'disabled' if saved else ''} title="{description}" aria-label="{label} — {description} {esc(filename)}">{'Saved' if saved else label}</button>
        <small class="status" role="status"></small><small class="progress"></small>
        <code class="saved-name">{esc(saved or "")}</code><button type="button" class="copy" {'' if saved else 'hidden'}>Copy filename</button>
        </form>"""
        body += "</section></article>"
    if not matches:
        body += "<p>No matching files. Change the filters or check the item on archive.org.</p>"
    return layout(title, body + pager("/files", params, page, len(matches)) + '<script src="/save.js" defer></script>')


ARCHIVE_NAME = re.compile(r"archive-[0-9a-f]{24}\.zip")


def plain_entry(path, kind):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return kind(info.st_mode) and not (getattr(info, "st_file_attributes", 0) &
                                        stat.FILE_ATTRIBUTE_REPARSE_POINT)


def item_folder(identifier):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", identifier):
        raise ValueError("Invalid item identifier")
    name = identifier
    if (PureWindowsPath(name).is_reserved() or name.endswith(".") or len(name) > 200
            or name.lower().startswith("saved.sqlite3")):
        name = "item~" + hashlib.sha256(identifier.encode()).hexdigest()
    return SAVE_DIR / name


def saved_exists(folder, name):
    return (ARCHIVE_NAME.fullmatch(name) and plain_entry(folder, stat.S_ISDIR)
            and plain_entry(folder / name, stat.S_ISREG))


def ensure_folder(folder):
    folder.mkdir(mode=0o700, exist_ok=True)
    if not plain_entry(folder, stat.S_ISDIR):
        raise ValueError("Save destination must be a regular directory")


def migrate_saved_files():
    if not (SAVE_DIR / "saved.sqlite3").is_file():
        return
    with closing(save_index()) as db:
        for table in SAVE_TABLES.values():
            for item, name in db.execute(f"SELECT item, zip_name FROM {table}"):
                source = SAVE_DIR / name
                if not ARCHIVE_NAME.fullmatch(name) or not plain_entry(source, stat.S_ISREG):
                    continue
                folder = item_folder(item)
                ensure_folder(folder)
                destination = folder / name
                try:
                    os.link(source, destination)
                except FileExistsError:
                    if not plain_entry(destination, stat.S_ISREG) or not source.samefile(destination):
                        print(f"Migration conflict: kept both copies of {name}", flush=True)
                        continue
                source.unlink()


def public_folder(name):
    if (not name or name.startswith(".") or "/" in name or chr(92) in name
            or ":" in name or any(ord(c) < 32 for c in name)):
        return False
    return plain_entry(SAVE_DIR / name, stat.S_ISDIR)


def downloads_page():
    folders = []
    if SAVE_DIR.is_dir():
        folders = sorted((p for p in SAVE_DIR.iterdir() if public_folder(p.name)),
                         key=lambda p: p.stat().st_mtime_ns, reverse=True)
    links = "".join(
        f'<li><a class="topic" href="/download/{quote(folder.name, safe="")}" download>'
        f'{esc(folder.name)}</a></li>' for folder in folders
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Saved folders</title><link rel="stylesheet" href="/style.css"></head>
    <body><main><ul class="folders" aria-label="Folder downloads">{links}</ul></main></body></html>"""


def pack_folder(folder, inner):
    def fail(error):
        raise error

    for root, dirs, files in os.walk(folder, onerror=fail):
        path = Path(root)
        dirs[:] = sorted(d for d in dirs if plain_entry(path / d, stat.S_ISDIR))
        inner.writestr(path.relative_to(SAVE_DIR).as_posix() + "/", b"")
        for name in sorted(files):
            child = path / name
            if plain_entry(child, stat.S_ISREG):
                inner.write(child, child.relative_to(SAVE_DIR).as_posix())


class Handler(BaseHTTPRequestHandler):
    def download_saved(self, name):
        name = unquote(name)
        if not public_folder(name):
            self.send_error(404)
            return
        with tempfile.TemporaryFile() as download:
            with pyzipper.AESZipFile(download, "w", compression=pyzipper.ZIP_STORED) as archive:
                archive.setpassword(ZIP_PASSWORD)
                archive.setencryption(pyzipper.WZ_AES, nbits=256)
                with archive.open("contents.zip", "w", force_zip64=True) as member:
                    with zipfile.ZipFile(member, "w") as inner:
                        pack_folder(SAVE_DIR / name, inner)
            length = download.tell()
            download.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=\"folder-download.zip\"; filename*=UTF-8''" + quote(name + "-download.zip", safe=""))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            try:
                shutil.copyfileobj(download, self.wfile, 256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass


    def event(self, **data):
        self.wfile.write((json.dumps(data) + "\n").encode())
        self.wfile.flush()

    def save_zip(self, params, streaming):
        identifier = params.get("id", "")
        filename = params.get("name", "")
        parts = filename.split("/")
        if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", identifier)
                or any(part in ("", ".", "..") for part in parts)
                or chr(92) in filename or any(ord(c) < 32 for c in filename)):
            raise ValueError("Invalid file selection")
        mode = params.get("mode", "standard")
        if mode not in SAVE_TABLES:
            raise ValueError("Invalid save option")
        saved_name = reserve_zip_name(identifier, filename, mode)
        if not saved_exists(item_folder(identifier), saved_name):
            with active_lock:
                if saved_name in active_saves:
                    raise ValueError("This file is already being saved. Refresh the file list when it finishes.")
                active_saves.add(saved_name)
            try:
                if not saved_exists(item_folder(identifier), saved_name):
                    self.download_zip(identifier, filename, parts, saved_name, streaming, mode)
            finally:
                with active_lock:
                    active_saves.remove(saved_name)
        if streaming:
            self.event(state="saved", filename=saved_name)
            return
        self.send_response(303)
        self.send_header("Location", "/saved?" + urlencode({"file": saved_name, "id": identifier}))
        self.end_headers()

    def download_zip(self, identifier, filename, parts, saved_name, streaming, mode):
        item = api("/metadata/" + identifier)
        entry = next((f for f in item.get("files", []) if f.get("name") == filename
                      and str(f.get("private", "")).lower() not in ("true", "1")), None)
        if entry is None:
            raise ValueError("This file is unavailable on archive.org.")
        member = re.sub(r'[:<>"|?*]', "_", parts[-1])
        request = Request(f"{ARCHIVE}/download/{identifier}/{quote(filename)}",
                          headers={"User-Agent": "ArchiveIndex/1.0"})
        def report(message):
            if streaming:
                self.event(state="stage", message=message)

        with tempfile.TemporaryDirectory(dir=SAVE_DIR, prefix=".saving-") as temp:
            folder = Path(temp)
            media = folder / "source"
            with urlopen(request, timeout=45) as source, media.open("wb") as target:
                total = entry.get("size") or source.headers.get("Content-Length")
                content_type = source.headers.get("Content-Type", "").lower()
                size = 0
                digest = hashlib.sha1()
                last_update = 0.0
                for chunk in iter(lambda: source.read(256 * 1024), b""):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    now = time.monotonic()
                    if streaming and now - last_update >= 0.4:
                        self.event(state="progress", received=size, total=total)
                        last_update = now
            if total is not None and size != int(total):
                raise ValueError("Incomplete archive.org file")
            if entry.get("sha1") and digest.hexdigest() != entry["sha1"].lower():
                raise ValueError("Archive.org file checksum mismatch")

            compression = pyzipper.ZIP_STORED if mode == "original" else pyzipper.ZIP_DEFLATED
            if mode == "standard" and (Path(member).suffix.lower() in VIDEO_EXTENSIONS or content_type.startswith("video/")):
                media = encode_video(folder, report)
                member = Path(member).stem + ".mp4"
                compression = pyzipper.ZIP_STORED
            stage = "Compressing ZIP" if mode == "standard" else "Packaging ZIP"
            report(f"{stage} 0%")
            packed = folder / "archive.zip"
            with pyzipper.AESZipFile(packed, "w", compression=compression) as archive:
                archive.setpassword(ZIP_PASSWORD)
                archive.setencryption(pyzipper.WZ_AES, nbits=256)
                with archive.open("contents.zip", "w", force_zip64=True) as payload, \
                        zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as inner:
                    total_bytes = media.stat().st_size
                    written = last_percent = 0
                    with media.open("rb") as source, inner.open(member, "w", force_zip64=True) as dest:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            dest.write(chunk)
                            written += len(chunk)
                            percent = min(99, written * 100 // total_bytes)
                            if percent > last_percent:
                                report(f"{stage} {percent}%")
                                last_percent = percent
            destination = item_folder(identifier)
            ensure_folder(destination)
            try:
                os.link(packed, destination / saved_name)
            except FileExistsError:
                if not saved_exists(item_folder(identifier), saved_name):
                    raise
            report(f"{stage} 100%")

    def do_POST(self):
        if self.path != "/save":
            self.send_error(404)
            return
        streaming = False
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                self.send_error(400, "Invalid form length")
                return
            fields = parse_qs(self.rfile.read(size).decode("utf-8"))
            params = {k: v[0] for k, v in fields.items()}
            if params.get("token") != FORM_TOKEN:
                self.send_error(403, "Refresh the file list and try again")
                return
            streaming = self.headers.get("Accept") == "application/x-ndjson"
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.event(state="starting")
            self.save_zip(params, streaming)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (sqlite3.Error, OSError, HTTPException, ValueError, KeyError) as err:
            self.log_error("Save failed: %s", err)
            cause = err.reason if isinstance(err, URLError) else err
            if isinstance(err, HTTPError):
                message = f"Archive.org returned HTTP {err.code} while fetching this file."
            elif isinstance(cause, (socket.timeout, TimeoutError)):
                message = "Archive.org timed out."
            elif getattr(err, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
                message = "The server has run out of disk space or storage quota."
            elif isinstance(err, PermissionError):
                message = "The server cannot create or write to the project’s save/ folder."
            elif isinstance(err, ValueError):
                message = str(err)
            else:
                message = "The save was interrupted. Check the connection and try again."
            if streaming:
                self.event(state="error", message=message)
            else:
                self.send_error(502, message)

    def do_GET(self):
        path = urlsplit(self.path)
        params = {k:v[0] for k,v in parse_qs(path.query, keep_blank_values=True).items()}
        status, mime = 200, "text/html"
        try:
            if path.path == "/":
                body = downloads_page()
            elif path.path.startswith("/download/"):
                return self.download_saved(path.path[len("/download/"):])
            elif path.path in ("/home", "/search"):
                body = search(params)
            elif path.path == "/saved":
                name = params.get("file", "")
                folder = item_folder(params["id"]) if params.get("id") and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", params["id"]) else None
                if folder and saved_exists(folder, name):
                    relative_path = "save/" + folder.name + "/" + name
                    body = layout("File saved", f'<h1>File saved</h1><p>Project path: <strong>{relative_path}</strong>.</p><a href="{link("/files", id=params.get("id", ""))}">Return to files</a>')
                else:
                    status, body = 404, layout("Missing file", "<h1>Saved ZIP unavailable</h1>")
            elif path.path == "/files":
                body = files(params)
            elif path.path == "/save.js":
                body = (PROJECT_ROOT / "save.js").read_text(encoding="utf8")
                mime = "text/javascript"
            elif path.path == "/style.css":
                body = Path(__file__).with_name("style.css").read_text(encoding="utf8")
                mime = "text/css"
            else:
                status, body = 404, layout("Missing page","<h1>Page not found</h1><a href='/home'>Return to search</a>")
        except (sqlite3.Error, OSError, ValueError, KeyError) as err:
            self.log_error("Archive lookup failed: %s", err)
            status, body = 502, layout("Archive unavailable","<h1>Archive request failed</h1><p>Refresh to retry, or return to search.</p><a href='/home'>Return to search</a>")
        data = body.encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    port = parser.parse_args().port
    migrate_saved_files()
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Open http://127.0.0.1:{port} in your browser.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
