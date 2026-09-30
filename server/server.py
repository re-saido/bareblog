"""sumo: a tiny server-rendered blog.

Everything is a file in one flat store, written through an S3-style API.
A markdown file's frontmatter `type:` says what it is: a post, a page or settings.
"""

import datetime
import functools
import mimetypes
import os
import re
import secrets
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

import markdown
from flask import Flask, Response, abort, redirect, render_template, request
from markupsafe import Markup

import auth

FILES = Path(os.environ.get("BLOG_FILES", Path(__file__).parent.parent / "files"))
TOKEN = os.environ.get("BLOG_TOKEN") or secrets.token_urlsafe(24)
# Flat names with at most one extension, so `about.md` renders at /about and
# no page can shadow a route like /login.
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}(\.[a-z0-9]{1,10})?")
TYPES = ("post", "page", "settings")
SETTINGS_KEY = "_settings.md"  # internal files: the leading _ keeps them from being pages
INDEX_KEY = "_index.md"  # its body sits above the post list on the home page
RESERVED = {"admin", "edit", "files", "login", "logout", "passkeys"}  # routes, so not page names
SETTING_KEYS = {"type", "title", "subtitle", "powered_by"}
BOOLS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}
MAX_BODY = 10_000_000  # room for a phone photo
# Shown in the browser from /files/. Anything else (HTML, SVG, scripts...)
# downloads instead, so an uploaded file can't run as a page on the blog.
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "text/plain", "text/markdown"}
DEFAULT_SETTINGS = {
    "title": os.environ.get("BLOG_TITLE", "sumo"),
    "subtitle": "my new sumo",
    "powered_by": True,
    "template": "",
}
# New posts start with this, then the settings' template text.
NEW_POST = "---\ntitle: \n---\n\n"
# What the editor starts from when there's no settings file yet.
SETTINGS_TEXT = f"""---
type: settings
title: {DEFAULT_SETTINGS["title"]}
subtitle: {DEFAULT_SETTINGS["subtitle"]}
powered_by: true
---
"""

FILES.mkdir(parents=True, exist_ok=True)
AUTH = auth.Auth(os.environ.get("BLOG_AUTH", FILES / "_auth.json"), TOKEN, os.environ.get("BLOG_ORIGIN"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
app.url_map.strict_slashes = False  # /files/ lists the files too
app.jinja_env.trim_blocks = app.jinja_env.lstrip_blocks = True


def parse(text):
    """Split optional `---` fenced `key: value` frontmatter from the body."""
    meta, body = {}, text
    if text.startswith("---\n"):
        head, sep, rest = text[3:].partition("\n---\n")
        if sep:
            pairs = (l.split(":", 1) for l in head.splitlines() if ":" in l)
            meta = {k.strip().lower(): v.strip() for k, v in pairs}
            body = rest.lstrip("\n")
    meta["type"] = meta.get("type", "").lower() or "post"
    return meta, body


def title_of(name, meta):
    return meta.get("title") or name


app.jinja_env.globals |= {"title_of": title_of, "settings_key": SETTINGS_KEY, "max_body": MAX_BODY}


def slugify(title):
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:100].rstrip("-")


def stamp_date(text):
    """Give a post today's date if it has none. Used by the editor, not the API."""
    meta, body = parse(text)
    if meta["type"] != "post" or meta.get("date"):
        return text
    date = f"date: {datetime.date.today()}"
    if body == text:  # no frontmatter to add it to
        return f"---\n{date}\n---\n\n{text}"
    return text.replace("\n---\n", f"\n{date}\n---\n", 1)  # just before the closing fence


# The store: flat keys to bytes. Only check() knows anything about content.

def valid(key):
    return bool(KEY.fullmatch(key)) or key in (SETTINGS_KEY, INDEX_KEY)


def read(key):
    path = FILES / key
    return path.read_bytes() if valid(key) and path.is_file() else None


def keys():
    return sorted(p.name for p in FILES.iterdir() if valid(p.name) and p.is_file())


def write(path, data):
    """Write via a temp file + rename, so concurrent readers never see half a file."""
    tmp = path.with_name(f"_{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def check(key, data):
    """Why `data` can't be stored as `key`, or None if it can."""
    if not valid(key):
        return f"bad key {key!r}: use a-z, 0-9, - and _, plus one optional .extension"
    if not key.endswith(".md"):
        return None
    try:
        meta, _ = parse(data.decode().replace("\r\n", "\n"))
    except UnicodeDecodeError:
        return "markdown files must be UTF-8"
    kind, name = meta["type"], key.removesuffix(".md")
    if kind not in TYPES:
        return f"unknown type {kind!r}: use {', '.join(TYPES)}"
    if (kind == "settings") != (key == SETTINGS_KEY):
        return f"settings live in {SETTINGS_KEY}, and only there"
    if kind == "settings":
        if unknown := sorted(meta.keys() - SETTING_KEYS):
            return f"unknown setting {unknown[0]!r}; settings are {', '.join(sorted(SETTING_KEYS - {'type'}))}"
        if meta.get("powered_by", "true").lower() not in BOOLS:
            return "powered_by must be true or false"
    elif name in RESERVED:
        return f"/{name} is taken by sumo; pick another name"
    elif kind == "page" and meta.get("nav", "true").lower() not in BOOLS:
        return "nav must be true or false"
    return None


def put(key, data):
    """Store `data` as `key`. Returns an error message, or None once written."""
    error = check(key, data)
    if error is None:
        write(FILES / key, data.replace(b"\r\n", b"\n") if key.endswith(".md") else data)
    return error


def remove(key):
    if read(key) is None:
        return False
    (FILES / key).unlink()
    return True


# Reading the store as a blog.

def docs():
    """Every markdown file as (name, meta, body), in name order."""
    out = []
    for key in keys():
        if key.endswith(".md"):
            try:
                meta, body = parse((FILES / key).read_text())
            except (OSError, UnicodeDecodeError):  # deleted mid-scan, or put there by hand
                continue
            out.append((key.removesuffix(".md"), meta, body))
    return out


def date_key(date):
    """Sort key for a free-text `Y-M-D[ H:MM]` date, compared as numbers so
    2026-9-1 sorts before 2026-10-01. Anything else sorts as oldest."""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", date.strip())
    return tuple(int(g or 0) for g in m.groups()) if m else ()


def posts(all_docs=None):
    """Posts, newest first; ties on date stay in name order."""
    found = [(n, m) for n, m, _ in (all_docs or docs()) if m["type"] == "post" and not n.startswith("_")]
    return sorted(found, key=lambda p: date_key(p[1].get("date", "")), reverse=True)


def settings(all_docs=None):
    """Defaults, overridden by _settings.md. Its body is the text new posts start with."""
    conf = dict(DEFAULT_SETTINGS)
    for name, meta, body in all_docs or docs():
        if f"{name}.md" != SETTINGS_KEY:
            continue
        conf |= {k: meta[k] for k in ("title", "subtitle") if meta.get(k)}
        if "powered_by" in meta:
            conf["powered_by"] = BOOLS.get(meta["powered_by"].lower(), True)
        if body.strip():
            conf["template"] = body
    return conf




# Rendering and auth helpers.

def render(template, all_docs=None, **context):
    """Render a template inside the site layout. Pass `all_docs` if the view already loaded them."""
    all_docs = all_docs or docs()
    nav = [(n, title_of(n, m)) for n, m, _ in all_docs
           if m["type"] == "page" and not n.startswith("_") and BOOLS.get(m.get("nav", "true").lower(), True)]
    return render_template(template, conf=settings(all_docs), nav=nav, admin=is_admin(), **context)


@app.template_filter("markdown")
def markdown_html(text):
    return Markup(markdown.markdown(text, extensions=["fenced_code", "tables"]))


def plain(message, code=200, headers=None):
    return Response(message + "\n", code, headers, mimetype="text/plain")


def go(to):
    return redirect(to, 303)


def is_admin():
    """True if the request carries the token as a Bearer header or the session cookie."""
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if given:
        return AUTH.check_token(given)
    session = request.cookies.get("session")
    return bool(session) and AUTH.check_session(session)


def with_session(response, session):
    """Log this browser in. The cookie holds a session value, never the token."""
    response.set_cookie("session", session, max_age=31536000, httponly=True, samesite="Strict",
                        secure=(AUTH.origin or "").startswith("https:"))
    return response


def admin_only(api=False):
    """Guard a view. Browsers are sent to /login; API clients get 401, or 429 while rate limited."""
    def decorate(view):
        @functools.wraps(view)
        def guarded(*args, **kwargs):
            if is_admin():
                return view(*args, **kwargs)
            if not api:
                return go("/login")
            if request.headers.get("Authorization") and (wait := AUTH.token_wait()):
                return plain(f"too many wrong tokens; try again in {wait}s", 429, {"Retry-After": str(wait)})
            return plain("unauthorized", 401)
        return guarded
    return decorate


def passkeys_only(view):
    """Guard for the passkey JSON endpoints, which need BLOG_ORIGIN."""
    @functools.wraps(view)
    def guarded(*args, **kwargs):
        return view(*args, **kwargs) if AUTH.rp_id else plain("passkeys need BLOG_ORIGIN set", 400)
    return guarded


def json_body():
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else abort(plain("body must be a JSON object", 400))


@app.after_request
def no_sniffing(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(404)
def not_found(_):
    return render("notice.html", title="Not found"), 404


@app.errorhandler(413)
def too_big(_):
    return plain(f"body over {MAX_BODY} bytes", 413)


# The blog.

@app.get("/")
def index():
    all_docs = docs()
    intro = read(INDEX_KEY)
    intro = parse(intro.decode(errors="replace"))[1] if intro else ""
    return render("index.html", all_docs, posts=posts(all_docs), intro=intro)


@app.get("/<name>")
def show(name):
    data = None if name.startswith("_") else read(f"{name}.md")
    meta, body = parse(data.decode(errors="replace")) if data is not None else ({}, "")
    if meta.get("type") not in ("post", "page"):
        abort(404)
    return render("doc.html", name=name, meta=meta, body=body, title=title_of(name, meta))


# The file API.

@app.get("/files")
def list_files():
    return plain("".join(f"{k}\n" for k in keys()).rstrip("\n"))


@app.get("/files/<key>")
def get_file(key):
    data = read(key)
    if data is None:
        return plain("not found", 404)
    ctype = "text/markdown" if key.endswith(".md") else mimetypes.guess_type(key)[0] or "application/octet-stream"
    # sandbox: even if a file does render, it gets no scripts and no
    # access to the blog's origin (so none to the login cookie).
    headers = {"Content-Security-Policy": "sandbox"}
    if ctype not in INLINE_TYPES:
        headers["Content-Disposition"] = "attachment"
    return Response(data, headers=headers, mimetype=ctype)


@app.put("/files/<key>")
@admin_only(api=True)
def put_file(key):
    if request.content_length is None:
        return plain("Content-Length required", 411)
    data = request.get_data()
    if not data.strip():
        return plain("empty body (use DELETE to remove a file)", 400)
    if request.headers.get("If-None-Match") == "*" and read(key) is not None:
        return plain(f"{key} already exists", 412)
    if error := put(key, data):
        return plain(error, 400)
    return plain(f"saved {key}")


@app.delete("/files/<key>")
@admin_only(api=True)
def delete_file(key):
    return plain(f"deleted {key}") if remove(key) else plain("not found", 404)


# Logging in.

def login_page(error=""):
    return render("login.html", title="Log in", error=error, token_enabled=AUTH.token_enabled(),
                  passkeys=bool(AUTH.rp_id and AUTH.passkeys()), origin=AUTH.origin)


@app.get("/login")
def login_form():
    return login_page()


@app.post("/login")
def login():
    if AUTH.check_token(request.form.get("token", "")):
        return with_session(go("/"), AUTH.session())
    if wait := AUTH.token_wait():
        return login_page(f"Too many wrong tokens. Try again in {-(-wait // 60)} min."), 429, {"Retry-After": str(wait)}
    return login_page("Wrong token."), 401


@app.get("/logout")
def logout():
    response = go("/")
    response.delete_cookie("session")
    return response


# The passkey JSON calls behind the buttons: fetch options for the browser's
# prompt, then send back what it signed.

@app.post("/login/passkey/options")
@passkeys_only
def passkey_login_options():
    if not AUTH.passkeys():
        return plain("no passkeys yet", 400)
    return Response(AUTH.login_options(), mimetype="application/json")


@app.post("/login/passkey")
@passkeys_only
def passkey_login():
    session = AUTH.login(json_body())
    if session is None:
        return plain("passkey not accepted", 401)
    return with_session(Response("{}", mimetype="application/json"), session)


@app.post("/passkeys/options")
@passkeys_only
@admin_only(api=True)
def passkey_register_options():
    return Response(AUTH.registration_options(settings()["title"]), mimetype="application/json")


@app.post("/passkeys/add")
@passkeys_only
@admin_only(api=True)
def passkey_register():
    data = json_body()
    error = AUTH.register(data.get("credential"), str(data.get("name", "")))
    return plain(error, 400) if error else {}


# Admin pages.

def passkeys_page(error=""):
    return render("passkeys.html", title="Passkeys", error=error, passkeys=AUTH.passkeys(),
                  token_enabled=AUTH.token_enabled(), origin=AUTH.origin)


@app.get("/passkeys")
@admin_only()
def passkeys_form():
    return passkeys_page()


@app.post("/passkeys")
@admin_only()
def passkeys_change():
    action, form = request.form.get("action"), request.form
    if action == "remove":
        error = AUTH.remove_passkey(form.get("id", ""))
    elif action in ("token-off", "token-on"):
        error = AUTH.set_token_enabled(action == "token-on")
        if error is None:
            return go("/login")  # every session just ended
    else:
        error = "unknown action"
    return (passkeys_page(error), 400) if error else go("/passkeys")


@app.get("/admin")
@admin_only()
def admin():
    rows = []
    for key in keys():
        is_md = key.endswith(".md")
        kind = parse((FILES / key).read_text(errors="replace"))[0]["type"] if is_md else "file"
        rows.append((kind, key, f"/edit?key={key}" if is_md else f"/files/{key}"))
    return render("admin.html", title="Files", rows=rows)


# The editor.

def editable(key, data):
    """The text the editor would show for a file, or None if saving that text
    back could change the file (it isn't markdown, or isn't valid UTF-8)."""
    if not key.endswith(".md"):
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


NOT_EDITABLE = "the editor only edits UTF-8 .md files; use PUT /files/<key> for anything else"


def editor_page(old, key, text, error=""):
    """`old` is the file being edited ("" for a new one), `key` the name to fall back on."""
    return render("edit.html", title="Edit", old=old, key=key, text=text, error=error)


@app.get("/edit")
@admin_only()
def edit_form():
    key = request.args.get("key", "")
    data = read(key)
    if data is None:  # a new file
        default = SETTINGS_TEXT if key == SETTINGS_KEY else NEW_POST + settings()["template"]
        return editor_page("", key, default)
    if (existing := editable(key, data)) is None:
        return render("notice.html", title=f"Can't edit {key}", message=NOT_EDITABLE), 400
    return editor_page(key, key, existing)


def editor_key(old, key, meta):
    """The name a save goes under: a `slug:` wins, then the name it already has,
    then the title. Other files (images, say) keep the name they came with."""
    if "slug" in meta or not (old or key):
        name = slugify(meta.get("slug") or meta.get("title", ""))
        return SETTINGS_KEY if meta["type"] == "settings" else f"{name}.md" if name else ""
    if old:
        return old
    return key if "." in key else key + ".md"


def save_error(old, key):
    """Why the editor can't save as `key`, or None."""
    old_data = read(old)
    if not key:
        return "add a title, or a slug: in the frontmatter"
    if not key.endswith(".md") or (old_data is not None and editable(old, old_data) is None):
        return NOT_EDITABLE
    if key != old and read(key) is not None:
        return f"{key} already exists"
    return None


@app.post("/edit")
@admin_only()
def edit_save():
    old, given_key = request.form.get("old", ""), request.form.get("key", "").strip()
    text = request.form.get("text", "").replace("\r\n", "\n")
    if request.form.get("action") == "delete":
        remove(old)
        return go("/admin")
    meta = parse(text)[0]
    key = editor_key(old, given_key, meta)
    if not old:
        text = stamp_date(text)
    if error := save_error(old, key) or put(key, text.encode()):
        return editor_page(old, key, text, error), 400
    if old and old != key:
        remove(old)
    if key == INDEX_KEY:
        return go("/")
    name = key.removesuffix(".md")
    return go(f"/{quote(name)}" if meta["type"] in ("post", "page") else "/admin")


if __name__ == "__main__":
    if sys.argv[1:] == ["enable-token"]:
        AUTH.set_token_enabled(True)
        print("token turned on; every browser has been logged out")
        sys.exit()
    host = os.environ.get("BLOG_HOST", "127.0.0.1")
    port = int(os.environ.get("BLOG_PORT", "8000"))
    if not AUTH.token_enabled():
        print("token is off: log in with a passkey (`server.py enable-token` turns it back on)")
    elif "BLOG_TOKEN" not in os.environ:
        print(f"BLOG_TOKEN not set, using a random one for this run: {TOKEN}")
    print(f"passkeys: {AUTH.origin or 'off (set BLOG_ORIGIN to this blog’s https:// address)'}")
    app.run(host, port, threaded=True)
