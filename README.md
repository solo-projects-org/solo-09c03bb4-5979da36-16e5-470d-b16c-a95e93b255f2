# 期刊补缺核对 API

给一卷旧期刊补缺前，把 **有藏 / 缺刊 / 待翻册** 分清楚。FastAPI + SQLite，卷清单最多 24 期，按条码成批增补或更正馆藏，每册最多三组候选期次（用于封面模糊的合刊）。求解器只为**争用期次**（出现在至少两册候选中的期次）分配互斥状态，争用期数上限可配置，适合小内存主机。

## 快速启动

```bash
docker compose up --build
```

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- 健康检查: <http://localhost:8000/healthz>（含 `max_contended_issues` 现行上限）
- 数据卷：命名卷 `periodical-data`，SQLite 文件在容器的 `/data/periodicals.sqlite3`
- 争用期数上限：环境变量 `MAX_CONTENDED_ISSUES`，默认 `24`；启动时可用
  `MAX_CONTENDED_ISSUES=12 docker compose up --build` 调低（compose 透传该变量）。

本地开发（无需 Docker）：

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
PERIODICALS_DB=./data/periodicals.sqlite3 uvicorn app.main:app --reload
python3 scripts/demo.py            # 或：API_BASE=http://127.0.0.1:8000 python3 scripts/demo.py
pytest                             # 220 项测试
```

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 一卷最多 24 期 | 建卷 `issues` 长度校验（1–24），求解器硬上限 `MAX_ISSUES=24` |
| 每册最多三组候选期次 | 请求校验 `candidates` 长度 ≤ 3；库中 `candidate_groups.position` 只允许 0–2 |
| 每组内期次一并计入 | 候选组是期次位掩码，求解时整组占用 |
| 每册必须选定一组 | 求解要求每册恰好选一个候选组，不能跳过 |
| 同一期最多归一册 | 所有被选候选组两两不相交 |
| 只为争用期次分配互斥状态 | 出现在 **≥2 册**候选中的期次才进入 DP；其余期次不占状态位 |
| 争用期数上限可配置，默认 24 | `MAX_CONTENDED_ISSUES`；超限抛 `ContentionLimitExceeded`，API 返回 **507** |
| 两组占用同样争用期次 | 紧凑争用掩码相同也分别保留，各自对其他（非争用）期次的覆盖独立计入 |
| 有藏 / 缺刊 / 待翻册 | 对**全部合法方案**判定：都覆盖→`held`；都未覆盖→`missing`；其余→`review` |
| 整批核对 | 每批与库中其余馆藏合并后统一求解，不是逐条判断 |
| 查卷/查缺刊检查现行馆藏 | `GET /volumes/{id}`、`GET /volumes/{id}/gaps` 都对当前馆藏跑求解，超限即 507 |
| 试算与提交检查批次前后两份馆藏 | `before`（现行馆藏）与 `after`（合并批次后）各跑一次求解，任一超限即 507 |
| 超限只报资源不足 | 507 `contention_limit_exceeded`，**不写馆藏、不写批次回执**（连 rejected 记录也不写），事务回滚 |
| 无解整批退回 | 求解失败（且未超限）时不写任何 `copies/candidate_*`，只写一条 `rejected` 批次记录；原馆藏与缺刊结论不变 |
| 并发以实际入库顺序为准 | `BEGIN IMMEDIATE` 先取写锁再读馆藏，提交串行化，`batches.seq` 即真实提交顺序 |
| 原馆藏和缺刊结论仍可查 | 退批次保留 `reason`、请求快照、提交前后对照；`GET /volumes/{id}/gaps` 始终可查 |

### 状态空间是怎样缩小的

互斥冲突只会发生在被多册同时觊觎的期次上。设一卷有 `n` 期、其中争用期次有 `c` 期：

1. 每个候选组投影成一个 `c` 位的**紧凑争用掩码**；非争用期次不进状态。
2. 可达性 DP 只在最多 `2**c` 个紧凑状态上推进（而非 `2**n`）。互不相交约束只涉及争用位，因此可满足性判定与全量 DP 完全等价。
3. 每期的有藏/缺刊/待翻册，用同一套紧凑 DP 回答两个精确的可满足性问题：是否存在覆盖该期的合法方案、是否存在**不**覆盖该期的合法方案。两个候选组争用掩码相同时，它们对非争用期次的覆盖仍分别计入，不会被合并。
4. 稀疏馆藏（多数期次只有一册提到）下 `c` 远小于 `n`，例如 24 期卷只有 1 个争用期次时状态空间只有 2；上限由 `MAX_CONTENDED_ISSUES` 兜底，超限在分配任何 `2**c` 位集之前就拒绝。

正确性由 1900+ 组随机用例与暴力全枚举对拍（`tests/test_solver.py`），覆盖可满足/无解、赋值合法性、争用掩码收缩与上限行为。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/volumes` | 建卷：`title`、`volume_label`、`issues`（1–24 个有序期次名） |
| GET | `/volumes/{id}` | 卷清单、当前馆藏、实时缺刊报告 |
| GET | `/volumes/{id}/gaps` | 仅缺刊报告：`held` / `missing` / `review` + 中文标签 |
| POST | `/volumes/{id}/batches` | 提交一批增补/更正；`preview=true` 只试算不落库 |
| GET | `/volumes/{id}/batches` | 批次审计，按实际提交顺序 |
| GET | `/volumes/{id}/batches/{batch_id}` | 单批详情（含提交前后对照与变化明细） |

批次请求体：

```json
{
  "items": [
    {"barcode": "BC-001", "action": "add",    "candidates": [["第1期", "第2期"], ["第3期", "第4期"]]},
    {"barcode": "BC-009", "action": "update", "candidates": [["第3期"]]}
  ],
  "preview": false
}
```

`action=add` 要求条码在该卷尚不存在；`action=update` 用新候选组整体替换旧候选组（可借此更正旧的模糊判断）。同一批内条码不可重复，条码全局唯一（不能同时属于两卷）。

无解时返回 **201**，响应体 `status="rejected"` 并给出 `reason`；请求本身的格式/引用错误返回 4xx（`unknown_issue`、`too_many_candidates`、`barcode_exists` 等）。

争用期数超限时返回 **507**，错误体 `error.code="contention_limit_exceeded"`，`error.details` 给出 `phase`（`current_holdings` / `batch_preview_after` / `batch_submit_after`）、`contended_count`、`max_contended`、`state_space_size` 与争用期次名。该错误**不产生任何批次回执**，馆藏与已有审计记录保持不变。

## Swagger 演示（低上限）：核对一卷 24 期旧刊，再试一批超限补录

先用低上限启动，模拟小内存主机：

```bash
MAX_CONTENDED_ISSUES=3 docker compose up --build
# 本地：MAX_CONTENDED_ISSUES=3 PERIODICALS_DB=./data/demo.sqlite3 \
#       uvicorn app.main:app --reload
```

打开 <http://localhost:8000/docs>，依次：

1. `GET /healthz` → 确认 `max_contended_issues=3`。
2. `POST /volumes` 建 24 期卷，`issues` 为 `["第1期", …, "第24期"]`。
3. `POST /volumes/{id}/batches` 补一批互不争用的单期册（各占不同期）→ `committed`，响应里 `reconciliation.after.contended_count=0`、`state_space_size=1`（24 期卷却只用 1 个互斥状态）。
4. 再补三册让第 9–11 期两两出现在多册候选中，争用期数正好到 3 → 仍 `committed`，`state_space_size=8`。
5. 试算一批让**两个新册**同时觊觎第 12 期（及第 9/10 期），`preview=true` → **507** `contention_limit_exceeded`，`details.contended_count=4 > max_contended=3`，`phase="batch_preview_after"`；不落库。
6. 去掉 `preview` 同样提交 → 仍 **507**，`phase="batch_submit_after"`；随后 `GET /volumes/{id}/batches` 回执条数不变、`GET /volumes/{id}` 里没有这两个新条码，`GET /gaps` 仍可查（现行馆藏停在 3 个争用期）。
7. 作为对照，再提交一批与现行馆藏冲突但**不超限**的补录 → 仍是 201 `rejected` 并写回执，说明「无解退回」与「资源不足拒绝」是两条独立路径。

## Swagger 演示：新册排除旧候选后，缺刊结论怎样变化

1. `POST /volumes` 建 4 期卷（`["第1期","第2期","第3期","第4期"]`）。
2. `POST /volumes/{id}/batches` 加合刊册 `BC-001`，候选组 `[["第1期","第2期"], ["第3期","第4期"]]` → 四期全是 **待翻册**。
3. `POST /volumes/{id}/batches` 加 `BC-002`，候选 `[["第3期"]]`，**`preview=true`** → `would_commit`，`reconciliation.after` 变成：第1–3期 **有藏**，第4期 **缺刊**。原因：BC-002 占了第3期，BC-001 只剩 `[1,2]` 一种合法选择，第4期不再被任何合法方案覆盖。
4. 去掉 `preview` 再提交一次 → 结论落库。
5. 再提交 `BC-003` 候选 `[["第1期","第2期"]]` → 与 BC-001 冲突，`status="rejected"`；`GET /gaps` 仍是「1–3 有藏、4 缺刊」，`GET /batches` 能看到这条退回记录。

`python3 scripts/demo.py` 会把上述流程连同并发提交一起跑一遍并打印结果。

## 目录

```
app/main.py      FastAPI 路由与 Swagger 文档
app/config.py    运行配置（MAX_CONTENDED_ISSUES 争用期数上限）
app/service.py   卷/批次校验、事务、审计、缺刊报告、507 资源不足映射
app/solver.py    紧凑争用掩码 DP 精确求解器（有藏/缺刊/待翻册 + 上限）
app/db.py        SQLite 连接、WAL、schema
app/schemas.py   Pydantic 请求模型
tests/           220 项测试（求解器暴力对拍 + API/事务/并发/上限）
scripts/demo.py  演示脚本
```

## 待办 / 已知限制

- **当前环境没有 Docker**（无 `docker`/`dockerd`，非 root 无法安装），`Dockerfile` 与 `docker-compose.yml` 已按标准写法交付但**未在本机实际构建**；服务本身用 `uvicorn` 直接启动、端到端冒烟与 220 项测试均通过。请在有 Docker 的机器上跑一次 `docker compose up --build` 确认镜像可构建。
- 每期判定最多触发若干次紧凑 DP（上限 `2**MAX_CONTENDED_ISSUES`）。默认 24 期稠密用例实测约 2 s；小内存主机把上限调低（如 12）后单次状态空间降至 2¹²，耗时与内存随之下降。
- SQLite 是单写者模型：并发提交会串行排队（`busy_timeout=30s`）。一期一馆的量级足够；若要多实例横向扩展需换 PostgreSQL 并保留同样的 `SERIALIZABLE`/行锁语义。
- 批次是「整批要么全提交、要么全退回」；暂不支持部分接受。若需部分接受，应先拆批再提交。
- 未做鉴权与操作人记录。生产部署前应加认证，并把 `batches` 增加 `operator` 字段。
- `preview` 在只读事务里执行并在结束时回滚，仍会短暂持有读锁；对写并发无影响。
- 缺刊报告只依据「候选组合法分配」，不校验物理册数上限（例如同一条码的复本）；如馆藏存在复本，需要先按册登记不同条码。
- `MAX_CONTENDED_ISSUES` 是进程级环境变量，调整后需重启服务；暂不支持按卷或按请求单独指定上限。
