import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_complete_dashboard_is_bundled_without_household_layers():
    text = (ROOT / "dashboard.html").read_text("utf-8")
    assert "Breath" in text and "Reverie" in text and "Constellations" in text
    assert "Drive Ledger" in text and "Thought Pool" in text
    for removed in ["opening.html", "Atmosphere", "Gravity", "Catroom", "嘉嘉", "Nox"]:
        assert removed not in text


def test_removed_household_modules_stay_absent():
    for name in [
        "catroom_store.py",
        "room_store.py",
        "rhythm_store.py",
        "opening.html",
        "reclassify_domains.py",
    ]:
        assert not (ROOT / name).exists()


def test_public_mcp_surface_still_has_continuity_loop(tmp_path):
    env = dict(os.environ, OMBRE_BUCKETS_DIR=str(tmp_path))
    code = (
        "import inspect, server\n"
        "print('TOOLS', ','.join(sorted(server.mcp._tool_manager._tools)))\n"
        "print('HOLDKIND', ','.join(server.HoldKind.__args__))\n"
        "print('HOLDPARAMS', ','.join(inspect.signature(server.hold).parameters))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            text=True, capture_output=True, check=True)
    lines = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in result.stdout.strip().splitlines() if " " in line}
    tools = set(lines["TOOLS"].split(","))
    kinds = set(lines["HOLDKIND"].split(","))
    params = set(lines["HOLDPARAMS"].split(","))
    assert {"hold", "breath", "trace", "wander", "drive", "undercurrent"} <= tools
    assert "grow" not in tools
    assert not {"room", "rhythm"} & tools
    assert "letter" in kinds
    assert {"name", "pinned", "protected"} <= params


def test_live_hold_does_not_call_dehydrator():
    text = (ROOT / "server.py").read_text("utf-8")
    hold_src = text.split("async def hold(", 1)[1].split("# grow — Ombre", 1)[0]
    assert "dehydrator.analyze" not in hold_src
    assert "dehydrator.merge" not in hold_src
    assert "dehydrator.digest" not in hold_src
    assert not re.search(r"@mcp\.tool\([^)]*\)\s*\nasync def grow\(", text)
