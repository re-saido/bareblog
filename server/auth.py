"""Who's the admin: the API token, passkeys, and browser sessions.

State lives in one JSON file (by default `_auth.json` in the file store). Its
leading underscore keeps it outside the file API, so nothing can read or write it
over HTTP. Once there's a passkey the token can be turned off, which also logs
out every browser.
"""

import datetime
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

CHALLENGE_TTL = 300  # seconds to finish a passkey prompt
# Wrong tokens allowed per window, from anyone, before every token check fails
# until the window has passed. Global rather than per client, since behind a
# proxy every client looks the same. Sessions and passkeys aren't affected.
MAX_WRONG_TOKENS = 10
WRONG_TOKEN_WINDOW = 600  # seconds


class Auth:
    def __init__(self, path, token, origin=None):
        self.path, self.token = Path(path), token
        # Passkeys are bound to a site, so they need its exact origin, e.g.
        # https://blog.example.com (browsers only allow them over HTTPS).
        self.origin = origin.rstrip("/") if origin else None
        self.rp_id = urlsplit(self.origin).hostname if self.origin else None
        self._lock = threading.RLock()
        self._challenges = {}  # challenge -> (purpose, expiry)
        self._wrong_tokens = deque()  # when each recent wrong token was tried

    # Stored state

    def _state(self):
        """The saved state, creating the file on first use. A corrupt file is an
        error rather than a reset, since a reset would turn the token back on."""
        with self._lock:
            try:
                state = json.loads(self.path.read_text())
            except FileNotFoundError:
                state = {}
            if not isinstance(state, dict):
                raise ValueError(f"{self.path} isn't a JSON object")
            if "session_secret" not in state:
                state |= {"session_secret": secrets.token_hex(32), "user_id": bytes_to_base64url(secrets.token_bytes(16))}
                self._save(state)
            return state

    def _save(self, state):
        """Write via a private temp file + rename; the file holds the session secret."""
        tmp = self.path.with_name(f"_{self.path.name}.{secrets.token_hex(4)}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, self.path)

    # The token, and sessions

    def token_enabled(self):
        return not self._state().get("token_disabled", False)

    def check_token(self, given):
        """True if `given` is the token. Wrong ones count towards the limit;
        while it's hit this is False even for the right token."""
        if not self.token_enabled() or self.token_wait():
            return False
        if hmac.compare_digest(given.encode(), self.token.encode()):
            return True
        with self._lock:
            self._wrong_tokens.append(time.monotonic())
        return False

    def token_wait(self):
        """Seconds until the token can be tried again; 0 if it can be now."""
        now = time.monotonic()
        with self._lock:
            while self._wrong_tokens and self._wrong_tokens[0] <= now - WRONG_TOKEN_WINDOW:
                self._wrong_tokens.popleft()
            if len(self._wrong_tokens) < MAX_WRONG_TOKENS:
                return 0
            return int(self._wrong_tokens[0] + WRONG_TOKEN_WINDOW - now) + 1

    def session(self, state=None):
        """The session cookie value. It changes when the token does, or when the
        token is turned on or off, which logs every browser out."""
        state = state or self._state()
        token = b"" if state.get("token_disabled") else self.token.encode()
        return hmac.new(state["session_secret"].encode(), b"session:" + token, "sha256").hexdigest()

    def check_session(self, value):
        return hmac.compare_digest(value.encode(), self.session().encode())

    def set_token_enabled(self, enabled):
        """Returns an error message, or None once done."""
        with self._lock:
            state = self._state()
            if not enabled and not state.get("passkeys"):
                return "add a passkey before turning off the token"
            state["token_disabled"] = not enabled
            state["session_secret"] = secrets.token_hex(32)
            self._save(state)
        return None

    # Passkeys

    def passkeys(self):
        return self._state().get("passkeys", [])

    def remove_passkey(self, passkey_id):
        """Returns an error message, or None once removed."""
        with self._lock:
            state = self._state()
            keep = [p for p in state.get("passkeys", []) if p["id"] != passkey_id]
            if len(keep) == len(state.get("passkeys", [])):
                return "no such passkey"
            if not keep and state.get("token_disabled"):
                return "that's your last passkey and the token is off; turn the token on first"
            state["passkeys"] = keep
            self._save(state)
        return None

    def _challenge(self, purpose, options):
        now = time.monotonic()
        with self._lock:
            self._challenges = {c: v for c, v in self._challenges.items() if v[1] > now}
            self._challenges[options.challenge] = (purpose, now + CHALLENGE_TTL)
        return webauthn.options_to_json(options)

    def _take_challenge(self, purpose, credential):
        """The challenge the browser signed, if we issued it for `purpose` and it
        hasn't expired or been used. Each one works once."""
        client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = base64url_to_bytes(client_data["challenge"])
        with self._lock:
            issued = self._challenges.pop(challenge, None)
        if not issued or issued[0] != purpose or issued[1] < time.monotonic():
            raise ValueError("unknown or expired challenge; try again")
        return challenge

    def registration_options(self, rp_name):
        state = self._state()
        return self._challenge("register", webauthn.generate_registration_options(
            rp_id=self.rp_id,
            rp_name=rp_name,
            user_name="admin",
            user_id=base64url_to_bytes(state["user_id"]),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                                 for p in state.get("passkeys", [])],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        ))

    def register(self, credential, name):
        """Add a passkey from the browser's response. Returns an error message, or None."""
        try:
            verified = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=self._take_challenge("register", credential),
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as e:
            return f"passkey not added: {e}"
        with self._lock:
            state = self._state()
            state.setdefault("passkeys", []).append({
                "id": bytes_to_base64url(verified.credential_id),
                "public_key": bytes_to_base64url(verified.credential_public_key),
                "sign_count": verified.sign_count,
                "name": name.strip()[:60] or "passkey",
                "added": str(datetime.date.today()),
            })
            self._save(state)
        return None

    def login_options(self):
        return self._challenge("login", webauthn.generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"])) for p in self.passkeys()],
            user_verification=UserVerificationRequirement.REQUIRED,
        ))

    def login(self, credential):
        """A session cookie value if `credential` is a good passkey sign-in, else None."""
        with self._lock:
            try:
                challenge = self._take_challenge("login", credential)
                state = self._state()
                raw_id = bytes_to_base64url(base64url_to_bytes(credential["rawId"]))
                passkey = next((p for p in state.get("passkeys", []) if p["id"] == raw_id), None)
                if passkey is None:
                    return None
                verified = webauthn.verify_authentication_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=self.rp_id,
                    expected_origin=self.origin,
                    credential_public_key=base64url_to_bytes(passkey["public_key"]),
                    credential_current_sign_count=passkey["sign_count"],
                    require_user_verification=True,
                )
            except (WebAuthnException, ValueError, KeyError, TypeError):
                return None
            passkey["sign_count"] = verified.new_sign_count
            passkey["last_used"] = str(datetime.date.today())
            self._save(state)
            return self.session(state)
