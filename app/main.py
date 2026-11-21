"""FastAPI 路由与页面：外字号稿次管理。"""
from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import service
from .errors import DomainError
from .parsers import parse_legacy_csv, parse_list_csv, parse_mapping_csv

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.ensure_db()
    yield


app = FastAPI(
    title="古籍制版 · 外字号稿次管理",
    description=(
        "按册名与正整数稿次分别接收排版组外字号清单与校勘组对照表；"
        "两表可先后到达、待补提示；齐全且编号唯一对应单字且稿次更高时整稿启用；"
        "交印只读取当时启用稿的完整用字。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def _domain_handler(_request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "type": exc.__class__.__name__},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- 两份 CSV 上传 ----------


@app.post(
    "/api/volumes/{volume_name}/drafts/{draft_no}/list",
    summary="上传某册某稿次的外字号清单（排版组，每行一个外字号）",
)
async def upload_list(volume_name: str, draft_no: int, file: UploadFile = File(...)):
    codes = parse_list_csv(await file.read())
    return service.upload_list(volume_name, draft_no, codes)


@app.post(
    "/api/volumes/{volume_name}/drafts/{draft_no}/mapping",
    summary="上传某册某稿次的对照表（校勘组，每行外字号与单字）",
)
async def upload_mapping(volume_name: str, draft_no: int, file: UploadFile = File(...)):
    pairs = parse_mapping_csv(await file.read())
    return service.upload_mapping(volume_name, draft_no, pairs)


@app.post(
    "/api/volumes/{volume_name}/legacy",
    summary="原用字表上传（仅适用于尚未启用外字稿的分册）",
)
async def upload_legacy(volume_name: str, file: UploadFile = File(...)):
    content, char_count = parse_legacy_csv(await file.read())
    return service.upload_legacy(volume_name, content, char_count)


# ---------- 交印 ----------


@app.post("/api/volumes/{volume_name}/print", summary="交印：读取当时启用稿的完整用字")
async def print_volume(
    volume_name: str,
    x_fail_commit: str | None = Header(default=None, alias="X-Fail-Commit"),
):
    fail = (x_fail_commit or "").lower() in ("1", "true", "yes")
    return service.print_volume(volume_name, fail_commit=fail)


@app.get(
    "/api/volumes/{volume_name}/print/download",
    summary="下载当前启用稿完整用字 CSV（带 BOM，Excel 直开）",
)
def print_download(volume_name: str):
    payload = service.print_volume(volume_name)
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow([
        f"交印单 {payload['job_id']}（册：{payload['volume']}，"
        f"启用稿次：{payload['active_draft_no']}，交印时间：{payload['printed_at']}）"
    ])
    w.writerow(["外字号", "字"])
    for item in payload["items"]:
        w.writerow([item["code"], item["glyph_char"]])
    filename = f"print-{quote(volume_name)}-draft{payload['active_draft_no']}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=print-draft{payload['active_draft_no']}.csv; "
                f"filename*=UTF-8''{filename}"
            )
        },
    )


# ---------- 状态与清空 ----------


@app.get("/api/state", summary="查看各册当前启用稿次、各稿待补情况与启用稿完整用字")
def state():
    return service.get_state()


@app.post("/api/reset", summary="演示清空：清除所有外字稿与原用字表")
def reset():
    service.reset_all()
    return {"status": "reset"}


# ---------- 网页与样例 ----------


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/samples", StaticFiles(directory=str(SAMPLES_DIR)), name="samples")
