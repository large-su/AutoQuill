# 一键自动更新（In-app Update）设计稿 v1

| 项目 | 内容 |
|---|---|
| **适用版本** | v5.0.4 |
| **最后更新** | 2026-10-01 |
| **状态** | 活跃 |
| **说明** | 应用内一键更新的设计与实现依据（含换装进程必须脱离主进程树的原因） |

> 目标：GitHub 有新版本 → 软件内提示 → 点「立即更新」→ 自动下载、校验、换装、重启、清理。
> 状态：待确认后实施。作者：DSH 会话（2026-09-29）

## 1. 现状 vs 目标

| | 现在 | 目标 |
|---|---|---|
| 发现新版本 | 手动点「检查更新」 | 启动后自动检查一次 + 每 6 小时一次；发现即提示 |
| 下载 | 跳 GitHub 网页手动下 | 软件内下载（带进度） |
| 校验 | 无（用户自己比对 sha256） | 自动校验 SHA256，**不一致就中止** |
| 安装 | 手动双击、走向导 | 静默安装到**原安装目录** |
| 重启 | 用户手动开 | 自动重新拉起 |
| 清理 | 安装包留在下载目录 | 自动删除 |

## 2. 关键约束（决定了方案长什么样）

1. **运行中的 exe 不能被替换**——必须「先退出、再安装、后重启」，所以需要一个
   **脱离父进程的独立小进程**来干这件事（父进程一退，它的子进程不能跟着死）。
2. **安装目录可能不是默认值**（你机器上就是 `D:\AutoQuill`，而安装器默认是
   `%LOCALAPPDATA%\Programs\AutoQuill`）→ 换装时必须显式带
   `/DIR="<当前安装目录>"`，从 `sys.executable` 的父目录取。
3. **静默安装不需要管理员权限**（安装器是 `PrivilegesRequired=lowest` +
   用户目录），这一点比 Clash Verge 简单——它要装服务，得求 UAC。
4. **安装器会自己关掉正在运行的程序**（Inno 默认行为）。必须显式
   `CloseApplications=no`，把「什么时候关」的控制权留在我们手里，否则它可能在
   我们还没准备好时就动手。
5. **不能拿你的生产环境当试验场**（上次的教训）→ 提供 `--dry-run`：
   下载 + 校验 + 落盘全部照做，只**不执行安装、不重启**。

## 3. 模块划分（3 个新文件 + 2 处小改）

```
core/updater.py              纯逻辑：解析 release 资产名/校验和文件、版本比较、
                             sha256 计算、暂存目录约定（不碰 UI、不碰浏览器）
core/update_stage.py         落盘状态机：staged/verified/installing/done/failed
                             （JSON，写在 DATA_ROOT/update/，含已下载包与日志路径）
tools/apply_update.py        独立子进程：等父进程退出 → 静默安装 → 重启 → 清理
                             （用 python.exe 或 AutoQuill.exe --apply-update 运行）
webui/update_api.py          新增接口：/api/update/download、/api/update/apply、
                             /api/update/stage（进度与状态查询）
webui/api_setup.py           扩展现有 /api/update/check：补 asset_url/sha256_url/
                             notes（现在只有版本号与页面链接）
webui/static/*               设置页加「发现新版本」卡片：版本号、更新说明、
                             「立即更新」按钮、下载进度、失败原因
```

**依赖方向**：`core/*` 不认识 webui；`tools/apply_update.py` 只依赖 core/update_stage；
webui 只做编排与暴露接口。

## 4. 完整流程

```
① 检查（已有）      /api/update/check → has_update + latest
② 扩展检查          + 资产名/下载直链/sha256 直链/更新说明
③ 用户点「立即更新」  /api/update/download
                    · 后台线程下载到 DATA_ROOT/update/AutoQuill-Setup-<new>.exe.part
                    · 下完校验 SHA256（对比 release 里的 .sha256 资产）
                    · 通过 → 改名去掉 .part，状态 staged；不通过 → 删除并报错
④ 落盘状态           update/stage.json：{version, installer, sha256, at, stage}
                    + 同目录 update.log（子进程的日志）
⑤ 用户点「重启并安装」 /api/update/apply
                    · 启动分离子进程 apply_update.py（DETACHED_PROCESS）
                    · 立刻正常退出主程序
⑥ 子进程             · 轮询父 pid 直到消失（最多 60s，超时就用 taskkill /PID）
                    · 运行 <installer> /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
                        /NOCANCEL /DIR="<当前安装目录>" /LOG="<update.log>"
                    · 成功 → 启动 <安装目录>\AutoQuill.exe
                    · 删除安装包与 .part
                    · 写 stage.json: done / failed（附日志尾部 20 行）
⑦ 失败也能兜底        若第⑥步失败：下次启动时读到 failed → 界面提示
                    「上次自动更新未完成」+ 打开日志 / 手动安装包位置
```

## 5. 安全与边界

- **只从本仓库的 Release 下载**（`large-su/AutoQuill`），资产名严格匹配
  `AutoQuill-Setup-<version>.exe`；
- **SHA256 必须匹配**才允许执行；不匹配→删除+报错（不提供"仍然安装"）；
- 安装包暂存在 `DATA_ROOT/update/`（用户数据目录），**不写系统临时目录**；
- 更新过程中**绝不动用户数据**（安装器本身也不删 `%APPDATA%\AutoQuill`）；
- 网络失败/磁盘失败：状态机记录原因，下次启动能看见，可重试；
- **不自动安装**：永远由用户点一次确认（避免"我正在写文章时被换装"）。

## 6. 我能测什么、不能测什么（上次的教训）

| 能测（我会在源码态 + 独立数据目录做） | 不能测 |
|---|---|
| 资产名/校验和解析（含异常输入） | **换装真机替换**（会替换掉你的 `D:\AutoQuill`） |
| 下载 + SHA256 校验（真下 43MB） | 静默安装的实际界面行为 |
| 校验失败→中止+清理（可构造假包） | 安装器对"文件被占用"的处理 |
| 安装目录探测（含中文/空格路径） | |
| `--dry-run` 全流程（到"即将安装"为止） | |
| 子进程的等待逻辑（用假 pid 模拟） | |

真机替换那一步，我建议**第一次由你点**（你本来就要更新），确认成功后我再视为已验证。

## 7. 分阶段实施（建议）

- **P1（本次）**：`core/updater.py` + 暂存 + 下载 + 校验 + `/api/update/*` +
  设置页「立即更新（下载并暂存）」+ `apply` 接口 + `apply_update.py` + dry-run。
- **P2（下次）**：启动自动检查 + 顶部横幅提示（不打扰写文章）。
- **P3（可选）**：增量更新（只下差异）——**不建议**，收益小、风险大。

## 8. 工作量与风险

- 预计改动：新增 ~350 行、改 ~60 行；单测 ~20 例（纯逻辑为主）。
- 主要风险：**静默安装的真机行为**（唯一无法在源码态验证的部分）。
  缓解：`/LOG` 留全量日志 + 失败状态回读 + 安装包保留（失败时可手动双击）。
  ——「失败可手动兜底」是底线，绝不让更新把软件弄成打不开。
