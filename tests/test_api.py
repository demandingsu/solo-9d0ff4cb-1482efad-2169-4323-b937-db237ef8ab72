"""端到端测试：外字号清单/对照表分稿次接收、错序到件、整稿启用、
稿次状态机、交印只读启用稿、写入失败回滚、重启续传、清空。

运行：
    PYTHONPATH=.:.pydeps/lib/python3.11/site-packages pytest -q
"""
from __future__ import annotations

import io
import os
import tempfile

import pytest

# 每个测试会话使用独立临时库
_tmp = tempfile.TemporaryDirectory()
os.environ["GLYPH_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    service.set_db_path(None)  # 回到本会话的临时库
    service.ensure_db()
    service.reset_all()
    with TestClient(app) as c:
        yield c
    service.set_db_path(None)


def _csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


def _list_csv(codes: list[str]) -> bytes:
    return _csv("外字号\n" + "\n".join(codes) + "\n")


def _mapping_csv(pairs: list[tuple[str, str]]) -> bytes:
    buf = io.StringIO()
    buf.write("外字号,字\n")
    for code, ch in pairs:
        buf.write(f"{code},{ch}\n")
    return _csv(buf.getvalue())


def _upload_list(client, volume: str, draft: int, codes: list[str]):
    return client.post(
        f"/api/volumes/{volume}/drafts/{draft}/list",
        files={"file": ("list.csv", _list_csv(codes), "text/csv")},
    )


def _upload_mapping(client, volume: str, draft: int, pairs: list[tuple[str, str]]):
    return client.post(
        f"/api/volumes/{volume}/drafts/{draft}/mapping",
        files={"file": ("mapping.csv", _mapping_csv(pairs), "text/csv")},
    )


# ---------- 1. 两表齐全后自动启用（先后到达） ----------


def test_two_tables_arrive_sequentially_then_activate(client):
    r = _upload_list(client, "册甲", 1, ["W1", "W2", "W3"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["activated"] is False
    assert d["active_draft_no"] is None
    assert d["draft"]["missing_tables"] == ["对照表"]
    assert "还缺表" in d["message"]

    r = _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W2", "地")])
    assert r.status_code == 200, r.text
    d = r.json()
    # 表齐了但清单编号 W3 还没有对应字
    assert d["activated"] is False
    assert d["draft"]["missing_codes"] == ["W3"]
    assert "W3" in d["message"]

    # 补传对照表（未启用稿可重传改表）
    r = _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W2", "地"), ("W3", "玄")])
    d = r.json()
    assert r.status_code == 200, r.text
    assert d["activated"] is True
    assert d["active_draft_no"] == 1
    assert d["draft"]["status"] == "active"


# ---------- 2. 错序到件：对照表先到、清单后到 ----------


def test_mapping_arrives_before_list(client):
    r = _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W2", "地")])
    d = r.json()
    assert r.status_code == 200, r.text
    assert d["activated"] is False
    assert d["draft"]["missing_tables"] == ["外字号清单"]

    r = _upload_list(client, "册甲", 1, ["W1", "W2"])
    d = r.json()
    assert d["activated"] is True and d["active_draft_no"] == 1


# ---------- 3. 缺编号 / 重复编号 / 重复字 ----------


def test_missing_code_keeps_pending(client):
    _upload_list(client, "册甲", 1, ["W1", "W2"])
    r = _upload_mapping(client, "册甲", 1, [("W1", "天")])
    d = r.json()
    assert d["activated"] is False
    assert d["draft"]["missing_codes"] == ["W2"]
    # 状态接口同样提示待补
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert vol["active_draft_no"] is None
    assert vol["drafts"][0]["missing_codes"] == ["W2"]


def test_duplicate_code_and_char_rejected(client):
    r = client.post(
        "/api/volumes/册甲/drafts/1/mapping",
        files={"file": ("m.csv", _csv("外字号,字\nW1,天\nW1,地\n"), "text/csv")},
    )
    assert r.status_code == 400 and "外字号重复" in r.json()["error"]

    r = client.post(
        "/api/volumes/册甲/drafts/1/mapping",
        files={"file": ("m.csv", _csv("外字号,字\nW1,天\nW2,天\n"), "text/csv")},
    )
    assert r.status_code == 400 and "单字重复" in r.json()["error"]

    r = client.post(
        "/api/volumes/册甲/drafts/1/mapping",
        files={"file": ("m.csv", _csv("外字号,字\nW1,天地\n"), "text/csv")},
    )
    assert r.status_code == 400 and "单个字符" in r.json()["error"]


# ---------- 4. 未启用稿可重传改表，重传即替换 ----------


def test_pending_draft_can_be_replaced(client):
    _upload_list(client, "册甲", 1, ["W1", "W2"])
    r = _upload_list(client, "册甲", 1, ["W1", "W3"])
    d = r.json()
    assert r.status_code == 200 and d["replaced"] is True
    assert d["draft"]["list_codes"] == ["W1", "W3"]
    _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W3", "玄")])
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert vol["active_draft_no"] == 1


# ---------- 5. 更高稿次启用并作废旧稿；同编号可对应不同字 ----------


def test_higher_draft_supersedes_and_same_code_can_change_char(client):
    _upload_list(client, "册甲", 1, ["W1", "W2"])
    _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W2", "地")])
    p1 = client.post("/api/volumes/册甲/print").json()
    assert p1["full_text"] == "天地"

    # 稿次 2：W2 改对应“玄”——同一编号在不同稿代表不同的字
    _upload_list(client, "册甲", 2, ["W1", "W2"])
    r = _upload_mapping(client, "册甲", 2, [("W1", "天"), ("W2", "玄")])
    d = r.json()
    assert d["activated"] is True and d["active_draft_no"] == 2
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    by_no = {x["draft_no"]: x["status"] for x in vol["drafts"]}
    assert by_no == {1: "superseded", 2: "active"}

    p2 = client.post("/api/volumes/册甲/print").json()
    assert p2["active_draft_no"] == 2 and p2["full_text"] == "天玄"


# ---------- 6. 已启用稿不能重传；迟到旧稿不能换回 ----------


def test_active_draft_is_locked_and_late_old_draft_rejected(client):
    _upload_list(client, "册甲", 1, ["W1"])
    _upload_mapping(client, "册甲", 1, [("W1", "天")])
    assert client.get("/api/state").json()["volumes"][0]["active_draft_no"] == 1

    r = _upload_list(client, "册甲", 1, ["W1", "W2"])
    assert r.status_code == 409 and "已启用" in r.json()["error"]

    r = _upload_mapping(client, "册甲", 1, [("W1", "天")])
    assert r.status_code == 409 and "已启用" in r.json()["error"]

    # 启用稿次 2 后，迟到的稿次 1（此处尝试任何上传）仍被拒
    _upload_list(client, "册甲", 2, ["W1"])
    _upload_mapping(client, "册甲", 2, [("W1", "天")])
    r = _upload_mapping(client, "册甲", 1, [("W1", "地")])
    assert r.status_code == 409 and "迟到" in r.json()["error"]

    # 当前启用仍是稿次 2，用字没有被旧稿换回
    p = client.post("/api/volumes/册甲/print").json()
    assert p["active_draft_no"] == 2 and p["full_text"] == "天"


def test_lower_draft_never_activates_even_if_complete(client):
    # 先备一个齐全但稿次更低的待补稿，再启用更高稿次：低稿永远不能启用
    _upload_list(client, "册甲", 2, ["W9"])
    _upload_list(client, "册甲", 1, ["W1"])
    _upload_mapping(client, "册甲", 1, [("W1", "天")])
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert vol["active_draft_no"] == 1  # 此时只有稿次1，正常启用

    _upload_mapping(client, "册甲", 2, [("W9", "地")])
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert vol["active_draft_no"] == 2
    # 没有启用之后，任何对稿次1的补传都被拒
    r = _upload_list(client, "册甲", 1, ["W1"])
    assert r.status_code == 409


# ---------- 7. 交印只读取当时启用稿的完整用字 ----------


def test_print_uses_active_draft_only_and_blocks_without_active(client):
    # 待补稿存在但未启用：不能交印
    _upload_list(client, "册甲", 1, ["W1", "W2"])
    r = client.post("/api/volumes/册甲/print")
    assert r.status_code == 409 and "尚无启用" in r.json()["error"]

    _upload_mapping(client, "册甲", 1, [("W1", "天"), ("W2", "地")])
    p = client.post("/api/volumes/册甲/print").json()
    assert p["code_count"] == 2
    assert [x["code"] for x in p["items"]] == ["W1", "W2"]
    assert [x["glyph_char"] for x in p["items"]] == ["天", "地"]

    r = client.get("/api/volumes/册甲/print/download")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8-sig")
    assert "W1" in text and "天" in text and "稿次：1" in text

    # 未知册 404
    assert client.post("/api/volumes/不存在/print").status_code == 404


# ---------- 8. 写入失败：回滚，原稿与待补记录保持不变，可继续补传 ----------


def test_write_failure_rolls_back_and_keeps_pending(client):
    _upload_list(client, "册甲", 1, ["W1"])
    # 模拟交印阶段写入失败（启用稿不存在，本应 409；先造一个启用稿再测失败钩子）
    _upload_mapping(client, "册甲", 1, [("W1", "天")])
    r = client.post("/api/volumes/册甲/print", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500 and r.json()["type"] == "SaveError"

    # 状态未被破坏，正常交印仍可用
    p = client.post("/api/volumes/册甲/print").json()
    assert p["full_text"] == "天"

    # 上传事务失败：测试钩子让写事务在提交前抛错，验证当笔整体回滚、
    # 已启用的稿次1原稿不受影响；待补稿可重新建立并继续补传
    _upload_list(client, "册甲", 2, ["W1"])
    service.set_fail_next_write(True)
    r = _upload_mapping(client, "册甲", 2, [("W1", "地")])
    assert r.status_code == 500 and r.json()["type"] == "SaveError"

    # 启用稿仍是1，且当笔对照表未落库（稿次2仍是只有清单的待补稿）
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert vol["active_draft_no"] == 1
    d2 = next(x for x in vol["drafts"] if x["draft_no"] == 2)
    assert d2["status"] == "pending" and d2["has_mapping"] is False
    r = _upload_mapping(client, "册甲", 2, [("W1", "地")])
    assert r.status_code == 200 and r.json()["activated"] is True


# ---------- 9. 重启后待补记录仍在，可接着补传 ----------


def test_pending_records_survive_restart(tmp_path):
    db_path = str(tmp_path / "restart.db")
    service.set_db_path(db_path)  # 独立库，模拟另一进程的数据文件
    try:
        service.ensure_db()
        with TestClient(app) as c1:
            r = _upload_list(c1, "册甲", 1, ["W1", "W2"])
            assert r.status_code == 200 and r.json()["activated"] is False
        # 模拟进程重启：重新 ensure_db + 新客户端，待补记录仍在
        service.ensure_db()
        with TestClient(app) as c2:
            state = c2.get("/api/state").json()
            vol = next(v for v in state["volumes"] if v["volume"] == "册甲")
            assert vol["active_draft_no"] is None
            assert vol["drafts"][0]["missing_tables"] == ["对照表"]
            r = _upload_mapping(c2, "册甲", 1, [("W1", "天"), ("W2", "地")])
            assert r.json()["activated"] is True
            assert c2.post("/api/volumes/册甲/print").json()["full_text"] == "天地"
    finally:
        service.set_db_path(None)


# ---------- 10. 原用字表：仅限未启用外字稿的分册 ----------


def test_legacy_upload_rules(client):
    r = client.post(
        "/api/volumes/册乙/legacy",
        files={"file": ("legacy.csv", _csv("字\n天\n地\n玄\n"), "text/csv")},
    )
    assert r.status_code == 200 and r.json()["char_count"] == 3
    state = client.get("/api/state").json()
    vol = next(v for v in state["volumes"] if v["volume"] == "册乙")
    assert vol["legacy_table"] is not None and vol["active_draft_no"] is None

    # 一旦启用外字稿，原用字表上传即被拒
    _upload_list(client, "册乙", 1, ["W1"])
    _upload_mapping(client, "册乙", 1, [("W1", "天")])
    r = client.post(
        "/api/volumes/册乙/legacy",
        files={"file": ("legacy.csv", _csv("字\n天\n"), "text/csv")},
    )
    assert r.status_code == 409 and "已启用外字稿" in r.json()["error"]

    # 已有待补外字稿（未启用）的册同样不适用
    r = client.post(
        "/api/volumes/册丙/legacy",
        files={"file": ("legacy.csv", _csv("字\n天\n"), "text/csv")},
    )
    assert r.status_code == 200
    _upload_list(client, "册丙", 1, ["W7"])
    r = client.post(
        "/api/volumes/册丙/legacy",
        files={"file": ("legacy.csv", _csv("字\n天\n"), "text/csv")},
    )
    assert r.status_code == 409 and "外字稿" in r.json()["error"]


# ---------- 11. 演示清空：所有外字稿一并清除 ----------


def test_reset_clears_everything(client):
    _upload_list(client, "册甲", 1, ["W1"])
    _upload_mapping(client, "册甲", 1, [("W1", "天")])
    client.post(
        "/api/volumes/册乙/legacy",
        files={"file": ("legacy.csv", _csv("字\n天\n"), "text/csv")},
    )
    assert len(client.get("/api/state").json()["volumes"]) == 2

    r = client.post("/api/reset")
    assert r.status_code == 200
    state = client.get("/api/state").json()
    assert state["volumes"] == []
    # 清空后同稿次可重新开始
    r = _upload_list(client, "册甲", 1, ["W1"])
    assert r.status_code == 200


# ---------- 12. 稿次参数与 CSV 格式校验 ----------


def test_draft_no_must_be_positive_int(client):
    # 0 通过路径类型转换，由业务层拒绝（400）
    r = client.post(
        "/api/volumes/册甲/drafts/0/list",
        files={"file": ("x.csv", _list_csv(["W1"]), "text/csv")},
    )
    assert r.status_code == 400 and "正整数" in r.json()["error"]
    # 负整数无法通过路由层 int 校验；本路由把 0/负数都当业务校验，统一 400
    r = client.post(
        "/api/volumes/册甲/drafts/-3/list",
        files={"file": ("x.csv", _list_csv(["W1"]), "text/csv")},
    )
    assert r.status_code == 400 and "正整数" in r.json()["error"]


def test_bad_csv_headers_and_encoding(client):
    r = client.post(
        "/api/volumes/册甲/drafts/1/mapping",
        files={"file": ("x.csv", _csv("foo,bar\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400 and "缺少必需列" in r.json()["error"]

    # 无表头清单：首行首格即编号
    r = client.post(
        "/api/volumes/册甲/drafts/1/list",
        files={"file": ("x.csv", "W1\nW2\n".encode("gb18030"), "text/csv")},
    )
    assert r.status_code == 200 and r.json()["draft"]["list_codes"] == ["W1", "W2"]

    # 空文件
    r = client.post(
        "/api/volumes/册甲/drafts/1/list",
        files={"file": ("x.csv", b"", "text/csv")},
    )
    assert r.status_code == 400 and "为空" in r.json()["error"]
