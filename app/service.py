"""核心业务逻辑：外字号清单 / 对照表分稿次接收、整稿启用、交印读取。

业务规则
--------
1. 网页按“册名 + 正整数稿次”分别收两份 CSV：
   - 排版组《外字号清单》：每行一个外字号；
   - 校勘组《对照表》：每行外字号 ↔ 单字。
2. 两表可先后到达。只到一份时稿次为 pending（待补），响应提示还缺哪份表、
   对照表还缺哪些编号。
3. 未启用（pending）的稿次可重传改表，重传即整表替换。
4. 同稿次两份表齐全、清单里每个用到的编号都在对照表中唯一对应一个字，
   且稿次高于当前启用稿次时，整稿自动启用：旧启用稿标记 superseded。
5. 已启用稿次的原表不接受重传（重传不重复启用）；改表必须使用更高的新稿次；
   迟到的低稿次不能换回当前启用稿。
6. 网页显示当前启用稿次；交印只读取当时启用稿的完整用字。
7. 写入失败时整笔回滚，原稿与待补记录保持原样，重启后仍可接着补传。
8. 原用字表上传只适用于“尚未启用任何外字稿”的分册。
9. 演示清空时一并清除所有外字稿。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from .database import connect, init_db
from .errors import DomainError, SaveError

# 进程内互斥：同一时刻只有一个上传/启用/交印事务在改写状态。
_state_lock = threading.RLock()

# 测试可覆盖数据库路径（重启续传测试用独立库）；None 时取 database.DB_PATH。
_db_path_override: str | None = None

# 测试钩子：置 True 后下一次写事务在提交前抛 SaveError（模拟磁盘写入失败）。
_fail_next_write: bool = False


def set_fail_next_write(value: bool) -> None:
    global _fail_next_write
    _fail_next_write = value


def set_db_path(path: str | None) -> None:
    global _db_path_override
    _db_path_override = path
    if path is not None:
        init_db(path)


def _conn():
    return connect(_db_path_override)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_db() -> None:
    init_db(_db_path_override)


# ---------- 基础读取 ----------


def _get_or_create_volume(conn, name: str) -> int:
    row = conn.execute("SELECT volume_id FROM volumes WHERE name=?", (name,)).fetchone()
    if row:
        return row["volume_id"]
    cur = conn.execute("INSERT INTO volumes(name) VALUES (?)", (name,))
    return cur.lastrowid


def _active_draft_no(conn, volume_id: int) -> int | None:
    row = conn.execute(
        "SELECT draft_no FROM drafts WHERE volume_id=? AND status='active'",
        (volume_id,),
    ).fetchone()
    return row["draft_no"] if row else None


def _load_list_codes(conn, volume_id: int, draft_no: int) -> list[str]:
    return [
        r["code"]
        for r in conn.execute(
            "SELECT code FROM draft_list_codes WHERE volume_id=? AND draft_no=? "
            "ORDER BY source_order",
            (volume_id, draft_no),
        ).fetchall()
    ]


def _load_mappings(conn, volume_id: int, draft_no: int) -> list[dict]:
    return [
        {"code": r["code"], "glyph_char": r["glyph_char"]}
        for r in conn.execute(
            "SELECT code, glyph_char FROM draft_mappings WHERE volume_id=? AND draft_no=? "
            "ORDER BY source_order",
            (volume_id, draft_no),
        ).fetchall()
    ]


def _draft_summary(conn, volume_id: int, draft_no: int) -> dict:
    draft = conn.execute(
        "SELECT status, has_list, has_mapping FROM drafts WHERE volume_id=? AND draft_no=?",
        (volume_id, draft_no),
    ).fetchone()
    if draft is None:
        return None
    codes = _load_list_codes(conn, volume_id, draft_no)
    mappings = _load_mappings(conn, volume_id, draft_no)
    mapping_codes = {m["code"] for m in mappings}
    missing_codes = [c for c in codes if c not in mapping_codes]
    return {
        "draft_no": draft_no,
        "status": draft["status"],
        "has_list": bool(draft["has_list"]),
        "has_mapping": bool(draft["has_mapping"]),
        "list_count": len(codes),
        "mapping_count": len(mappings),
        "list_codes": codes,
        "missing_tables": [
            name
            for ok, name in (
                (draft["has_list"], "外字号清单"),
                (draft["has_mapping"], "对照表"),
            )
            if not ok
        ],
        "missing_codes": missing_codes,
    }


def _check_activation(conn, volume_id: int, draft_no: int) -> dict | None:
    """满足启用条件则整稿启用，返回启用结果；否则返回 None（保持待补）。"""
    summary = _draft_summary(conn, volume_id, draft_no)
    if summary is None or summary["status"] != "pending":
        return None
    if not summary["has_list"] or not summary["has_mapping"]:
        return None
    if summary["missing_codes"]:
        return None
    current = _active_draft_no(conn, volume_id)
    if current is not None and draft_no <= current:
        # 迟到的旧稿不能换回；正常上传入口已拦截，这里做事务内兜底
        return None

    conn.execute(
        "UPDATE drafts SET status='active', activated_at=? "
        "WHERE volume_id=? AND draft_no=?",
        (_now_iso(), volume_id, draft_no),
    )
    if current is not None:
        conn.execute(
            "UPDATE drafts SET status='superseded' WHERE volume_id=? AND draft_no=?",
            (volume_id, current),
        )
    return {"activated": True, "previous_active": current, "active_draft": draft_no}


# ---------- 上传 ----------


def _validate_draft_no(draft_no: int) -> None:
    if not isinstance(draft_no, int) or isinstance(draft_no, bool) or draft_no <= 0:
        raise DomainError("稿次必须是正整数")


def _upload(volume_name: str, draft_no: int, kind: str, payload) -> dict:
    """kind: 'list'（外字号清单）或 'mapping'（对照表）。"""
    _validate_draft_no(draft_no)
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        volume_id = _get_or_create_volume(conn, volume_name)
        draft = conn.execute(
            "SELECT status, has_list, has_mapping FROM drafts "
            "WHERE volume_id=? AND draft_no=?",
            (volume_id, draft_no),
        ).fetchone()

        if draft is not None and draft["status"] == "active":
            raise DomainError(
                f"册「{volume_name}」稿次 {draft_no} 已启用，原表不能重传、不会重复启用；"
                "改表请使用更高的新稿次",
                409,
            )
        current = _active_draft_no(conn, volume_id)
        if current is not None and draft_no <= current:
            raise DomainError(
                f"册「{volume_name}」当前已启用稿次 {current}；迟到的稿次 {draft_no} "
                "不能换回或覆盖，改表请用更高的新稿次",
                409,
            )

        if draft is None:
            conn.execute(
                "INSERT INTO drafts(volume_id, draft_no, status) VALUES (?,?, 'pending')",
                (volume_id, draft_no),
            )

        replaced = draft is not None and (
            draft["has_list"] if kind == "list" else draft["has_mapping"]
        )
        if kind == "list":
            conn.execute(
                "DELETE FROM draft_list_codes WHERE volume_id=? AND draft_no=?",
                (volume_id, draft_no),
            )
            conn.executemany(
                "INSERT INTO draft_list_codes(volume_id, draft_no, code, source_order) "
                "VALUES (?,?,?,?)",
                [(volume_id, draft_no, code, i) for i, code in enumerate(payload)],
            )
            conn.execute(
                "UPDATE drafts SET has_list=1 WHERE volume_id=? AND draft_no=?",
                (volume_id, draft_no),
            )
        else:
            conn.execute(
                "DELETE FROM draft_mappings WHERE volume_id=? AND draft_no=?",
                (volume_id, draft_no),
            )
            conn.executemany(
                "INSERT INTO draft_mappings(volume_id, draft_no, code, glyph_char, source_order) "
                "VALUES (?,?,?,?,?)",
                [(volume_id, draft_no, code, ch, i) for i, (code, ch) in enumerate(payload)],
            )
            conn.execute(
                "UPDATE drafts SET has_mapping=1 WHERE volume_id=? AND draft_no=?",
                (volume_id, draft_no),
            )

        activation = _check_activation(conn, volume_id, draft_no)
        if _fail_next_write:
            raise SaveError("模拟写入失败（测试钩子）")
        conn.commit()
    except DomainError:
        conn.rollback()
        if _fail_next_write:
            set_fail_next_write(False)
        raise
    except Exception as exc:  # 写入失败：原稿与待补记录保持原样
        conn.rollback()
        set_fail_next_write(False)
        raise SaveError(f"数据库写入失败，已回滚: {exc}")
    finally:
        conn.close()

    # 提交后重新组装响应（连接已关闭，重开只读连接）
    return _upload_response(volume_name, draft_no, kind, bool(replaced), activation)


def _upload_response(volume_name, draft_no, kind, replaced, activation) -> dict:
    conn = _conn()
    try:
        volume_id = conn.execute(
            "SELECT volume_id FROM volumes WHERE name=?", (volume_name,)
        ).fetchone()["volume_id"]
        summary = _draft_summary(conn, volume_id, draft_no)
        active = _active_draft_no(conn, volume_id)
    finally:
        conn.close()

    received = "外字号清单" if kind == "list" else "对照表"
    response = {
        "volume": volume_name,
        "draft_no": draft_no,
        "received": received,
        "replaced": replaced,
        "draft": summary,
        "active_draft_no": active,
        "activated": bool(activation),
    }
    if activation:
        response["message"] = (
            f"两份表齐全且编号均唯一对应单字，稿次 {draft_no} 已整稿启用"
            + (f"，原启用稿次 {activation['previous_active']} 已作废"
               if activation["previous_active"] is not None else "")
        )
    else:
        hints = []
        if summary["missing_tables"]:
            hints.append("还缺表：" + "、".join(summary["missing_tables"]))
        if summary["missing_codes"]:
            hints.append(
                f"对照表还缺 {len(summary['missing_codes'])} 个编号: "
                + "、".join(summary["missing_codes"])
            )
        response["message"] = (
            f"{received}已收到，稿次 {draft_no} 待补。" + ("；".join(hints) if hints else "")
        )
    return response


def upload_list(volume_name: str, draft_no: int, codes: list[str]) -> dict:
    with _state_lock:
        return _upload(volume_name, draft_no, "list", codes)


def upload_mapping(volume_name: str, draft_no: int, pairs: list[tuple[str, str]]) -> dict:
    with _state_lock:
        return _upload(volume_name, draft_no, "mapping", pairs)


# ---------- 原用字表 ----------


def upload_legacy(volume_name: str, content: str, char_count: int) -> dict:
    """原用字表：仅适用于尚未启用任何外字稿的分册。"""
    if not content:
        raise DomainError("原用字表为空")
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        volume_id = _get_or_create_volume(conn, volume_name)
        active = _active_draft_no(conn, volume_id)
        if active is not None:
            raise DomainError(
                f"册「{volume_name}」已启用外字稿次 {active}，原用字表上传不再适用；"
                "请按稿次上传外字号清单与对照表",
                409,
            )
        any_draft = conn.execute(
            "SELECT 1 FROM drafts WHERE volume_id=? LIMIT 1", (volume_id,)
        ).fetchone()
        if any_draft is not None:
            raise DomainError(
                f"册「{volume_name}」已存在外字稿（尚未启用），原用字表上传不适用",
                409,
            )
        conn.execute("DELETE FROM legacy_tables WHERE volume_id=?", (volume_id,))
        conn.execute(
            "INSERT INTO legacy_tables(volume_id, content) VALUES (?,?)",
            (volume_id, content),
        )
        conn.commit()
    except DomainError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise SaveError(f"数据库写入失败，已回滚: {exc}")
    finally:
        conn.close()
    return {
        "volume": volume_name,
        "kind": "legacy",
        "char_count": char_count,
        "message": "原用字表已收录（该册尚未启用外字稿）",
        "uploaded_at": _now_iso(),
    }


# ---------- 交印 ----------


def print_volume(volume_name: str, fail_commit: bool = False) -> dict:
    """交印：只读取当时启用稿的完整用字。"""
    with _state_lock:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            vol = conn.execute(
                "SELECT volume_id FROM volumes WHERE name=?", (volume_name,)
            ).fetchone()
            if vol is None:
                raise DomainError(f"册「{volume_name}」不存在", 404)
            volume_id = vol["volume_id"]
            active_no = _active_draft_no(conn, volume_id)
            if active_no is None:
                raise DomainError(
                    f"册「{volume_name}」尚无启用的外字稿，不能交印；请补齐清单与对照表",
                    409,
                )
            mappings = _load_mappings(conn, volume_id, active_no)
            code_to_char = {m["code"]: m["glyph_char"] for m in mappings}
            codes = _load_list_codes(conn, volume_id, active_no)
            # 启用条件保证清单编号都有唯一对应字，这里按清单顺序组装完整用字
            chars = [code_to_char[c] for c in codes]
            job_id = f"PT{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
            payload = {
                "job_id": job_id,
                "volume": volume_name,
                "active_draft_no": active_no,
                "printed_at": _now_iso(),
                "code_count": len(codes),
                "items": [
                    {"code": code, "glyph_char": ch} for code, ch in zip(codes, chars)
                ],
                "full_text": "".join(chars),
            }
            if fail_commit:
                # 测试钩子：模拟写入/提交失败
                raise SaveError("模拟保存失败（测试）")
            conn.commit()
        except DomainError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, SaveError):
                raise
            raise SaveError(f"交印读取失败，已回滚: {exc}")
        finally:
            conn.close()
        return payload


# ---------- 状态总览 ----------


def get_state() -> dict:
    conn = _conn()
    try:
        volumes = []
        for v in conn.execute("SELECT volume_id, name FROM volumes ORDER BY volume_id"):
            vid = v["volume_id"]
            active_no = _active_draft_no(conn, vid)
            drafts = [
                _draft_summary(conn, vid, r["draft_no"])
                for r in conn.execute(
                    "SELECT draft_no FROM drafts WHERE volume_id=? ORDER BY draft_no",
                    (vid,),
                ).fetchall()
            ]
            legacy = conn.execute(
                "SELECT content, uploaded_at FROM legacy_tables WHERE volume_id=?",
                (vid,),
            ).fetchone()
            entry = {
                "volume": v["name"],
                "active_draft_no": active_no,
                "drafts": drafts,
                "legacy_table": (
                    {"content": legacy["content"], "uploaded_at": legacy["uploaded_at"]}
                    if legacy is not None
                    else None
                ),
            }
            if active_no is not None:
                mappings = _load_mappings(conn, vid, active_no)
                codes = _load_list_codes(conn, vid, active_no)
                code_to_char = {m["code"]: m["glyph_char"] for m in mappings}
                entry["active_full_text"] = "".join(code_to_char[c] for c in codes)
            else:
                entry["active_full_text"] = None
            volumes.append(entry)
        return {"volumes": volumes}
    finally:
        conn.close()


def reset_all() -> None:
    """演示清空：所有外字稿、对照表、原用字表与分册全部清除。"""
    with _state_lock:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "draft_mappings",
                "draft_list_codes",
                "legacy_tables",
                "drafts",
                "volumes",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
