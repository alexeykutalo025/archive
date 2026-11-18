"""Archive Index. Run with Python 3.8 or newer."""
import argparse
import json
import hashlib
import os
import errno
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
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

ARCHIVE = "https://archive.org"
TYPES = {"": "All media", "texts": "Texts", "audio": "Audio", "movies": "Video",
         "software": "Software", "image": "Images", "web": "Web", "data": "Data"}
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
    media = params.get("media", "")
    sort = params.get("sort", "")
    year = params.get("year", "").strip()
    controls = f"""<h1>Search the archive<span>.</span></h1>
    <form action="/search"><label>Search terms<input type="search" name="q" placeholder="Title, creator, or subject" value="{esc(term)}"></label>
    <div class="controls">{select("media","Media type",TYPES,media)}
    <label>Year<input name="year" inputmode="numeric" pattern="[0-9]{{4}}" placeholder="Any year" value="{esc(year)}"></label>
    {select("sort","Sort by",SORT,sort)}<button>Search</button></div></form>"""
    if not params:
        topics = ["Space exploration", "Jazz", "Silent film", "Natural history"]
        items = "".join(f'<a class="topic" href="{link("/search", q=t)}">{t} →</a>' for t in topics)
        return layout("Search", controls + '<h2>Browse by topic</h2>' + items)
    if year and not re.fullmatch(r"[0-9]{4}", year):
        return layout("Search", controls + "<p>Enter a four-digit year.</p>")
    if media not in TYPES or sort not in SORT:
        return layout("Search", controls + "<p>Select a listed filter and search again.</p>")
    # Treat punctuation as text separators, keeping Archive query syntax internal.
    words = re.findall(r"[^\W_]+", term, re.UNICODE)
    if term and not words:
        return layout("Search", controls + "<p>Enter a word or number to search.</p>")
    clauses = ['"' + w + '"' for w in words]
    clauses.append("mediatype:" + media if media else "NOT mediatype:collection")
    if year:
        clauses.append("year:" + year)
    if clauses[0].startswith("NOT "):
        clauses.insert(0, "*:*")
    page = page_number(params)
    args = {"q": " AND ".join(clauses), "fl[]": ["identifier","title","creator","year","mediatype"],
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
        meta = " · ".join(esc(v) for v in [TYPES.get(doc.get("mediatype"),doc.get("mediatype")),doc.get("year"),creator] if v)
        rows.append(f'<article><a href="{link("/files", id=identifier)}">{esc(doc.get("title") or identifier)}</a><small>{meta}</small></article>')
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
    body = f"""<a href="/">← Search</a><h1>{esc(title)}</h1><a href="{ARCHIVE}/details/{ident}">View on archive.org ↗</a>
    <form action="/files" class="controls"><input type="hidden" name="id" value="{ident}">
    <label>Filename<input name="name" value="{esc(name)}" placeholder="Filter by filename"></label>
    {select("format","File format",{"":"All formats", **{x:x for x in formats}},fmt)}<button>Filter files</button></form>
    <h2>{len(matches):,} {'file' if len(matches) == 1 else 'files'}</h2>"""
    if matches:
        body += "<p>Encrypted ZIPs are saved in the project’s <code>save/</code> folder.</p>"
    for f in matches[start:start + LIMIT]:
        filename = f["name"]
        if any(part in (".","..") for part in filename.split("/")):
            continue
        detail = " · ".join(esc(x) for x in [f.get("format"),file_size(f.get("size"))] if x)
        body += f"""<article class="file"><div>{esc(filename)}<small>{detail}</small></div>
        <form method="post" action="/save" class="save-file">
        <input type="hidden" name="token" value="{FORM_TOKEN}">
        <input type="hidden" name="id" value="{esc(ident)}">
        <input type="hidden" name="name" value="{esc(filename)}">
        <button aria-label="Save {esc(filename)} to server">Click</button>
        <small class="status" role="status"></small><small class="progress"></small>
        <code class="saved-name"></code><button type="button" class="copy" hidden>Copy filename</button>
        </form></article>"""
    if not matches:
        body += "<p>No matching files. Change the filters or check the item on archive.org.</p>"
    return layout(title, body + pager("/files", params, page, len(matches)) + '<script src="/save.js" defer></script>')


class Handler(BaseHTTPRequestHandler):
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
        item = api("/metadata/" + identifier)
        entry = next((f for f in item.get("files", []) if f.get("name") == filename
                      and str(f.get("private", "")).lower() not in ("true", "1")), None)
        if entry is None:
            raise ValueError("This file is unavailable on archive.org.")
        member = re.sub(r'[:<>"|?*]', "_", parts[-1])
        request = Request(f"{ARCHIVE}/download/{identifier}/{quote(filename)}",
                          headers={"User-Agent": "ArchiveIndex/1.0"})
        SAVE_DIR.mkdir(mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=SAVE_DIR, prefix=".saving-", suffix=".part") as output:
            with urlopen(request, timeout=45) as source, pyzipper.AESZipFile(
                output, "w", compression=pyzipper.ZIP_DEFLATED
            ) as archive:
                archive.setpassword(ZIP_PASSWORD)
                archive.setencryption(pyzipper.WZ_AES, nbits=256)
                digest = hashlib.sha1()
                size = 0
                last_update = 0.0
                total = entry.get("size") or source.headers.get("Content-Length")
                # The ZIP directory is public. Put the original directory entry
                # inside the encrypted payload along with the file data.
                with archive.open("contents.zip", "w", force_zip64=True) as payload, \
                        zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as inner, \
                        inner.open(member, "w", force_zip64=True) as target:
                    for chunk in iter(lambda: source.read(256 * 1024), b""):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        now = time.monotonic()
                        if streaming and now - last_update >= 0.4:
                            self.event(state="progress", received=size, total=total)
                            last_update = now
                if entry.get("size") is not None and size != int(entry["size"]):
                    raise ValueError("Incomplete archive.org file")
                if entry.get("sha1") and digest.hexdigest() != entry["sha1"].lower():
                    raise ValueError("Archive.org file checksum mismatch")
            output.flush()
            saved_name = f"archive-{secrets.token_hex(12)}.zip"
            os.link(output.name, SAVE_DIR / saved_name)
        if streaming:
            self.event(state="saved", filename=saved_name)
            return
        self.send_response(303)
        self.send_header("Location", "/saved?" + urlencode({"file": saved_name, "id": identifier}))
        self.end_headers()

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
        except (OSError, HTTPException, ValueError, KeyError) as err:
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
            if path.path in ("/", "/search"):
                body = search(params)
            elif path.path == "/saved":
                name = params.get("file", "")
                if re.fullmatch(r"archive-[0-9a-f]{24}\.zip", name) and ((SAVE_DIR / name).is_file() or (PROJECT_ROOT / name).is_file()):
                    relative_path = "save/" + name if (SAVE_DIR / name).is_file() else name
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
                status, body = 404, layout("Missing page","<h1>Page not found</h1><a href='/'>Return to search</a>")
        except (OSError, ValueError, KeyError) as err:
            self.log_error("Archive lookup failed: %s", err)
            status, body = 502, layout("Archive unavailable","<h1>Archive request failed</h1><p>Refresh to retry, or return to search.</p><a href='/'>Return to search</a>")
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
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Open http://127.0.0.1:{port} in your browser.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
