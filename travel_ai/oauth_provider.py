"""Persistent public-client OAuth grants for the Travel MCP endpoint."""
import hashlib
import ipaddress
import json
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "travel:mcp"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def valid_redirect(value: str) -> bool:
    try:
        uri = urlsplit(value)
        if not uri.hostname or uri.fragment or uri.username or uri.password:
            return False
        _ = uri.port
        if uri.scheme == "https":
            return True
        if uri.scheme != "http":
            return False
        return uri.hostname == "localhost" or ipaddress.ip_address(uri.hostname).is_loopback
    except ValueError:
        return False


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class TravelOAuthProvider:
    def __init__(self, path, origin):
        self.path = str(path)
        self.origin = origin.rstrip("/")
        self.resource = self.origin + "/ai/mcp/"

    def _db(self):
        db = sqlite3.connect(self.path, timeout=15, factory=_Connection)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        return db

    def initialize(self):
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS oauth_clients (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires REAL NOT NULL, browser TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_grants (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, subject TEXT NOT NULL, expires INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL, expires INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens(grant_id)")
            if 'created_at' not in {row['name'] for row in db.execute('PRAGMA table_info(oauth_grants)')}:
                db.execute('ALTER TABLE oauth_grants ADD COLUMN created_at INTEGER')
            if 'last_used_at' not in {row['name'] for row in db.execute('PRAGMA table_info(oauth_grants)')}:
                db.execute('ALTER TABLE oauth_grants ADD COLUMN last_used_at INTEGER')

    async def get_client(self, client_id):
        with self._db() as db:
            row = db.execute("SELECT data FROM oauth_clients WHERE id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info):
        if client_info.token_endpoint_auth_method != "none" or client_info.client_secret:
            raise RegistrationError("invalid_client_metadata", "Only public clients with authentication method none are supported")
        if not client_info.redirect_uris or not all(valid_redirect(str(uri)) for uri in client_info.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "HTTPS or loopback HTTP redirects required")
        if client_info.scope and client_info.scope.split() != [SCOPE]:
            raise RegistrationError("invalid_client_metadata", "Unsupported scope")
        with self._db() as db:
            db.execute("INSERT INTO oauth_clients VALUES (?,?)", (client_info.client_id, client_info.model_dump_json()))

    async def authorize(self, client, params):
        if params.scopes not in (None, [], [SCOPE]):
            raise AuthorizeError("invalid_scope", "Unsupported scope")
        if not params.resource or params.resource.rstrip("/") != self.resource.rstrip("/"):
            raise AuthorizeError("invalid_request", "A matching Travel MCP resource is required")
        if not valid_redirect(str(params.redirect_uri)) or str(params.redirect_uri) not in [str(u) for u in client.redirect_uris or []]:
            raise AuthorizeError("invalid_request", "Redirect URI is not registered")
        params = params.model_copy(update={"scopes": [SCOPE], "resource": self.resource})
        request_id = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("DELETE FROM oauth_pending WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO oauth_pending VALUES (?,?,?,?,NULL)", (request_id, client.client_id, params.model_dump_json(), time.time()+600))
        return self.origin + "/ai/oauth/consent?request_id=" + request_id

    def get_pending(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT p.*, c.data AS client FROM oauth_pending p JOIN oauth_clients c ON c.id=p.client_id WHERE p.id=? AND p.expires>?", (request_id, time.time())).fetchone()
        if not row:
            return None
        return {"id": row["id"], "client_id": row["client_id"], "client_name": json.loads(row["client"]).get("client_name"), "params": json.loads(row["params"]), "expires_at": row["expires"]}

    def bind_browser(self, request_id, nonce):
        with self._db() as db:
            return bool(db.execute("UPDATE oauth_pending SET browser=? WHERE id=? AND expires>? AND (browser IS NULL OR browser=?)", (digest(nonce), request_id, time.time(), digest(nonce))).rowcount)

    def check_browser(self, request_id, nonce):
        with self._db() as db:
            row = db.execute("SELECT browser FROM oauth_pending WHERE id=? AND expires>?", (request_id, time.time())).fetchone()
        return bool(row and row[0] and secrets.compare_digest(row[0], digest(nonce)))

    def _consume_pending(self, db, request_id):
        row = db.execute("DELETE FROM oauth_pending WHERE id=? AND expires>? RETURNING *", (request_id, time.time())).fetchone()
        if not row:
            raise AuthorizeError("invalid_request", "Authorization request expired or already completed")
        return row, AuthorizationParams.model_validate_json(row["params"])

    def complete_consent(self, request_id, owner):
        if isinstance(owner, dict):
            subject = digest(f"{owner['household_id']}:{owner['user_id']}")
        elif isinstance(owner, tuple):
            subject = digest(f"{owner[0]}:{owner[1]}")
        else:
            subject = owner
        if not isinstance(subject, str) or not subject:
            raise ValueError("Resource owner required")
        code = secrets.token_urlsafe(32)
        with self._db() as db:
            row, params = self._consume_pending(db, request_id)
            data = AuthorizationCode(code="", scopes=[SCOPE], expires_at=time.time()+120, client_id=row["client_id"], code_challenge=params.code_challenge, redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource, subject=subject)
            db.execute("INSERT INTO oauth_codes VALUES (?,?)", (digest(code), data.model_dump_json()))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny_consent(self, request_id):
        with self._db() as db:
            _, params = self._consume_pending(db, request_id)
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        with self._db() as db:
            row = db.execute("SELECT data FROM oauth_codes WHERE hash=?", (digest(authorization_code),)).fetchone()
        if not row:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        if code.client_id != client.client_id or code.expires_at <= time.time():
            return None
        return code.model_copy(update={"code": authorization_code})

    def _issue(self, db, grant_id, expires):
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_expires = min(now+3600, expires)
        db.execute("INSERT INTO oauth_tokens VALUES (?,?,?,?)", (digest(access), "access", grant_id, access_expires))
        db.execute("INSERT INTO oauth_tokens VALUES (?,?,?,?)", (digest(refresh), "refresh", grant_id, expires))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=access_expires-now, refresh_token=refresh, scope=SCOPE)

    async def exchange_authorization_code(self, client, authorization_code):
        with self._db() as db:
            row = db.execute("SELECT data FROM oauth_codes WHERE hash=?", (digest(authorization_code.code),)).fetchone()
            code = AuthorizationCode.model_validate_json(row[0]) if row else None
            if not code or code.client_id != client.client_id or code.expires_at <= time.time():
                raise TokenError("invalid_grant", "Authorization code expired or consumed")
            db.execute("DELETE FROM oauth_codes WHERE hash=?", (digest(authorization_code.code),))
            grant_id, expires = secrets.token_urlsafe(24), int(time.time())+30*86400
            db.execute("INSERT INTO oauth_grants (id,client_id,subject,expires,created_at) VALUES (?,?,?,?,?)", (grant_id, code.client_id, code.subject, expires, int(time.time())))
            return self._issue(db, grant_id, expires)

    def _load_token(self, db, token, kind):
        return db.execute("SELECT t.*, g.client_id, g.subject, g.expires AS grant_expires FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind=? AND t.expires>? AND g.expires>?", (digest(token), kind, time.time(), time.time())).fetchone()

    def _revoke_refresh_reuse(self, db, client_id, token):
        row = db.execute("SELECT t.grant_id FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind='refresh_used' AND g.client_id=?", (digest(token), client_id)).fetchone()
        if row:
            db.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (row[0],))
            db.execute("DELETE FROM oauth_grants WHERE id=?", (row[0],))
        return bool(row)

    async def load_refresh_token(self, client, refresh_token):
        with self._db() as db:
            self._revoke_refresh_reuse(db, client.client_id, refresh_token)
            row = self._load_token(db, refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=[SCOPE], expires_at=row["expires"], resource=self.resource, subject=row["subject"])

    async def load_access_token(self, token):
        with self._db() as db:
            row = self._load_token(db, token, "access")
        if not row:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=[SCOPE], expires_at=row["expires"], resource=self.resource, subject=row["subject"])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if scopes != [SCOPE]:
            raise TokenError("invalid_scope", "Unsupported scope")
        result = None
        with self._db() as db:
            reused = self._revoke_refresh_reuse(db, client.client_id, refresh_token.token)
            row = None if reused else self._load_token(db, refresh_token.token, "refresh")
            if row and row["client_id"] == client.client_id:
                db.execute("DELETE FROM oauth_tokens WHERE grant_id=? AND kind='access'", (row["grant_id"],))
                db.execute("UPDATE oauth_tokens SET kind='refresh_used' WHERE hash=?", (digest(refresh_token.token),))
                result = self._issue(db, row["grant_id"], row["grant_expires"])
        # Raise after commit so replay detection cannot roll back grant revocation.
        if result is None:
            raise TokenError("invalid_grant", "Refresh token expired or consumed")
        return result

    async def revoke_token(self, token):
        with self._db() as db:
            row = db.execute("SELECT t.grant_id FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id WHERE t.hash=? AND g.client_id=?", (digest(token.token), token.client_id)).fetchone()
            if row:
                db.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (row[0],))
                db.execute("DELETE FROM oauth_grants WHERE id=?", (row[0],))

    def record_use(self, token):
        # Only called after MCP resource, scope and current household checks pass.
        with self._db() as db:
            row = self._load_token(db, token, "access")
            if row:
                db.execute("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (int(time.time()), row["grant_id"]))

    def list_connections(self, owner):
        with self._db() as db:
            rows = db.execute("SELECT g.id,g.expires,g.created_at,g.last_used_at,c.data FROM oauth_grants g JOIN oauth_clients c ON c.id=g.client_id WHERE g.subject=? ORDER BY g.expires DESC", (owner,)).fetchall()
        return [{"id": row["id"], "client_name": json.loads(row["data"]).get("client_name") or "未命名应用",
                 "created_at": row["created_at"], "last_used_at": row["last_used_at"], "expires_at": row["expires"],
                 "status": "active" if row["expires"] > time.time() else "expired"} for row in rows]

    def revoke_connection(self, owner, grant_id):
        with self._db() as db:
            row = db.execute("DELETE FROM oauth_grants WHERE id=? AND subject=? RETURNING id", (grant_id, owner)).fetchone()
            if row:
                db.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (grant_id,))
        return bool(row)
