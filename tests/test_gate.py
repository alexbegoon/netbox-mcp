import asyncio

import httpx
import pytest

from netbox_mcp import server

OPS = [
    ("create_device", {"data": {"name": "a"}}, "POST"),
    ("update_device", {"id": 7, "data": {"name": "b"}}, "PATCH"),
    ("delete_device", {"id": 7}, "DELETE"),
]
IDS = [o[0] for o in OPS]


@pytest.mark.parametrize("name,args,method", OPS, ids=IDS)
@pytest.mark.parametrize("extra", [{}, {"dry_run": False}])
def test_confirm_mode_always_previews(nb, name, args, method, extra):
    nb.log.clear()
    r = nb.res(nb.call(name, {**args, **extra}))
    assert set(nb.log) <= {"GET"}
    assert r["dry_run"] and r["mode"] == "confirm" and len(r["preview_id"]) == 12 and r["expires_in_s"] == 600
    assert r["next_step"] == f'To apply, call the same tool again with the identical arguments plus confirm="{r["preview_id"]}".'
    assert "forced_by_env" not in r and "note" not in r


@pytest.mark.parametrize("name,args,method", OPS, ids=IDS)
def test_confirm_applies_once(nb, name, args, method):
    r = nb.res(nb.call(name, args))
    nb.log.clear()
    nb.call(name, {**args, "confirm": r["preview_id"]})
    assert nb.log == [method]
    nb.refused(name, {**args, "confirm": r["preview_id"]})  # single use


def test_preview_consumed_even_when_netbox_errors(nb):
    args = OPS[0][1]
    r = nb.res(nb.call("create_device", args))
    nb.fail_writes = True
    nb.log.clear()
    with pytest.raises(Exception, match="500"):
        nb.call("create_device", {**args, "confirm": r["preview_id"]})
    assert nb.log == ["POST"]
    nb.refused("create_device", {**args, "confirm": r["preview_id"]})


def test_wrong_or_mismatched_confirm_refused(nb):
    pid = nb.res(nb.call("update_device", {"id": 7, "data": {"name": "b"}}))["preview_id"]
    nb.refused("update_device", {"id": 7, "data": {"name": "b"}, "confirm": "deadbeef0000"})  # wrong id
    nb.refused("update_device", {"id": 7, "data": {"name": "c"}, "confirm": pid})  # other payload
    nb.refused("update_device", {"id": 8, "data": {"name": "b"}, "confirm": pid})  # other id
    nb.refused("create_device", {"data": {"name": "b"}, "confirm": pid})  # other action
    nb.refused("update_prefix", {"id": 7, "data": {"name": "b"}, "confirm": pid})  # other endpoint
    nb.log.clear()
    nb.call("update_device", {"id": 7, "data": {"name": "b"}, "confirm": pid})  # still valid
    assert nb.log == ["PATCH"]


def test_preview_ttl(nb, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(server, "_now", lambda: t[0])
    args = {"data": {"name": "s"}}
    r = nb.res(nb.call("create_site", args))
    t[0] += 599
    nb.log.clear()
    nb.call("create_site", {**args, "confirm": r["preview_id"]})
    assert nb.log == ["POST"]
    r = nb.res(nb.call("create_site", args))
    t[0] += 601
    nb.refused("create_site", {**args, "confirm": r["preview_id"]})


@pytest.mark.parametrize("name,args,method", OPS, ids=IDS)
def test_dry_run_true_never_applies_and_keeps_preview(nb, name, args, method):
    r = nb.res(nb.call(name, args))
    nb.log.clear()
    r2 = nb.res(nb.call(name, {**args, "dry_run": True, "confirm": r["preview_id"]}))
    assert set(nb.log) <= {"GET"} and r2["dry_run"]
    nb.res(nb.call(name, {**args, "dry_run": True}))
    assert set(nb.log) <= {"GET"}
    nb.log.clear()
    nb.call(name, {**args, "confirm": r["preview_id"]})
    assert nb.log == [method]


@pytest.mark.parametrize("value", [None, "", "1", "true", "yes", "banana"])
def test_non_off_values_select_confirm_mode(nb, monkeypatch, value):
    nb.set_mode(value, monkeypatch)
    nb.log.clear()
    for name, args, _ in OPS:
        r = nb.res(nb.call(name, args))
        assert r["mode"] == "confirm" and "next_step" in r
    assert set(nb.log) <= {"GET"}


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF ", "False"])
def test_direct_mode(nb, monkeypatch, value):
    nb.set_mode(value, monkeypatch)
    for name, args, method in OPS:
        nb.log.clear()
        r = nb.res(nb.call(name, args))
        assert nb.log == [method] and "preview_id" not in r and "mode" not in r
        nb.log.clear()
        nb.call(name, {**args, "confirm": "junk"})
        assert nb.log == [method]
        nb.log.clear()
        nb.call(name, {**args, "dry_run": False})
        assert nb.log == [method]
        nb.log.clear()
        r = nb.res(nb.call(name, {**args, "dry_run": True}))
        assert set(nb.log) <= {"GET"} and r["mode"] == "direct" and "preview_id" in r and "next_step" not in r


def test_update_diff_normalisation(nb):
    r = nb.res(nb.call("update_device", {"id": 7, "data": {"site": 1, "status": "offline", "name": "router-1"}}))
    assert r["changes"] == {"status": {"from": "active", "to": "offline"}}  # choice -> value
    assert set(r["unchanged"]) == {"site", "name"}  # nested {"id":1} -> 1
    assert "touches_primary_ip" not in r


def test_update_device_primary_ip_flag(nb):
    assert nb.res(nb.call("update_device", {"id": 7, "data": {"primary_ip4": 5}}))["touches_primary_ip"]


def test_update_missing_object_readable_error(nb, monkeypatch):
    monkeypatch.setattr(server, "_client", httpx.Client(base_url="http://t/api", transport=httpx.MockTransport(lambda req: httpx.Response(404, text="nope"))))
    with pytest.raises(Exception, match=r"device 99 not found \(HTTP 404\)"):
        nb.call("update_device", {"id": 99, "data": {"name": "x"}})


def test_netbox_error_carries_status(nb):
    nb.fail_writes = True
    with pytest.raises(server.NetBoxError) as ei:
        server._request("POST", "dcim/devices", body={})
    assert ei.value.status_code == 500 and isinstance(ei.value, RuntimeError)
    assert "-> HTTP 500: boom" in str(ei.value)


@pytest.mark.parametrize("stem,expected", [
    ("prefix", {"child_prefixes", "ip_addresses"}),
    ("vlan", {"prefixes", "interfaces"}),
    ("site", {"devices", "racks", "prefixes", "vlans"}),
    ("interface", {"ip_addresses", "cable"}),
    ("ip_address", {"devices_using_as_primary_ip"}),
])
def test_delete_impact_keys(nb, stem, expected):
    r = nb.res(nb.call(f"delete_{stem}", {"id": 7}))
    assert set(r["impact"]) == expected and r["warning"] and set(nb.log) == {"GET"}
    assert all("limit=1" in g for g in nb.gets[1:] if not (stem == "vlan" and "dcim/interfaces" in g)), nb.gets


def test_delete_impact_unknown_fallback(nb):
    nb.break_ip_parent = True
    r = nb.res(nb.call("delete_prefix", {"id": 7}))
    assert r["impact"]["ip_addresses"] == "unknown" and r["impact"]["child_prefixes"] == 3


def test_annotations():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["delete_site"].annotations.destructiveHint and not tools["create_site"].annotations.destructiveHint
