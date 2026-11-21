# 古籍制版 · 外字号稿次管理

排版组与校勘组围绕同一分册的**外字**分别交表：排版组交《外字号清单》（每行一个外字号），
校勘组交《对照表》（每行外字号与对应的单字）。同一编号在不同稿里可能代表不同的字，
本服务按**册名 + 正整数稿次**分别接收两份 CSV，管理“待补 → 整稿启用 → 旧稿作废”的全过程，
交印时只读取当时启用稿的完整用字。

## 业务规则

1. 网页按册名和正整数稿次分别收两份 CSV：
   - 外字号清单：每行一个外字号（表头 `外字号`，兼容无表头）；
   - 对照表：每行 `外字号,字`，字必须是单字；同一稿内编号不重复、单字也不重复。
2. 两表可**先后到达**。只到一份时稿次为“待补”，响应与状态页明确提示缺哪份表、
   对照表还缺哪些编号。
3. **未启用的稿次可重传改表**，重传即整表替换。
4. 同稿次两份表齐全、清单里每个用到的编号都在对照表中**唯一对应一个字**，
   且稿次**高于当前启用稿次**时，整稿自动启用；旧启用稿标记为“已作废”。
5. 已启用稿次的原表不能重传（重传不重复启用，返回 409）；改表必须用更高的新稿次；
   迟到的低稿次不能换回当前启用稿。
6. 网页显示每册当前启用稿次；**交印只读取当时启用稿**的完整用字（按清单顺序）。
7. 写入失败时整笔事务回滚，原稿与待补记录保持原样；待补记录持久化在 SQLite，
   **重启后仍可接着补传**。
8. 原用字表上传只适用于**尚未启用外字稿**的分册（已有待补外字稿也会被拒）。
9. 演示清空（`POST /api/reset`）一并清除所有外字稿、对照表与原用字表。

## 目录

```
app/            FastAPI 应用（database/parsers/service/main/errors）
web/index.html  单页操作界面（两份表上传、原用字表、交印、状态总览）
samples/        可直接使用的样例 CSV
tests/          pytest 端到端测试（15 个用例）
Dockerfile / docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/
# SQLite 数据持久化在命名卷 glyph-data -> /data/glyph.db
```

页面上可直接下载 `/samples/` 下的样例文件。推荐演练顺序：
① 册甲稿次 1 先传清单（提示缺对照表）→ ② 传对照表（缺编号 W004，提示待补）→
③ 重传完整对照表（稿次 1 自动启用）→ ④ 传稿次 2 两表（稿次 2 启用、稿次 1 作废，
注意 W003 由“玄”改“宇”——同编号不同字）→ ⑤ 交印对比两稿用字。

## 本地运行（无 Docker 时）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 测试
pip install -r requirements-dev.txt
pytest -q
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/volumes/{册名}/drafts/{稿次}/list` | 上传外字号清单 CSV |
| POST | `/api/volumes/{册名}/drafts/{稿次}/mapping` | 上传对照表 CSV |
| POST | `/api/volumes/{册名}/legacy` | 原用字表（仅限未启用外字稿的分册） |
| POST | `/api/volumes/{册名}/print` | 交印：读取当前启用稿完整用字（JSON） |
| GET  | `/api/volumes/{册名}/print/download` | 下载交印 CSV（带 BOM，Excel 直开） |
| GET  | `/api/state` | 各册启用稿次、各稿待补情况、启用稿完整用字 |
| POST | `/api/reset` | 演示清空（清除所有外字稿） |
| GET  | `/docs` | Swagger 交互文档 |

上传响应关键字段：`activated`（本次是否触发整稿启用）、`active_draft_no`（当前启用稿次）、
`draft.status`（`pending` / `active` / `superseded`）、`draft.missing_tables`、
`draft.missing_codes`、`replaced`（是否重传替换了未启用稿的原表）、`message`（中文提示）。

## 数据模型（SQLite，WAL + 立即事务）

- `volumes`：分册。
- `drafts`：(册, 稿次) 主表，状态 `pending/active/superseded` 与两份表到达标记。
- `draft_list_codes`：清单外字号（保留文件顺序，去重）。
- `draft_mappings`：对照表 外字号→单字（编号、单字均唯一约束）。
- `legacy_tables`：原用字表（每册至多一份）。

所有写操作在 `BEGIN IMMEDIATE` 事务内完成，配合进程内互斥锁串行化；
提交前抛错即回滚，待补状态与已启用稿都不受影响。

## 做完了什么 / 验过什么 / 未完成

### 已完成

- FastAPI + SQLite 全量功能：两份 CSV 分稿次接收、错序到件、缺表/缺编号提示、
  未启用稿重传替换、条件满足自动整稿启用、高稿次作废旧稿、已启用稿与迟到旧稿锁定、
  交印读取启用稿完整用字（JSON + CSV 下载）、原用字表限制、演示清空、单页网页与样例。
- 写入失败整笔回滚（服务层测试钩子模拟）；待补记录落库，重启后续传。
- Dockerfile 与 docker-compose（命名卷持久化、健康检查）。

### 已验证（本环境实际运行）

- `pytest` 15 个用例全部通过，覆盖：两表先后/错序到件、缺编号与重复校验、
  待补稿重传替换、高稿次启用并作废（同编号 W003 改字）、已启用稿 409 锁定、
  迟到低稿不能换回、交印只读启用稿、写入失败回滚后补传成功、**重启后续传**、
  原用字表限制、演示清空、稿次正整数校验、GB18030/无表头/空文件兼容。
- 真实启动 uvicorn 用样例 CSV 冒烟（见下文运行记录）。

### 未完成 / 限制

- 本环境没有 Docker 守护进程，`docker compose up --build` 仅做了文件级准备，未实机构建；
  Dockerfile 基于 `python:3.11-slim` 与已验证的依赖版本，逻辑与本地运行一致。
- 进程内互斥锁只在单进程内生效；多副本部署依赖 SQLite 立即事务与忙等待兜底，
  建议按 compose 默认单副本 + 卷持久化运行。
- 没有账号/鉴权与操作审计（题目未要求）；`/api/reset` 仅适用于演示环境。
