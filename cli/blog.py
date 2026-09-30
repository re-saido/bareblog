#!/usr/bin/env python3
"""sumo CLI: a client for the /files API.

  blog.py ls                list files
  blog.py cat <key>         print a file
  blog.py put <key> [file]  create/replace a file from file (or stdin)
  blog.py edit <key>        edit a file in $EDITOR and upload it
  blog.py rm <key>          delete a file

A key without an extension means <key>.md, so `blog.py edit about` edits about.md.
Env: BLOG_URL (default http://127.0.0.1:8000), BLOG_TOKEN (for put/edit/rm).
"""

import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

URL = os.environ.get("BLOG_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("BLOG_TOKEN", "")


def req(method, path, data=None):
    r = urllib.request.Request(URL + path, data=data, method=method)
    if method != "GET":  # reads are public; don't send the token where it isn't needed
        r.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        sys.exit(f"{e.code}: {e.read().decode().strip()}")
    except urllib.error.URLError as e:
        sys.exit(f"can't reach {URL}: {e.reason}")


def out(data):
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def main(cmd=None, key=None, file=None, *_):
    if key and "." not in key:
        key += ".md"
    if cmd == "ls":
        out(req("GET", "/files/"))
    elif cmd == "cat" and key:
        data = req("GET", f"/files/{key}")
        if data is None:
            sys.exit("not found")
        out(data)
    elif cmd == "put" and key:
        if file:
            with open(file, "rb") as f:
                data = f.read()
        else:
            data = sys.stdin.buffer.read()
        out(req("PUT", f"/files/{key}", data))
    elif cmd == "edit" and key:
        old = req("GET", f"/files/{key}")
        text = old.decode() if old is not None else f"---\ntitle: {key.removesuffix('.md')}\n---\n\n"
        # Re-read by path afterwards: many editors save by writing a new file
        # and renaming it over the old one, so an open handle would go stale.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, key)
            with open(path, "w") as f:
                f.write(text)
            subprocess.run([*shlex.split(os.environ.get("EDITOR", "vi")), path], check=True)
            with open(path) as f:
                new = f.read()
        if new == text:
            return print("no changes")
        out(req("PUT", f"/files/{key}", new.encode()))
    elif cmd == "rm" and key:
        out(req("DELETE", f"/files/{key}"))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])
