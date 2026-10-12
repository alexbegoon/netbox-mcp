import pytest

from netbox_mcp import server


@pytest.fixture(autouse=True)
def _no_client(monkeypatch):
    monkeypatch.setattr(server, "_client", None)


def test_import_has_no_side_effects():
    assert server._client is None  # no client built, no env validation at import


def test_missing_token_exits(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    monkeypatch.setenv("NETBOX_URL", "https://nb.example")
    with pytest.raises(SystemExit) as ei:
        server.main()
    assert ei.value.code == 1 and "NETBOX_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("url", [None, "", "  ", "nb.example", "ftp://nb.example"])
def test_missing_or_bad_url_exits(monkeypatch, capsys, url):
    monkeypatch.setenv("NETBOX_TOKEN", "dummy")
    if url is None:
        monkeypatch.delenv("NETBOX_URL", raising=False)
    else:
        monkeypatch.setenv("NETBOX_URL", url)
    with pytest.raises(SystemExit) as ei:
        server.main()
    assert ei.value.code == 1 and "NETBOX_URL" in capsys.readouterr().err


def test_valid_env_runs_server(monkeypatch):
    ran = []
    monkeypatch.setenv("NETBOX_TOKEN", "dummy")
    monkeypatch.setenv("NETBOX_URL", "https://nb.example/")
    monkeypatch.setattr(server.mcp, "run", lambda transport: ran.append(transport))
    server.main()
    assert ran == ["stdio"]


def test_get_client_lazy_and_cached(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", "nbt_abc")
    monkeypatch.setenv("NETBOX_URL", "https://nb.example/")
    c = server._get_client()
    assert str(c.base_url).rstrip("/") == "https://nb.example/api"
    assert c.headers["Authorization"] == "Bearer nbt_abc"
    assert server._get_client() is c
    c.close()
