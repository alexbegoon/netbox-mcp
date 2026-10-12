import asyncio
import json

import httpx
import pytest

from netbox_mcp import server


class Harness:
    """Mock NetBox plus a call helper. `log` holds every request method, `gets` every GET URL."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.gets: list[str] = []
        self.break_ip_parent = False  # GET ipam/ip-addresses with `parent` -> 400
        self.fail_writes = False  # POST/PATCH -> 500
        self.fail_vlan_interfaces = False  # GET dcim/interfaces with vlan_id -> 500

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.log.append(req.method)
        url, path = str(req.url), req.url.path
        if req.method == "GET":
            self.gets.append(url)
            if self.break_ip_parent and path.endswith("ipam/ip-addresses/") and "parent" in url:
                return httpx.Response(400, json={"x": "bad"})
            if path.endswith("dcim/interfaces/") and "vlan_id" in url:
                if self.fail_vlan_interfaces:
                    return httpx.Response(500, text="boom")
                return httpx.Response(200, json={"count": 6, "results": [{"id": i} for i in (1, 1, 2, 2, 2, 3)]})
            if path.endswith("/7/") or path.endswith("/9/"):
                return httpx.Response(200, json={
                    "id": int(path.split("/")[-2]), "display": "o", "name": "router-1", "slug": "l",
                    "prefix": "10.0.0.0/24", "vrf": None, "site": {"id": 1}, "parent": None, "vid": 10,
                    "status": {"value": "active", "label": "A"},
                })
            return httpx.Response(200, json={"count": 3, "results": [{"id": 1}]})
        if req.method == "DELETE":
            return httpx.Response(204)
        if self.fail_writes:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"id": 7})

    def call(self, name: str, args: dict):
        return asyncio.run(server.mcp.call_tool(name, args))

    @staticmethod
    def res(r) -> dict:
        c = r[1] if isinstance(r, tuple) else r
        return c if isinstance(c, dict) else json.loads(c[0].text)

    def refused(self, name: str, args: dict, why=("preview", "confirm does not match")) -> None:
        self.log.clear()
        with pytest.raises(Exception) as ei:
            self.call(name, args)
        assert any(w in str(ei.value) for w in why), ei.value
        assert not [m for m in self.log if m != "GET"], self.log

    def set_mode(self, value: str | None, monkeypatch) -> None:
        server._PREVIEWS.clear()
        if value is None:
            monkeypatch.delenv("NETBOX_DRY_RUN", raising=False)
        else:
            monkeypatch.setenv("NETBOX_DRY_RUN", value)


@pytest.fixture
def nb(monkeypatch) -> Harness:
    monkeypatch.delenv("NETBOX_DRY_RUN", raising=False)
    h = Harness()
    monkeypatch.setattr(server, "_client", httpx.Client(base_url="http://t/api", transport=httpx.MockTransport(h.handler)))
    server._PREVIEWS.clear()
    yield h
    server._PREVIEWS.clear()
