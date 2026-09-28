# 进度校核（Progress Reconciliation）设计稿 v1

> 目标：**让「今天做了多少」这个数字只有一个出口，且必须来自线上。**
> 状态：待用户确认后实施。作者：DSH 会话（2026-09-28）

## 1. 问题（用今天的数据说清）

今天 `publish_drafts` 台账 12 条：7 条 `done`、5 条 `failed`。
而线上实际的今日发布数是 **10**（创作中心 `paging.totals`）——**那 5 条「失败」里有 3 条其实发布成功了**。

| 时间 | 本地记 | 线上事实 |
|---|---|---|
| 09:15 / 10:53 / 12:44 / 14:38 / 16:04 / 17:30 / 20:30 | done ×7 | 已发布 |
| 18:31 / 18:53 / 19:15 | **failed** | **其实已发布** |
| 21:33 / 21:54 | **failed** | **其实已发布** |

根因：配额用「台账里 `status==done` 的 units」当已完成量。
**误报的失败会把已发布的内容从计数里漏掉** → 规划器以为还差 3 篇 → 再排 3 篇 → 排到时间轴外。

这不是"数据脏了要清洗"，而是**计数口径放错了层**：拿"我们的自述"当"事实"。

## 2. 核心原则（对应你的三点要求）

| 你的要求 | 本设计的落点 |
|---|---|
| **数据真实** | 计数**只认线上**；本地台账降级为「审计日志」，不参与计数 |
| **规范可维护** | 新增 2 个小模块，各≈100 行，职责单一；判定逻辑做成纯函数（可单测） |
| **避免错误耦合** | 依赖方向单向：`automation → core ← webui`；planner 不认识网页，网页不认识台账 |

## 3. 数据源（已真机确认为正式接口，不用抠 DOM）

| 用途 | 接口 | 关键字段 |
|---|---|---|
| 今日已发布篇数 | `GET /api/v4/creators/creations/v2/answer?start=<今日0点>&end=<now>&limit=N&offset=0&sort_type=created` | `paging.totals` = 今日发布数；`data[].data.{id,created_time,title,question_id}` |
| 待发草稿数 | `GET /api/v4/answer-drafts/count` | `count` |

- 时间区间由接口自己过滤，**不用解析「2 小时前」这种相对时间**；
- 两条都是只读、轻量（各 ~1 次请求，实测 <1 秒返回）；
- qid 可用于"这篇是不是已经发过了"的去重判断。

## 4. 模块划分（2 新增 + 3 处小改）

```
core/progress.py        纯逻辑，不碰浏览器、不认识 automation
    ProgressSnapshot    不可变数据：day / published_today / drafts_pending / at
    parse_created(payload)      接口 JSON → [PublishedItem]（只挑需要的字段）
    normalize(...)              原始 dict → ProgressSnapshot
    merge_counts(ledger_done, snapshot)   → 计数只在这里合并
    save(snapshot) / load(day)   落 data/state/progress.json

webui/site_progress.py  编排：读页面 → 交给 core → 落盘 → 记差异
    refresh_progress(browser, force=False) -> ProgressSnapshot | None
    load_progress(day) -> ProgressSnapshot | None     （纯读，不碰浏览器）

webui/browser_progress.py  只有 DOM/接口原语（JS 常量 + 一次调用）
    read_progress_raw(browser) -> {"published": {...}, "drafts": {...}} | None
```

**改动点（都很小）**

1. `automation/executor.py`：每个 handler **浏览器已开时**调一次 `refresh_progress(b)`；
   发布类作业失败后再调一次 `refresh_progress(b, force=True)`。
2. `automation/store.py::done_counts`：`publish_drafts` 取
   `max(台账 units, snapshot.published_today)`；快照过期/缺失时退回台账并记一条 warning。
   → **planner / scheduler 一行不动**，它们只认计数出口。
3. `webui` 时间轴：显示「进度校核：线上今日发布 N 篇 · 草稿 M 篇（时间）」这一条事实。

## 5. 依赖方向（防错误耦合）

```
apps.zhihu_story.browser_*  ──┐
                              ├─→ webui/site_progress.py ──→ core/progress.py（唯一计数真源）
automation/executor.py ───────┘                                     ↑
                                                                    │ 只读
                                            automation/store.py ────┘
                                            automation/planner.py（不感知线上，也不感知台账细节）
```

- `core/*` 不 import `webui/automation`；
- `webui/*` 不 import `automation/*`；
- `automation/*` 只 import `core/*`（+ 既有例外：executor 调用 webui 跑任务）。

## 6. 成本（为什么不用"定时轮询"）

成本大头是**拉起浏览器**（~1.6s + 会话），不是读接口（<1s）。
所以校核**搭车**在已经开着的浏览器上：

| 时机 | 额外成本 |
|---|---|
| 每个自动化任务开始（浏览器已开） | 2 次只读请求，~1-2 秒 |
| 发布类作业失败后（浏览器仍开着） | 同上（且能当场把误报的失败判成成功） |
| 开机首次任务 / 日切 | 同上（无额外浏览器启动） |
| ~~每小时定时校核~~ | **不做**：要额外拉浏览器，还会和任务抢 profile 锁 |

## 7. 边界与失败处理

- 读不到（未登录/接口改版/超时）→ 返回 `None`，计数**退回台账**并记 warning，
  **绝不用猜的数字覆盖**；
- 快照带 `day` 字段：**跨天自动失效**，不会拿昨天的数字算今天；
- 拿不到浏览器时不同步（不为了校核单独拉一个）；
- 同一个错误只记一次差异（`reconcile.jsonl` 去重），避免日志噪音。

## 8. 数据分层（避免"为对账抹掉历史"）

| 层 | 内容 | 用途 |
|---|---|---|
| 事实层 | `data/state/progress.json`（线上快照） | **计数只用它** |
| 动作层 | `ledger.jsonl`（台账） | 审计"我下过什么指令、结果如何" |
| 差异层 | `reconcile.jsonl`（校核发现的偏差） | 回看"哪几次是误判"，不改写动作层 |

## 9. 验收标准

1. 单测：`parse_created` / `merge_counts` / 跨天失效 / 读不到时退回台账（纯函数，不碰浏览器）；
2. 真机：跑一次校核，`published_today` 必须等于创作中心的数字（今天应为 10）；
3. 端到端：把今天这 3 条误报失败在**差异层**标出来，界面数字与账号一致；
4. 全量 `tests/run_all.py` 通过。

## 10. 不做（本次范围外）

- 点赞/评论/收藏/关注的抓取（复用同一挂车点，后续加字段即可，`ProgressSnapshot` 预留 `extra`）；
- 改写历史台账（只记差异，不抹历史）；
- 任何定时轮询。
