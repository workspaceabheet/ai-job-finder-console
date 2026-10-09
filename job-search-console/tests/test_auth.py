import stat

from app import auth


def test_missing_token_rejected(client):
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_wrong_token_rejected(client, token):
    r = client.get("/api/settings", headers={"X-App-Token": "wrong-token"})
    assert r.status_code == 401


def test_forged_origin_rejected(client, token):
    r = client.get(
        "/api/settings",
        headers={"X-App-Token": token, "Origin": "http://evil.example"},
    )
    assert r.status_code == 403


def test_correct_token_and_origin_allowed(client, token):
    r = client.get(
        "/api/settings",
        headers={"X-App-Token": token, "Origin": "http://127.0.0.1:8000"},
    )
    assert r.status_code == 200


def test_missing_origin_falls_back_to_host(client, token):
    # No Origin header: Host (127.0.0.1:8000) is checked instead and passes.
    r = client.get("/api/settings", headers={"X-App-Token": token})
    assert r.status_code == 200


def test_forged_host_rejected_when_origin_absent(client, token):
    r = client.get(
        "/api/settings", headers={"X-App-Token": token, "Host": "evil.example"}
    )
    assert r.status_code == 403


def test_correct_hostname_wrong_port_rejected(client, token):
    # Same allowed hostname (127.0.0.1), but a different port -- e.g. some
    # other local dev server on the user's machine. ALLOWED_PORT must reject
    # this even though the hostname check alone would pass it.
    r = client.get(
        "/api/settings",
        headers={"X-App-Token": token, "Origin": "http://127.0.0.1:9999"},
    )
    assert r.status_code == 403


def test_correct_hostname_wrong_port_rejected_via_host_fallback(client, token):
    r = client.get(
        "/api/settings",
        headers={"X-App-Token": token, "Host": "127.0.0.1:9999"},
    )
    assert r.status_code == 403


def test_index_embeds_token_and_token_file_is_0600(client, token, data_dir):
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="app-token" content="{token}">' in r.text
    token_file = data_dir / ".session_token"
    assert token_file.read_text() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_no_route_serves_token(client, token):
    # Only GET / may render the token; no /api route returns it.
    for route in client.app.routes:
        path = getattr(route, "path", "")
        assert "token" not in path.lower()
    r = client.get("/api/token", headers={"X-App-Token": token})
    assert token not in r.text


def test_restart_remints_token(data_dir):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import BASE_URL

    with TestClient(create_app(), base_url=BASE_URL):
        first = auth.current_token()
    with TestClient(create_app(), base_url=BASE_URL) as c:
        second = auth.current_token()
        r = c.get("/api/settings", headers={"X-App-Token": first})
    assert first != second
    assert r.status_code == 401
