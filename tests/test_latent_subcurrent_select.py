"""Subcurrent selection: approved pool only, exact drive_tag, no silent river."""

from datetime import datetime, timedelta

import pytest

pytest.importorskip("mcp.server.fastmcp")

from server import (
    _approved_latent_note_payload,
    _latent_low_stock_drives,
    _latent_note_is_stale,
    _latent_select_weight,
    _select_approved_latent_note,
)


def _note(
    note_id: str,
    drive_tag: str,
    line: str,
    status: str = "approved",
    *,
    pinned: bool = False,
    delivered_count: int = 0,
    note_type: str = "inward",
    source_created: str = "",
) -> dict:
    return {
        "id": note_id,
        "status": status,
        "pinned": pinned,
        "note_type": note_type,
        "drive_tag": drive_tag,
        "dream_line": line,
        "source_created": source_created,
        "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "delivered_count": delivered_count,
    }


def test_exact_drive_tag_preferred(monkeypatch):
    data = {
        "notes": [
            _note("a1", "stewardship", "边界清楚了，房间和身体分开。"),
            _note("a2", "curiosity", "今日歌单还空着。让我想想。"),
            _note("a3", "general", "随便一张纸。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    picks = {_select_approved_latent_note(set(), drive_key="stewardship")["line"] for _ in range(12)}
    assert picks == {"边界清楚了，房间和身体分开。"}
    hit = _select_approved_latent_note(set(), drive_key="stewardship")
    assert hit["pool_match"] == "exact"
    assert hit["drive_tag"] == "stewardship"


def test_no_silent_general_or_cross_drive_fallback(monkeypatch):
    data = {
        "notes": [
            _note("c1", "curiosity", "今日歌单还空着。让我想想。"),
            _note("g1", "general", "流浪句。"),
            _note("r1", "reflection", "尺子量自己。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    assert _select_approved_latent_note(set(), drive_key="stewardship") is None
    assert _select_approved_latent_note(set(), drive_key="libido") is None


def test_relaxed_exclude_reuses_same_tag_only(monkeypatch):
    data = {
        "notes": [
            _note("s1", "stewardship", "映射有问题，明天得修。"),
            _note("c1", "curiosity", "今日歌单还空着。"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    hit = _select_approved_latent_note({"s1"}, drive_key="stewardship")
    assert hit is not None
    assert hit["note_id"] == "s1"
    assert hit["pool_match"] == "relaxed_exclude"
    assert "歌单" not in hit["line"]


def test_draft_notes_never_deliver(monkeypatch):
    data = {
        "notes": [
            _note("d1", "stewardship", "还在草稿里。", status="draft"),
            _note("a1", "stewardship", "已确认的修屋。", status="approved"),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    hit = _select_approved_latent_note(set(), drive_key="stewardship")
    assert hit["line"] == "已确认的修屋。"


def test_stale_outward_note_with_live_wording_is_not_delivered(monkeypatch):
    old_day = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    data = {
        "notes": [
            _note(
                "old-news",
                "curiosity",
                "昨晚刚爆的模型消息，还挂在这里。",
                note_type="outward",
                source_created=old_day,
            ),
            _note(
                "timeless",
                "curiosity",
                "章鱼的腕足怎样分配局部决策？",
                note_type="outward",
                source_created=old_day,
            ),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)

    assert _latent_note_is_stale(data["notes"][0]) is True
    assert _latent_note_is_stale(data["notes"][1]) is False
    picks = {_select_approved_latent_note(set(), drive_key="curiosity")["note_id"] for _ in range(12)}
    assert picks == {"timeless"}


def test_recent_outward_note_keeps_source_provenance():
    source_day = datetime.utcnow().strftime("%Y-%m-%d")
    note = _note(
        "fresh-news",
        "curiosity",
        "昨晚刚发布的系统卡还有一处没看懂。",
        note_type="outward",
        source_created=source_day,
    )
    note.update({
        "source_kind": "window",
        "source_title": "系统卡",
        "source_bucket_id": "bucket-1",
    })

    assert _latent_note_is_stale(note) is False
    payload = _approved_latent_note_payload(note)
    assert payload["source_created"] == source_day
    assert payload["source_kind"] == "window"
    assert payload["source_title"] == "系统卡"
    assert payload["created_at"] == note["created_at"]


def test_pinned_is_reusable_not_vip_weight():
    fresh = {"pinned": False, "delivered_count": 0}
    pinned = {"pinned": True, "delivered_count": 0}
    assert _latent_select_weight(fresh) > _latent_select_weight(pinned)


def test_fresh_one_shot_preferred_over_pinned(monkeypatch):
    data = {
        "notes": [
            _note("p1", "curiosity", "常驻那句。", pinned=True),
            _note("f1", "curiosity", "新鲜一次。", pinned=False),
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)
    picks = [_select_approved_latent_note(set(), drive_key="curiosity")["line"] for _ in range(40)]
    # Weighted: fresh should dominate; allow rare pin draws
    assert picks.count("新鲜一次。") > picks.count("常驻那句。")


def test_low_stock_lists_empty_drives_first(monkeypatch):
    data = {
        "notes": [
            *[_note(f"c{i}", "curiosity", f"c{i}") for i in range(8)],
            _note("s1", "stewardship", "只一条"),
            _note("soc1", "social", "社1"),
            _note("soc2", "social", "社2"),
            *[_note(f"f{i}", "fatigue", f"乏{i}") for i in range(3)],
            # general 再空也不该进自动补货
        ]
    }
    monkeypatch.setattr("server._load_latent_notes", lambda: data)
    rows = _latent_low_stock_drives(data)
    tags = [r[0] for r in rows]
    assert "general" not in tags
    assert "social" in tags
    assert "fatigue" in tags
    # 绝对最少优先：social(2)/fatigue(3) 应排在 curiosity(8) 前
    assert tags.index("social") < tags.index("curiosity") if "curiosity" in tags else True
    assert tags.index("fatigue") < tags.index("curiosity") if "curiosity" in tags else True
