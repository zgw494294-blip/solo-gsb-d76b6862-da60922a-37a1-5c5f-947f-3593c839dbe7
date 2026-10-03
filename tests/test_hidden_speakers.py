"""播放中临时隐藏指定说话人（hidden_speakers）测试。

覆盖：
- 按 speaker 精确匹配提交无重复非空字符串数组：空数组清除、允许尚未
  出现者；非法集合（重复/空白/非字符串/非数组/缺失）422 且不改状态；
- 切换以当前游标的左闭右开活动区间发显示变更事件：原可见现隐藏发
  exit、原隐藏现可见发 enter，时刻为当前游标，先 exit 后 enter，同类
  按会话字幕顺序，无变化则无事件；游标不推进；
- 设置后 /advance 的 active、/events 的 active 与 events 均排除隐藏者，
  游标照常推进；隐藏期间整条掠过的短字幕不补发；
- 完整快照（创建/接纳/结束）、接纳冻结规则与分轨不受影响；接纳后
  设置保留；仅影响该会话、不持久化；
- 未知或已结束会话 404；切换与推进、事件推进、接纳、结束串行生效。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.playback import SessionFinished  # noqa: E402


def scene_payload(track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


HIDDEN_URL = "/sessions/{sid}/hidden_speakers"


def hide(client, sid, speakers):
    return client.post(
        HIDDEN_URL.format(sid=sid), json={"hidden_speakers": speakers}
    )


def make_session(client, scene_id="scene-hidden", payload=None):
    resp = client.put(f"/scenes/{scene_id}", json=payload or scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def revise(client, scene_id, expected_revision, subtitles, track_count=2):
    resp = client.put(f"/scenes/{scene_id}", json={
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    })
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True
    return resp.json()


def _event_triples(body):
    return [(e["time_ms"], e["type"], e["id"]) for e in body["events"]]


# ---------------------------------------------------------------------------
# 基本切换：隐藏发 exit、活动字幕排除隐藏者
# ---------------------------------------------------------------------------


def test_hide_active_speaker_emits_exit_and_filters_active():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 900：s1(Alice)、s2(Bob) 活动
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    resp = hide(client, sid, ["Bob"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["scene_id"] == "scene-hidden"
    assert body["source_revision"] == 1
    assert body["cursor_ms"] == 900  # 切换不推进游标
    assert body["hidden_speakers"] == ["Bob"]
    # 原可见现隐藏的活动字幕 s2 在当前游标发 exit，沿用事件字段
    assert body["events"] == [
        {"id": "s2", "speaker": "Bob", "track": 2, "time_ms": 900, "type": "exit"}
    ]
    # 可见活动字幕只剩 s1
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s1", 1)]


def test_hide_response_active_entries_carry_full_fields():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    body = hide(client, sid, ["Alice"]).json()
    # s1 被隐藏（exit）；活动字幕里只剩 s2，字段完整
    assert body["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 900, "type": "exit"}
    ]
    assert body["active"] == [
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2, "track": 2}
    ]


def test_hide_at_cursor_zero_emits_exit_for_zero_ms_active():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0：s1（start 0）活动，隐藏 Alice 即在 0 时刻发 s1 的 exit
    body = hide(client, sid, ["Alice"]).json()
    assert body["cursor_ms"] == 0
    assert body["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 0, "type": "exit"}
    ]
    assert body["active"] == []


def test_hide_speaker_without_active_subtitle_emits_no_event():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0：Bob 的 s2 要到 800 才开始；提前隐藏，无事件但设置保留
    resp = hide(client, sid, ["Bob"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["hidden_speakers"] == ["Bob"]
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s1", 1)]


def test_hide_unknown_speaker_allowed_and_echoed_in_submitted_order():
    """允许提交尚未出现（甚至永不出现）的说话人；回显按提交顺序。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    body = hide(client, sid, ["Zoe", "Bob"]).json()
    assert body["hidden_speakers"] == ["Zoe", "Bob"]
    # 只有活动的 Bob（s2）受影响；Zoe 无任何字幕，不产生事件
    assert [e["id"] for e in body["events"]] == ["s2"]


def test_hide_is_exact_speaker_match_case_sensitive():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # "bob" 不等于 "Bob"：无字幕被隐藏
    body = hide(client, sid, ["bob"]).json()
    assert body["events"] == []
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]


def test_resubmit_same_set_is_no_change_no_events():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert hide(client, sid, ["Bob"]).json()["events"] == [
        {"id": "s2", "speaker": "Bob", "track": 2, "time_ms": 900, "type": "exit"}
    ]
    # 再次提交同一集合：无变化则无事件，集合照旧回显
    body = hide(client, sid, ["Bob"]).json()
    assert body["events"] == []
    assert body["hidden_speakers"] == ["Bob"]
    assert body["cursor_ms"] == 900


def test_hide_respects_left_closed_right_open_interval_at_cursor():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 1000：s1[0,1000) 已结束（右开，不发 exit）；s3[1000,2500)
    # 刚开始（左闭，发 exit）
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})
    body = hide(client, sid, ["Alice"]).json()
    assert body["events"] == [
        {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000,
         "type": "exit"}
    ]
    assert body["active"] == [
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2, "track": 2}
    ]


# ---------------------------------------------------------------------------
# 恢复显示：enter 事件、排序（先 exit 后 enter、同类按会话顺序）
# ---------------------------------------------------------------------------


def test_clear_hidden_emits_enter_for_active_subtitle():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0 提前隐藏 Bob（s2 尚未开始，无事件）
    hide(client, sid, ["Bob"])
    # s2 在隐藏期间开始；游标到 1000 时清除：s2 此刻活动，发 enter
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})
    body = hide(client, sid, []).json()
    assert body["hidden_speakers"] == []
    assert body["cursor_ms"] == 1000
    assert body["events"] == [
        {"id": "s2", "speaker": "Bob", "track": 2, "time_ms": 1000, "type": "enter"}
    ]
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s2", 2),
        ("s3", 1),
    ]


def test_clear_after_subtitle_ended_emits_no_enter():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Bob"])
    # 游标到 3000：s2[800,3000) 已结束（右开），清除不补发 enter
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 3000})
    body = hide(client, sid, []).json()
    assert body["events"] == []
    assert body["active"] == []
    assert body["hidden_speakers"] == []


def test_same_instant_exit_before_enter_and_session_order_within_type():
    """同一次切换：先 exit 后 enter；同为 exit（或 enter）按会话字幕顺序。"""
    client = TestClient(app)
    # a[0,200) Alice，c[0,200) Carol：游标 100 时两条都活动
    client.put("/scenes/tie-hide", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "Alice", "start_ms": 0, "end_ms": 200},
            {"id": "c", "speaker": "Carol", "start_ms": 0, "end_ms": 200},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": "tie-hide"}).json()[
        "session_id"
    ]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 100})

    # 同时隐藏两人：两个 exit 同刻，按会话顺序 a 在 c 前
    body = hide(client, sid, ["Alice", "Carol"]).json()
    assert _event_triples(body) == [
        (100, "exit", "a"),
        (100, "exit", "c"),
    ]
    assert body["active"] == []

    # 同时恢复：两个 enter 同刻，按会话顺序 a 在 c 前
    body = hide(client, sid, []).json()
    assert _event_triples(body) == [
        (100, "enter", "a"),
        (100, "enter", "c"),
    ]
    assert [a["id"] for a in body["active"]] == ["a", "c"]


def test_mixed_hide_and_unhide_in_one_toggle_exit_before_enter():
    """同一次切换中现隐藏（exit）与现可见（enter）并存：exit 全在 enter 前，
    即使被恢复者的会话顺序更早。"""
    client = TestClient(app)
    # a[0,200) Alice，c[0,200) Carol：会话顺序 a 在 c 前
    client.put("/scenes/mix-hide", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "Alice", "start_ms": 0, "end_ms": 200},
            {"id": "c", "speaker": "Carol", "start_ms": 0, "end_ms": 200},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": "mix-hide"}).json()[
        "session_id"
    ]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 100})
    # 先隐藏 Alice（a 发 exit）
    hide(client, sid, ["Alice"])
    # 再一次切换：Alice 恢复（a enter）、Carol 隐藏（c exit）。
    # 会话顺序 a 在前，但 exit 必须整体排在 enter 之前。
    body = hide(client, sid, ["Carol"]).json()
    assert _event_triples(body) == [
        (100, "exit", "c"),
        (100, "enter", "a"),
    ]
    assert [a["id"] for a in body["active"]] == ["a"]


# ---------------------------------------------------------------------------
# 设置后推进 / 事件推进排除隐藏者，游标照常推进
# ---------------------------------------------------------------------------


def test_advance_active_excludes_hidden_speakers():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Bob"])

    # 原推进的 active 排除隐藏者：1000 时只看得到 s3（s2 被隐藏）
    body = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 1000}
    ).json()
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s3", 1)]
    assert body["cursor_ms"] == 1000  # 游标照常推进
    assert "events" not in body  # 原响应字段不变

    # 3000：s3 结束，s2 虽也结束但本就不可见；活动为空
    body = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 3000}
    ).json()
    assert body["active"] == []


def test_events_advance_excludes_hidden_speakers_enter_and_exit():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    # 游标 0 即隐藏 Bob：s2 的进入（800）与退出（3000）都不得出现在事件流
    hide(client, sid, ["Bob"])
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 3000}
    ).json()
    assert _event_triples(body) == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
        (2500, "exit", "s3"),
    ]
    assert body["active"] == []
    assert body["cursor_ms"] == 3000  # 游标照常推进


def test_hidden_short_subtitle_fully_between_samples_not_emitted():
    """隐藏期间整条掠过的短字幕：进入与退出都不补发。"""
    client = TestClient(app)
    client.put("/scenes/short-hidden", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000},
            {"id": "b", "speaker": "B", "start_ms": 500, "end_ms": 600},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": "short-hidden"}).json()[
        "session_id"
    ]
    # 游标 0 隐藏 B，随后直接跨过 b 的整条生命
    hide(client, sid, ["B"])
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 1000}
    ).json()
    assert _event_triples(body) == [(1000, "exit", "a")]
    assert body["active"] == []

    # 游标 1000 时清除：b 已结束，不补发 enter
    body = hide(client, sid, []).json()
    assert body["events"] == []


def test_hide_then_plain_advance_and_events_share_cursor():
    """切换不推进游标；两种推进共用游标，过滤口径一致。"""
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Bob"])

    # 事件推进等时（900 -> 900）：无事件，活动仍排除 s2
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 900}
    ).json()
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s1"]

    # 推进到 1000：s3 进入事件给出，s1 退出给出；s2 全程不可见
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 1000}
    ).json()
    assert _event_triples(body) == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
    ]


# ---------------------------------------------------------------------------
# 接纳：设置保留；完整快照、冻结规则与分轨不受影响
# ---------------------------------------------------------------------------


def test_hidden_speakers_retained_after_accept():
    client = TestClient(app)
    scene_id = "scene-hidden-accept"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Bob"])

    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 200

    # 设置保留：重复提交 ["Bob"] 无 exit 事件（若接纳把设置清空，此刻
    # 活动的 s2 会补发 exit）
    body = hide(client, sid, ["Bob"]).json()
    assert body["events"] == []
    assert body["hidden_speakers"] == ["Bob"]

    # 事件推进同样排除 Bob（s2 的 3000 退出不出现）
    body = client.post(
        f"/sessions/{sid}/events", json={"time_ms": 4000}
    ).json()
    assert body["source_revision"] == 2
    assert _event_triples(body) == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
        (3200, "enter", "s4"),
        (4000, "exit", "s3"),
        (4000, "exit", "s4"),
    ]


def test_full_snapshot_unaffected_by_hidden_speakers():
    """创建/接纳/结束的完整快照不随隐藏过滤，也不新增隐藏字段。"""
    client = TestClient(app)
    scene_id = "scene-hidden-snapshot"
    created = make_session(client, scene_id)
    sid = created["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    hide(client, sid, ["Bob"])
    hide(client, sid, ["Alice"])

    # 接纳（无场景变化的 no-op 成功）：快照仍是全部三条字幕
    state = client.post(f"/sessions/{sid}/accept").json()
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2", "s3"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]
    assert "hidden_speakers" not in state

    # 结束：最终完整快照同样不隐藏、不含隐藏字段
    final = client.post(f"/sessions/{sid}/finish").json()
    assert set(final.keys()) == set(created.keys())
    assert [s["id"] for s in final["subtitles"]] == ["s1", "s2", "s3"]
    assert final["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_accept_freeze_rules_still_apply_while_speaker_hidden():
    """隐藏不改变接纳冻结规则：已开始字幕（含被隐藏者）被修改仍 409。"""
    client = TestClient(app)
    scene_id = "scene-hidden-freeze"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # s2（Bob）已开始且被隐藏——隐藏不影响其“已开始”身份
    hide(client, sid, ["Bob"])

    subs = scene_payload()["subtitles"]
    subs[1]["locked_track"] = 1  # 改动已开始的 s2
    revise(client, scene_id, 1, subs)
    assert client.post(f"/sessions/{sid}/accept").status_code == 409

    # 失败的接纳不改会话：隐藏设置也仍在（s2 依旧不可见）
    body = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 900}
    ).json()
    assert [a["id"] for a in body["active"]] == ["s1"]


# ---------------------------------------------------------------------------
# 非法集合 422、失败不改状态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"hidden_speakers": ["Alice", "Alice"]},      # 重复
        {"hidden_speakers": [""]},                    # 空字符串
        {"hidden_speakers": ["   "]},                 # 全空白
        {"hidden_speakers": [1]},                     # 数字
        {"hidden_speakers": [True]},                  # 布尔
        {"hidden_speakers": [None]},                  # null
        {"hidden_speakers": ["Alice", 1]},            # 含非字符串
        {"hidden_speakers": "Alice"},                 # 字符串而非数组
        {"hidden_speakers": None},                    # null
        {"hidden_speakers": [["Alice"]]},             # 嵌套数组
        {},                                           # 字段缺失
    ],
)
def test_invalid_hidden_speakers_422(payload):
    client = TestClient(app)
    sid = make_session(client, "scene-hidden-422")["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    resp = client.post(
        HIDDEN_URL.format(sid=sid), json=payload
    )
    assert resp.status_code == 422


def test_invalid_toggle_does_not_change_state():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # 重复集合 422：Bob 不被隐藏
    assert hide(client, sid, ["Bob", "Bob"]).status_code == 422
    # 提交空集合：无 enter/exit 事件，证明此前隐藏集合仍为默认空
    body = hide(client, sid, []).json()
    assert body["events"] == []
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]

    # 再做一次：先合法隐藏，再用非法集合试图改动 → 失败不清除原设置
    hide(client, sid, ["Bob"])
    assert hide(client, sid, [""]).status_code == 422
    body = client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 1000}
    ).json()
    assert [a["id"] for a in body["active"]] == ["s3"]  # s2 仍被隐藏


# ---------------------------------------------------------------------------
# 404：未知 / 已结束会话；会话间隔离
# ---------------------------------------------------------------------------


def test_hidden_speakers_unknown_session_404():
    client = TestClient(app)
    assert hide(client, "no-such-session", []).status_code == 404


def test_hidden_speakers_after_finish_404():
    client = TestClient(app)
    sid = make_session(client, "scene-hidden-finish")["session_id"]
    assert client.post(f"/sessions/{sid}/finish").status_code == 200
    assert hide(client, sid, ["Bob"]).status_code == 404


def test_hidden_set_is_per_session_and_not_persisted():
    client = TestClient(app)
    client.put("/scenes/scene-two", json=scene_payload())
    a = client.post("/sessions", json={"scene_id": "scene-two"}).json()
    b = client.post("/sessions", json={"scene_id": "scene-two"}).json()
    client.post(f"/sessions/{a['session_id']}/advance", json={"time_ms": 900})
    client.post(f"/sessions/{b['session_id']}/advance", json={"time_ms": 900})

    # 仅会话 a 隐藏 Bob
    body = hide(client, a["session_id"], ["Bob"]).json()
    assert [x["id"] for x in body["active"]] == ["s1"]

    # 会话 b 不受影响：s2 仍可见，且自身隐藏集合为空（提交空集合无事件）
    body = client.post(
        f"/sessions/{b['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert [x["id"] for x in body["active"]] == ["s1", "s2"]
    body = hide(client, b["session_id"], []).json()
    assert body["events"] == []

    # 结束两个会话后，基于同场景新建的会话隐藏默认空（设置不持久化）
    client.post(f"/sessions/{a['session_id']}/finish")
    client.post(f"/sessions/{b['session_id']}/finish")
    c = client.post("/sessions", json={"scene_id": "scene-two"}).json()
    client.post(f"/sessions/{c['session_id']}/advance", json={"time_ms": 900})
    body = client.post(
        f"/sessions/{c['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert [x["id"] for x in body["active"]] == ["s1", "s2"]


def test_closed_session_reference_toggle_raises_session_finished(monkeypatch):
    """已取得会话引用，finish 先生效后，切换在锁内看到墓碑（404）。"""
    client = TestClient(app)
    sid = make_session(client, "scene-hidden-tomb")["session_id"]
    manager = main_module.get_playback()
    session = manager.get(sid)
    assert session is not None
    manager.finish(sid)

    with pytest.raises(SessionFinished):
        manager.set_hidden_speakers(session, ["Bob"])

    # HTTP 层：路由拿到已置墓碑的在途引用同样 404
    monkeypatch.setattr(manager, "get", lambda _sid: session)
    local = TestClient(app)
    assert hide(local, sid, ["Bob"]).status_code == 404


# ---------------------------------------------------------------------------
# 并发：切换与结束、推进串行生效
# ---------------------------------------------------------------------------


def test_concurrent_toggle_and_finish_serialized():
    client = TestClient(app)
    sid = make_session(client, "scene-hidden-race")["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    results = []
    barrier = threading.Barrier(8)

    def worker(kind):
        local = TestClient(app)
        barrier.wait()
        if kind == "finish":
            resp = local.post(f"/sessions/{sid}/finish")
        else:
            resp = local.post(
                HIDDEN_URL.format(sid=sid), json={"hidden_speakers": ["Bob"]}
            )
        results.append((kind, resp.status_code))

    kinds = ["finish"] + ["toggle"] * 7
    threads = [threading.Thread(target=worker, args=(k,)) for k in kinds]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 串行后只有两类结果：结束先持锁则 1 个 200 finish、切换全 404；
    # 切换先持锁则结束 200、其余切换 404。绝不出现部分生效的异常状态。
    assert all(status in (200, 404) for _, status in results)
    assert [s for k, s in results if k == "finish"] == [200]
    # 结束后一切请求 404
    assert hide(client, sid, []).status_code == 404
