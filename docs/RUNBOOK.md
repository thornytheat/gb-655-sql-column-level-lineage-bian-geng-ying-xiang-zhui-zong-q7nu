# 运行手册 lineageguard

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18214/api/health
```

宿主端口 `18214`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18214/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `lineageguard-data` 挂着 `/data/lineageguard.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| POST | `/api/handoffs` | 为当前或指定批次建交接单（同批次只建一张） |
| GET | `/api/handoffs` | 交接单列表（含状态计数） |
| GET | `/api/handoffs/{id}` | 交接单详情：任务、凭据、回执、冲突、回退进度 |
| GET | `/api/handoffs/{id}/manifest.json` | 领取凭据绑定的清单原文与摘要 |
| GET | `/api/handoffs/{id}/todos` | 写账中断留下的待办 |
| POST | `/api/handoffs/{id}/recall` | 撤回整单，返回逐项取消 / 待回执 / 待补偿 |
| POST | `/api/handoffs/{id}/tasks/{tid}/claim` | 领取任务或补偿任务，签发绑定清单的凭据 |
| POST | `/api/handoffs/{id}/tasks/{tid}/receipt` | 回执：`executed` / `not_executed` 或 `done` / `failed` |

## 6 执行交接

一次交接单对应一个真实批次。逐条任务由批次的影响面展开，一个下游节点一条任务；
任务结构直接回读批次里已有的引用解析结果，不重新判定血缘，也不扩展别名链。

状态推进只有下面几条边：

| 动作 | 前置状态 | 后置状态 |
|---|---|---|
| 领取（execute） | 待领 | 在途 |
| 回执 `executed` | 在途 | 已执行 |
| 回执 `not_executed` | 在途 | 待领（可再领） |
| 撤回 | 待领 | 已取消 |
| 撤回 | 在途 | 停在途，等回执 |
| 撤回 | 已执行 | 待补偿 |
| 回执 `executed`（撤回后到达） | 在途 | 待补偿 |
| 回执 `not_executed`（撤回后到达） | 在途 | 已取消 |
| 领取（compensate） | 待补偿 | 补偿在途 |
| 回执 `done` | 补偿在途 | 完成 |
| 回执 `failed` | 补偿在途 | 待补偿（可再领） |

- 只有撤回后每个任务要么已取消、要么补偿回执 `done`，才算回退完成；
  已执行项在补偿确认前一直计入未结算，不会因为整单撤销而虚报回退。
- 领取与撤回都带 `expected_revision`，版本落后即 409 `HANDOFF_STALE`。
- 凭据绑定任务清单摘要、用途、签发版本与到期时间；回执必须带凭据。
- 过期凭据的回执记为冲突（`CREDENTIAL_EXPIRED`），不推进状态、不抵销补偿。
- 带 `idempotency_key` 的重试取回首次结果，不重复派发、不重复记账；
  同键改作他用返回 409 `HANDOFF_CONFLICT`。
- 每次写账先落 `handoff_journal` 再比较交换 `revision`；读到待办时按日志补完，
  已落地的不重放，中断未落地的一次性补写。

## 7 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 409 `HANDOFF_STALE` | 领取或撤回带的交接版本落后 | 重读 `/api/handoffs/{id}` 的 `revision` 再重试 |
| 409 `HANDOFF_CONFLICT` | 状态不允许该动作、重复领取、重复撤回 | 看返回 `detail.reason` |
| 409 `HANDOFF_RECALLED` | 整单已撤回仍要领取待领任务 | 已取消的项不再派发 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |

## 8 端口与并发

本服务固定 18214。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `lineageguard-data` 卷名，避免与本机其他项目抢端口、串数据。
