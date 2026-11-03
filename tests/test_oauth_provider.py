import asyncio
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import AnyUrl
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from travel_ai.oauth_provider import TravelOAuthProvider, digest


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup(tmp_path):
    provider = TravelOAuthProvider(tmp_path / "gateway.sqlite3", "https://travel.example")
    assert not (tmp_path / "gateway.sqlite3").exists()
    provider.initialize()
    client = OAuthClientInformationFull(client_id="test", client_name="Synthetic", redirect_uris=["https://client.example/callback"], token_endpoint_auth_method="none", scope="travel:mcp")
    run(provider.register_client(client))
    return provider, client


def params(**changes):
    values = dict(state="synthetic-state", scopes=["travel:mcp"], code_challenge="challenge", redirect_uri="https://client.example/callback", redirect_uri_provided_explicitly=True, resource="https://travel.example/ai/mcp/")
    values.update(changes)
    return AuthorizationParams(**values)


def pending(p, client):
    return parse_qs(urlsplit(run(p.authorize(client, params()))).query)["request_id"][0]


def code(p, client):
    request_id = pending(p, client)
    p.bind_browser(request_id, "nonce")
    assert p.check_browser(request_id, "nonce")
    assert not p.check_browser(request_id, "wrong")
    assert not p.bind_browser(request_id, "other")
    redirect = p.complete_consent(request_id, ("household", "user"))
    with pytest.raises(AuthorizeError):
        p.complete_consent(request_id, ("household", "user"))
    return parse_qs(urlsplit(redirect).query)["code"][0]


def test_code_restart_single_use_and_no_plaintext(setup):
    p, c = setup
    raw = code(p, c)
    p = TravelOAuthProvider(p.path, p.origin)
    loaded = run(p.load_authorization_code(c, raw))
    assert loaded.subject == digest("household:user")
    token = run(p.exchange_authorization_code(c, loaded))
    with pytest.raises(TokenError):
        run(p.exchange_authorization_code(c, loaded))
    assert run(p.load_authorization_code(c, raw)) is None
    assert run(p.load_access_token(token.access_token)).resource == p.resource
    with sqlite3.connect(p.path) as db:
        dump = "\n".join(db.iterdump())
    assert raw not in dump and token.access_token not in dump and token.refresh_token not in dump
    assert run(p.get_client(c.client_id)).client_name == "Synthetic"


def test_refresh_fixed_lifetime_rotation_revocation(setup):
    p, c = setup
    token = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    refresh = run(p.load_refresh_token(c, token.refresh_token))
    newer = run(p.exchange_refresh_token(c, refresh, ["travel:mcp"]))
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(c, newer.refresh_token)).expires_at == refresh.expires_at
    access = run(p.load_access_token(newer.access_token))
    run(p.revoke_token(access))
    assert run(p.load_access_token(newer.access_token)) is None
    assert run(p.load_refresh_token(c, newer.refresh_token)) is None


def test_cross_client_scope_resource(setup):
    p, c = setup
    other = c.model_copy(update={"client_id": "other"})
    raw = code(p, c)
    assert run(p.load_authorization_code(other, raw)) is None
    loaded = run(p.load_authorization_code(c, raw))
    with pytest.raises(TokenError):
        run(p.exchange_authorization_code(other, loaded))
    token = run(p.exchange_authorization_code(c, loaded))
    assert run(p.load_refresh_token(other, token.refresh_token)) is None
    refresh = run(p.load_refresh_token(c, token.refresh_token))
    with pytest.raises(TokenError):
        run(p.exchange_refresh_token(other, refresh, ["travel:mcp"]))
    with pytest.raises(TokenError):
        run(p.exchange_refresh_token(c, refresh, ["admin"]))
    for overrides in ({"resource": None}, {"resource": "https://other.example/ai/mcp/"}, {"scopes": ["admin"]}, {"redirect_uri": "https://other.example/callback"}):
        with pytest.raises(AuthorizeError):
            run(p.authorize(c, params(**overrides)))
    assert run(p.authorize(c, params(resource=p.resource.rstrip("/"))))


def test_expiry_and_denial(setup, monkeypatch):
    p, c = setup
    request = pending(p, c)
    denied = parse_qs(urlsplit(p.deny_consent(request)).query)
    assert denied == {"error": ["access_denied"], "state": ["synthetic-state"]}
    assert p.get_pending(request) is None
    request = pending(p, c)
    import travel_ai.oauth_provider as module
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now+601)
    assert p.get_pending(request) is None
    assert not p.bind_browser(request, "nonce")
    with pytest.raises(AuthorizeError):
        p.complete_consent(request, "subject")
    monkeypatch.setattr(module.time, "time", lambda: now)
    raw = code(p, c)
    loaded = run(p.load_authorization_code(c, raw))
    monkeypatch.setattr(module.time, "time", lambda: now+121)
    assert run(p.load_authorization_code(c, raw)) is None
    with pytest.raises(TokenError):
        run(p.exchange_authorization_code(c, loaded))


@pytest.mark.parametrize("uri", ["http://remote.example/callback", "https://user:password@client.example/cb", "https://client.example/cb#fragment"])
def test_registration_rejects_redirects(setup, uri):
    p, c = setup
    client = c.model_copy(update={"client_id": "bad", "redirect_uris": [AnyUrl(uri)]})
    with pytest.raises(RegistrationError):
        run(p.register_client(client))


@pytest.mark.parametrize("uri", ["http://localhost:1234/cb", "http://127.0.0.1:1234/cb", "http://[::1]:1234/cb"])
def test_registration_allows_loopback(setup, uri):
    p, c = setup
    run(p.register_client(c.model_copy(update={"client_id": "loopback", "redirect_uris": [AnyUrl(uri)]})))


def test_parallel_code_exchange_only_one_wins(setup):
    from concurrent.futures import ThreadPoolExecutor
    p, c = setup
    loaded = run(p.load_authorization_code(c, code(p, c)))

    def exchange():
        try:
            run(p.exchange_authorization_code(c, loaded))
            return True
        except TokenError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: exchange(), range(2))) == [False, True]


def test_access_and_grant_expiry(setup, monkeypatch):
    import travel_ai.oauth_provider as module
    p, c = setup
    token = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    refresh = run(p.load_refresh_token(c, token.refresh_token))
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now+3601)
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(c, token.refresh_token)) is not None
    monkeypatch.setattr(module.time, "time", lambda: now+30*86400+1)
    assert run(p.load_refresh_token(c, token.refresh_token)) is None
    with pytest.raises(TokenError):
        run(p.exchange_refresh_token(c, refresh, ["travel:mcp"]))


def test_refresh_revokes_access_pair(setup):
    p, c = setup
    token = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    refresh = run(p.load_refresh_token(c, token.refresh_token))
    run(p.revoke_token(refresh))
    assert run(p.load_access_token(token.access_token)) is None
    assert run(p.load_refresh_token(c, token.refresh_token)) is None


def test_registration_rejects_client_secret(setup):
    p, c = setup
    with pytest.raises(RegistrationError):
        run(p.register_client(c.model_copy(update={"client_id": "secret", "client_secret": "synthetic-secret", "token_endpoint_auth_method": "client_secret_post"})))


@pytest.mark.parametrize("replay_method", ["load", "exchange"])
def test_refresh_replay_revokes_entire_grant(setup, replay_method):
    p, c = setup
    token = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    refresh = run(p.load_refresh_token(c, token.refresh_token))
    newer = run(p.exchange_refresh_token(c, refresh, ["travel:mcp"]))
    other = c.model_copy(update={"client_id": "other"})
    assert run(p.load_refresh_token(other, token.refresh_token)) is None
    with pytest.raises(TokenError):
        run(p.exchange_refresh_token(other, refresh, ["travel:mcp"]))
    assert run(p.load_access_token(newer.access_token)) is not None
    if replay_method == "load":
        assert run(p.load_refresh_token(c, token.refresh_token)) is None
    else:
        with pytest.raises(TokenError):
            run(p.exchange_refresh_token(c, refresh, ["travel:mcp"]))
    assert run(p.load_access_token(newer.access_token)) is None
    assert run(p.load_refresh_token(c, newer.refresh_token)) is None


def test_refresh_history_survives_multiple_rotations(setup):
    p, c = setup
    first = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    second = run(p.exchange_refresh_token(c, run(p.load_refresh_token(c, first.refresh_token)), ["travel:mcp"]))
    third = run(p.exchange_refresh_token(c, run(p.load_refresh_token(c, second.refresh_token)), ["travel:mcp"]))
    assert run(p.load_refresh_token(c, first.refresh_token)) is None
    assert run(p.load_access_token(third.access_token)) is None
    assert run(p.load_refresh_token(c, third.refresh_token)) is None


def test_existing_grants_migrate_without_inventing_creation_time(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE oauth_grants (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, subject TEXT NOT NULL, expires INTEGER NOT NULL)')
        db.execute('INSERT INTO oauth_grants VALUES (?,?,?,?)', ('legacy', 'test', 'owner', 1))
    provider = TravelOAuthProvider(path, 'https://travel.example')
    provider.initialize()
    provider.initialize()
    client = OAuthClientInformationFull(client_id='test', client_name='Legacy agent',
        redirect_uris=['https://client.example/callback'], token_endpoint_auth_method='none', scope='travel:mcp')
    run(provider.register_client(client))
    assert provider.list_connections('owner') == [{'id': 'legacy', 'client_name': 'Legacy agent',
        'created_at': None, 'last_used_at': None, 'expires_at': 1, 'status': 'expired'}]
    assert not provider.revoke_connection('other', 'legacy')
    assert provider.revoke_connection('owner', 'legacy')


def test_last_use_is_recorded_only_explicitly_and_survives_refresh(setup):
    p, c = setup
    first = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code(p, c)))))
    owner = run(p.load_access_token(first.access_token)).subject
    assert p.list_connections(owner)[0]['last_used_at'] is None
    p.record_use('invalid')
    assert p.list_connections(owner)[0]['last_used_at'] is None
    p.record_use(first.access_token)
    used = p.list_connections(owner)[0]['last_used_at']
    assert used is not None
    run(p.exchange_refresh_token(c, run(p.load_refresh_token(c, first.refresh_token)), ['travel:mcp']))
    assert p.list_connections(owner)[0]['last_used_at'] == used
