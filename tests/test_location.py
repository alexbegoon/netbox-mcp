import asyncio

from netbox_mcp import server


def test_location_tools_registered():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    for n in ("list_locations", "get_location", "create_location", "update_location", "delete_location"):
        assert n in tools, n


def test_location_create_preview_then_confirm(nb):
    data = {"name": "L", "slug": "l", "site": 1}
    r = nb.res(nb.call("create_location", {"data": data}))
    assert nb.log == [] and r["dry_run"] and r["body"]["slug"] == "l" and r["path"] == "/dcim/locations/"
    nb.log.clear()
    nb.call("create_location", {"data": data, "confirm": r["preview_id"]})
    assert nb.log == ["POST"]


def test_location_delete_impact(nb):
    r = nb.res(nb.call("delete_location", {"id": 7}))
    assert set(nb.log) == {"GET"} and r["impact"] == {"devices": 3, "racks": 3, "child_locations": 3}
    assert any("location_id=7" in g and "dcim/devices" in g for g in nb.gets)
    assert any("parent_id=7" in g for g in nb.gets)


def test_vlan_impact_distinct_interfaces(nb):
    r = nb.res(nb.call("delete_vlan", {"id": 7}))
    assert r["impact"]["interfaces"] == 3 and r["impact"]["prefixes"] == 3
    g = [x for x in nb.gets if "dcim/interfaces" in x][0]
    assert "limit=1000" in g and "brief=1" in g, g


def test_vlan_interfaces_unknown_on_error(nb):
    nb.fail_vlan_interfaces = True
    r = nb.res(nb.call("delete_vlan", {"id": 7}))
    assert r["impact"]["interfaces"] == "unknown" and r["impact"]["prefixes"] == 3
