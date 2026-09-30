"""Tests for sumo. Run with: python3 -m unittest discover tests"""

import base64
import hashlib
import http.client
import json
import os
import secrets
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import cbor2
from werkzeug.serving import make_server
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "server"), str(REPO / "cli")]

# server.py reads its config at import time.
os.environ["BLOG_TOKEN"] = "test-token"
os.environ["BLOG_FILES"] = tempfile.mkdtemp()

import auth  # noqa: E402
import blog  # noqa: E402
import server  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}
ORIGIN = "https://blog.test"
SETTINGS = b"---\ntype: settings\ntitle: My blog\nsubtitle: notes\npowered_by: no\n---\n\nStart here\n"


class Isolated(unittest.TestCase):
    """Gives each test an empty file store."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.files = Path(tmp.name)
        for name, value in [("FILES", self.files),
                            ("AUTH", auth.Auth(self.files / "_auth.json", "test-token", ORIGIN))]:
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestStore(Isolated):
    def test_parse_frontmatter(self):
        meta, body = server.parse("---\nTitle: Hi: there\ndate: 2026-01-02\n---\n\nbody\n")
        self.assertEqual(meta, {"title": "Hi: there", "date": "2026-01-02", "type": "post"})
        self.assertEqual(body, "body\n")

    def test_parse_without_frontmatter_is_a_post(self):
        meta, body = server.parse("just text")
        self.assertEqual(meta, {"type": "post"})
        self.assertEqual(body, "just text")

    def test_slugify(self):
        self.assertEqual(server.slugify("My Post!"), "my-post")
        self.assertEqual(server.slugify("Café crème"), "cafe-creme")
        self.assertEqual(server.slugify("日本語"), "")

    def test_put_stores_bytes_as_given(self):
        for key, data in [("a.md", b"no frontmatter, no date"), ("cat.png", b"\x89PNG\xff")]:
            with self.subTest(key=key):
                self.assertIsNone(server.put(key, data))
                self.assertEqual(server.read(key), data)

    def test_put_rejects_bad_keys_and_content(self):
        for key, data in [
            ("../etc", b"x"),                        # path traversal
            ("a/b.md", b"x"),                        # flat store only
            ("a.b.md", b"x"),                        # one extension, so no /a.b shadowing
            ("foo\n", b"x"),                         # `$` alone would allow a trailing newline
            ("_hidden", b"x"),                       # internal files start with an underscore
            ("login.md", b"x"),                      # would shadow /login
            ("a.md", b"\xff"),                       # markdown must be text
            ("a.md", b"---\ntype: poem\n---\n"),     # unknown type
            ("s.md", b"---\ntype: settings\ncolour: red\n---\n"),
            ("s.md", b"---\ntype: settings\npowered_by: maybe\n---\n"),
            ("a.md", b"---\ntype: page\nnav: maybe\n---\n"),
        ]:
            with self.subTest(key=key, data=data):
                self.assertIsNotNone(server.put(key, data))
        self.assertEqual(server.keys(), [])

    def test_posts_sort_newest_first_by_numeric_date(self):
        for name, date in [("a", "2026-9-1"), ("b", "2026-10-01"), ("c", "someday"), ("d", "2025-12-31")]:
            server.put(f"{name}.md", f"---\ndate: {date}\n---\n\nx".encode())
        server.put("about.md", b"---\ntype: page\ndate: 2030-01-01\n---\n\nx")
        self.assertEqual([n for n, _ in server.posts()], ["b", "a", "d", "c"])

    def test_settings_come_from_settings_file(self):
        self.assertEqual(server.settings(), server.DEFAULT_SETTINGS)
        server.put("_settings.md", SETTINGS)
        conf = server.settings()
        self.assertEqual((conf["title"], conf["subtitle"], conf["powered_by"]), ("My blog", "notes", False))
        self.assertEqual(conf["template"], "Start here\n")

    def test_settings_only_live_in_the_settings_file(self):
        self.assertIn("_settings.md", server.put("other.md", b"---\ntype: settings\n---\n"))
        self.assertIn("_settings.md", server.put("_settings.md", b"---\ntype: post\n---\n"))

    def test_pages_can_be_hidden_from_the_nav(self):
        server.put("about.md", b"---\ntype: page\ntitle: About me\n---\n")
        server.put("secret.md", b"---\ntype: page\ntitle: Secret\nnav: false\n---\n")
        html = server.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('href="/about"', html)
        self.assertNotIn('href="/secret"', html)

    def test_stamp_date(self):
        self.assertRegex(server.stamp_date("---\ntitle: x\n---\n\nhi"), r"^---\ntitle: x\ndate: \d{4}-\d\d-\d\d\n---\n\nhi$")
        self.assertRegex(server.stamp_date("hi"), r"^---\ndate: \d{4}-\d\d-\d\d\n---\n\nhi$")
        for text in ("---\ndate: 2020-01-01\n---\n\nhi", "---\ntype: page\n---\n\nhi"):
            self.assertEqual(server.stamp_date(text), text)


class Served(Isolated):
    """Runs the server on a free port for the tests to talk to."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read().decode(errors="replace")

    def login(self):
        resp, _ = self.request("POST", "/login", b"token=test-token",
                               {"Content-Type": "application/x-www-form-urlencoded"})
        return {"Cookie": resp.getheader("Set-Cookie").split(";")[0],
                "Content-Type": "application/x-www-form-urlencoded"}


class TestHTTP(Served):
    def test_file_api_round_trip(self):
        resp, _ = self.request("PUT", "/files/hi.md", b"---\ntitle: <b>Hi</b>\n---\n\nbody", AUTH)
        self.assertEqual(resp.status, 200)
        resp, raw = self.request("GET", "/files/hi.md")
        self.assertEqual(raw, "---\ntitle: <b>Hi</b>\n---\n\nbody")  # stored as sent: no slug or date added
        self.assertTrue(resp.getheader("Content-Type").startswith("text/markdown"))
        _, listing = self.request("GET", "/files/")
        self.assertEqual(listing, "hi.md\n")
        _, page = self.request("GET", "/hi")
        self.assertIn("&lt;b&gt;Hi&lt;/b&gt;", page)
        resp, _ = self.request("DELETE", "/files/hi.md", headers=AUTH)
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("GET", "/hi")
        self.assertEqual(resp.status, 404)

    def test_index_file_is_shown_above_the_posts_and_is_not_a_post(self):
        server.put("hello.md", b"---\ntitle: Hello\n---\n\nx")
        self.assertIsNone(server.put("_index.md", b"Welcome, **friend**"))
        self.assertEqual([n for n, _ in server.posts()], ["hello"])
        _, page = self.request("GET", "/")
        self.assertIn("<strong>friend</strong>", page)
        self.assertIn("Hello", page)
        self.assertEqual(self.request("GET", "/_index")[0].status, 404)

    def test_saving_the_index_from_the_editor_goes_home(self):
        resp, _ = self.request("POST", "/edit", urllib.parse.urlencode({"old": "", "key": "_index.md", "text": "Hi", "action": "save"}).encode(),
                               {**self.login(), "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/"))

    def test_type_decides_how_a_file_renders(self):
        self.request("PUT", "/files/post.md", b"---\ntitle: A post\ndate: 2026-01-01\n---\n\npost body", AUTH)
        self.request("PUT", "/files/about.md", b"---\ntype: page\ntitle: About me\n---\n\npage body", AUTH)
        self.request("PUT", "/files/_settings.md", SETTINGS, AUTH)
        self.request("PUT", "/files/cat.png", b"\x89PNG", AUTH)
        _, index = self.request("GET", "/")
        self.assertIn('href="/post">A post<', index)
        self.assertIn('<nav><a href="/">Home</a> <a href="/about">About me</a></nav>', index)  # pages go in the nav
        self.assertEqual(index.count("About me"), 1)  # ...not the post list
        self.assertIn("<h2>My blog</h2>", index)
        self.assertIn('class="subtitle">notes<', index)
        self.assertNotIn("Powered by", index)
        _, about = self.request("GET", "/about")
        self.assertIn("page body", about)
        self.assertNotIn("<time>", about)
        resp, _ = self.request("GET", "/settings")
        self.assertEqual(resp.status, 404)  # settings aren't a page
        resp, _ = self.request("GET", "/cat")
        self.assertEqual(resp.status, 404)
        resp, _ = self.request("GET", "/files/cat.png")
        self.assertEqual(resp.getheader("Content-Type"), "image/png")

    def test_writes_need_the_token(self):
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                resp, _ = self.request(method, "/files/hi.md", b"x", {"Authorization": "Bearer wrong"})
                self.assertEqual(resp.status, 401)
        resp, _ = self.request("PUT", "/hi", b"x", AUTH)
        self.assertEqual(resp.status, 405)  # writes only go through /files/
        self.assertEqual(server.keys(), [])

    def test_bad_puts_keep_the_old_file(self):
        self.request("PUT", "/files/hi.md", b"original", AUTH)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("PUT", "/files/hi.md")  # chunked-style upload: no Content-Length
        conn.putheader(*next(iter(AUTH.items())))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        for body, status in [(b"  \n", 400), (b"---\ntype: poem\n---\n", 400)]:
            with self.subTest(status=status):
                resp, _ = self.request("PUT", "/files/hi.md", body, AUTH)
                self.assertEqual(resp.status, status)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("PUT", "/files/hi.md")  # too big: refused from the header, before any body is read
        conn.putheader(*next(iter(AUTH.items())))
        conn.putheader("Content-Length", str(server.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        self.assertEqual(server.read("hi.md"), b"original")

    def test_uploads_that_could_run_as_pages_download_instead(self):
        for key, data in [("x.html", b"<script>alert(1)</script>"), ("x.svg", b"<svg onload='alert(1)'/>"),
                          ("x.xml", b"<x/>"), ("x.js", b"alert(1)"), ("x.bin", b"\x00")]:
            server.put(key, data)
            with self.subTest(key=key):
                resp, _ = self.request("GET", f"/files/{key}")
                self.assertEqual(resp.getheader("Content-Disposition"), "attachment")
                self.assertEqual(resp.getheader("Content-Security-Policy"), "sandbox")
        for key, data in [("cat.png", b"\x89PNG"), ("a.md", b"hi"), ("a.txt", b"hi")]:
            server.put(key, data)
            with self.subTest(key=key):
                resp, _ = self.request("GET", f"/files/{key}")
                self.assertIsNone(resp.getheader("Content-Disposition"))  # shown in the browser
                self.assertEqual(resp.getheader("Content-Security-Policy"), "sandbox")

    def test_wrong_tokens_are_rate_limited(self):
        cookie = self.login()
        for i in range(auth.MAX_WRONG_TOKENS):
            resp, _ = self.request("PUT", "/files/a.md", b"x", {"Authorization": "Bearer wrong"})
            self.assertEqual(resp.status, 401 if i < auth.MAX_WRONG_TOKENS - 1 else 429)  # the last one hits the limit
        resp, _ = self.request("PUT", "/files/a.md", b"x", AUTH)  # even the right token, for now
        self.assertEqual(resp.status, 429)
        self.assertGreater(int(resp.getheader("Retry-After")), 0)
        resp, page = self.request("POST", "/login", b"token=test-token")
        self.assertEqual(resp.status, 429)
        self.assertIn("Too many wrong tokens", page)
        self.assertEqual(self.request("GET", "/admin", headers=cookie)[0].status, 200)  # sessions still work
        with server.AUTH._lock:  # let the window pass
            server.AUTH._wrong_tokens = type(server.AUTH._wrong_tokens)(
                t - auth.WRONG_TOKEN_WINDOW for t in server.AUTH._wrong_tokens)
        self.assertEqual(self.request("PUT", "/files/a.md", b"x", AUTH)[0].status, 200)

    def test_login_cookie_is_not_the_token(self):
        cookie = self.login()["Cookie"]
        self.assertNotIn("test-token", cookie)
        resp, _ = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("GET", "/admin", headers={"Cookie": "session=test-token"})
        self.assertEqual(resp.status, 303)  # the raw token doesn't work as a cookie

    def test_editor_names_new_posts_dates_them_and_renames(self):
        headers = self.login()
        resp, _ = self.request("POST", "/edit", b"old=&key=&text=---%0Atitle: Hello World%0A---%0A%0Ahi", headers)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/hello-world"))
        meta, _ = server.parse(server.read("hello-world.md").decode())
        self.assertRegex(meta["date"], r"^\d{4}-\d\d-\d\d$")
        resp, _ = self.request("POST", "/edit", b"old=hello-world.md&key=hello-world.md&text=---%0Atype: page%0Aslug: Hi There%0A---%0A%0Ahi", headers)
        self.assertEqual(resp.getheader("Location"), "/hi-there")
        self.assertEqual(server.keys(), ["hi-there.md"])
        resp, _ = self.request("POST", "/edit", b"old=hi-there.md&key=hi-there.md&text=---%0Atitle: Other%0A---%0A%0Ahi", headers)
        self.assertEqual(resp.getheader("Location"), "/hi-there")  # editing keeps the name unless slug: says otherwise
        self.assertEqual(server.keys(), ["hi-there.md"])

    def test_editor_image_upload_puts_with_the_cookie_without_overwriting(self):
        cookie = {"Cookie": self.login()["Cookie"]}
        _, page = self.request("GET", "/edit", headers=cookie)
        self.assertIn('id="image"', page)
        resp, _ = self.request("PUT", "/files/cat.png", b"\x89PNG one", {**cookie, "If-None-Match": "*"})
        self.assertEqual(resp.status, 200)
        resp, _ = self.request("PUT", "/files/cat.png", b"\x89PNG two", {**cookie, "If-None-Match": "*"})
        self.assertEqual(resp.status, 412)
        self.assertEqual(server.read("cat.png"), b"\x89PNG one")
        resp, _ = self.request("PUT", "/files/cat.png", b"\x89PNG two", cookie)  # unconditional PUT replaces
        self.assertEqual(server.read("cat.png"), b"\x89PNG two")

    def test_editor_leaves_non_markdown_files_alone(self):
        headers = self.login()
        png = b"\x89PNG\r\n\x00\xff"
        server.put("cat.png", png)
        (self.files / "bad.md").write_bytes(b"---\ntitle: x\n---\n\xff")  # put there by hand
        for key in ("cat.png", "bad.md"):
            with self.subTest(key=key):
                resp, _ = self.request("GET", f"/edit?key={key}", headers=headers)
                self.assertEqual(resp.status, 400)
                resp, _ = self.request("POST", "/edit", f"old={key}&key={key}&text=x".encode(), headers)
                self.assertEqual(resp.status, 400)
        resp, _ = self.request("POST", "/edit", b"old=&key=new.txt&text=x", headers)
        self.assertEqual(resp.status, 400)
        self.assertEqual(server.read("cat.png"), png)
        self.assertEqual(server.read("bad.md"), b"---\ntitle: x\n---\n\xff")
        self.assertIsNone(server.read("new.txt"))

    def test_new_posts_start_with_a_title_then_the_template(self):
        server.put("_settings.md", SETTINGS)
        _, page = self.request("GET", "/edit", headers=self.login())
        self.assertIn(">---\ntitle: \n---\n\nStart here\n</textarea>", page)

    def test_editor_opens_default_settings_when_there_are_none(self):
        _, page = self.request("GET", "/edit?key=_settings.md", headers=self.login())
        self.assertIn("type: settings", page)

    def test_cli_edit_with_an_editor_that_saves_by_rename(self):
        self.request("PUT", "/files/hi.md", b"before", AUTH)
        editor = Path(tempfile.mkdtemp()) / "editor.py"
        editor.write_text(
            "import os, sys\n"
            "path = sys.argv[-1]\n"
            "text = open(path).read().replace('before', 'after')\n"
            "open(path + '.new', 'w').write(text)\n"
            "os.replace(path + '.new', path)\n"
        )
        env = {"EDITOR": f"{sys.executable} {editor} --wait"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(blog, "URL", f"http://127.0.0.1:{self.port}"), \
             mock.patch.object(blog, "TOKEN", "test-token"), \
             mock.patch("sys.stdout"):
            blog.main("edit", "hi")
        self.assertEqual(server.read("hi.md"), b"after")


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftPasskey:
    """A software authenticator: a real P-256 key signing real WebAuthn
    responses, so tests go through the same verification a browser's would."""

    def __init__(self, origin=ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id, self.origin, self.count = secrets.token_bytes(16), origin, 0

    def _client_data(self, kind, options):
        return json.dumps({"type": kind, "challenge": options["challenge"], "origin": self.origin}).encode()

    def _auth_data(self, rp_id, attested=b""):
        self.count += 1
        flags = 0x01 | 0x04 | (0x40 if attested else 0)  # user present, user verified, has a key
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + attested

    def create(self, options):
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth_data = self._auth_data(options["rp"]["id"], bytes(16) + struct.pack(">H", len(self.id)) + self.id + cose)
        return {"id": b64(self.id), "rawId": b64(self.id), "type": "public-key", "response": {
            "clientDataJSON": b64(self._client_data("webauthn.create", options)),
            "attestationObject": b64(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})),
        }}

    def get(self, options):
        client_data, auth_data = self._client_data("webauthn.get", options), self._auth_data(options["rpId"])
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.id), "rawId": b64(self.id), "type": "public-key", "response": {
            "clientDataJSON": b64(client_data), "authenticatorData": b64(auth_data), "signature": b64(signature),
        }}


class TestPasskeys(Served):
    def post_json(self, path, body, headers=None):
        return self.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})

    def cookie(self):
        return {"Cookie": self.login()["Cookie"]}

    def add_passkey(self, key, cookie):
        _, options = self.post_json("/passkeys/options", {}, cookie)
        return self.post_json("/passkeys/add", {"name": "Phone", "credential": key.create(json.loads(options))}, cookie)[0]

    def passkey_login(self, key):
        """The session cookie from logging in with `key`, or None."""
        _, options = self.post_json("/login/passkey/options", {})
        resp, _ = self.post_json("/login/passkey", key.get(json.loads(options)))
        return {"Cookie": resp.getheader("Set-Cookie").split(";")[0]} if resp.status == 200 else None

    def form(self, path, body, cookie):
        return self.request("POST", path, body, {**cookie, "Content-Type": "application/x-www-form-urlencoded"})[0]

    def test_add_a_passkey_then_log_in_with_it(self):
        key = SoftPasskey()
        self.assertEqual(self.add_passkey(key, self.cookie()).status, 200)
        _, page = self.request("GET", "/passkeys", headers=self.cookie())
        self.assertIn("Phone", page)
        _, options = self.post_json("/login/passkey/options", {})
        resp, _ = self.post_json("/login/passkey", key.get(json.loads(options)))
        self.assertEqual(resp.status, 200)
        self.assertIn("Secure", resp.getheader("Set-Cookie"))  # BLOG_ORIGIN is https
        cookie = {"Cookie": resp.getheader("Set-Cookie").split(";")[0]}
        self.assertEqual(self.request("GET", "/admin", headers=cookie)[0].status, 200)

    def test_turning_off_the_token(self):
        token_cookie = self.cookie()
        self.assertEqual(self.form("/passkeys", b"action=token-off", token_cookie).status, 400)  # no passkey yet
        key = SoftPasskey()
        self.add_passkey(key, token_cookie)
        self.assertEqual(self.form("/passkeys", b"action=token-off", token_cookie).status, 303)
        self.assertEqual(self.request("PUT", "/files/a.md", b"x", AUTH)[0].status, 401)
        self.assertEqual(self.request("POST", "/login", b"token=test-token")[0].status, 401)
        self.assertEqual(self.request("GET", "/admin", headers=token_cookie)[0].status, 303)  # logged out
        _, login_page = self.request("GET", "/login")
        self.assertNotIn('name="token"', login_page)
        cookie = self.passkey_login(key)
        self.assertEqual(self.request("PUT", "/files/a.md", b"x", cookie)[0].status, 200)
        self.assertEqual(self.form("/passkeys", b"action=token-on", cookie).status, 303)
        self.assertEqual(self.request("PUT", "/files/b.md", b"x", AUTH)[0].status, 200)

    def test_cant_remove_the_last_passkey_while_the_token_is_off(self):
        key = SoftPasskey()
        self.add_passkey(key, self.cookie())
        self.form("/passkeys", b"action=token-off", self.cookie())
        cookie = self.passkey_login(key)
        passkey_id = server.AUTH.passkeys()[0]["id"]
        self.assertEqual(self.form("/passkeys", f"action=remove&id={passkey_id}".encode(), cookie).status, 400)
        self.assertEqual(len(server.AUTH.passkeys()), 1)

    def test_bad_passkey_responses_are_refused(self):
        key = SoftPasskey()
        cookie = self.cookie()
        self.assertEqual(self.add_passkey(SoftPasskey(origin="https://evil.test"), cookie).status, 400)
        for path in ("/passkeys/options", "/passkeys/add"):  # adding needs you logged in
            self.assertEqual(self.post_json(path, {})[0].status, 401)
        self.add_passkey(key, cookie)
        _, options = self.post_json("/login/passkey/options", {})
        response = key.get(json.loads(options))
        self.assertEqual(self.post_json("/login/passkey", response)[0].status, 200)
        self.assertEqual(self.post_json("/login/passkey", response)[0].status, 401)  # a challenge works once
        self.assertIsNone(self.passkey_login(SoftPasskey()))  # a key that was never added
        for body in (b"[]", b"nope", b'{"response": 1}'):
            with self.subTest(body=body):
                resp, _ = self.request("POST", "/login/passkey", body, {"Content-Type": "application/json"})
                self.assertIn(resp.status, (400, 401))

    def test_auth_file_is_out_of_reach_of_the_file_api(self):
        self.add_passkey(SoftPasskey(), self.cookie())
        self.assertTrue((self.files / "_auth.json").exists())
        self.assertEqual(self.request("GET", "/files/_auth.json")[0].status, 404)
        self.assertEqual(self.request("PUT", "/files/_auth.json", b"{}", AUTH)[0].status, 400)
        self.assertNotIn("_auth.json", self.request("GET", "/files/")[1])
        self.assertEqual((self.files / "_auth.json").stat().st_mode & 0o777, 0o600)

    def test_enable_token_command_recovers_a_lockout(self):
        self.add_passkey(SoftPasskey(), self.cookie())
        server.AUTH.set_token_enabled(False)
        env = {**os.environ, "BLOG_FILES": str(self.files), "BLOG_TOKEN": "test-token"}
        subprocess.run([sys.executable, "server.py", "enable-token"], env=env, check=True, capture_output=True,
                       cwd=Path(server.__file__).parent)
        self.assertTrue(server.AUTH.token_enabled())


if __name__ == "__main__":
    unittest.main()
