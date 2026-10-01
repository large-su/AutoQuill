# Release 说明写作规范

| 项目 | 内容 |
|---|---|
| **适用版本** | v5.0.4 |
| **最后更新** | 2026-10-01 |
| **状态** | 活跃 |
| **说明** | Release 说明与 CHANGELOG 的写作规范、发布流程 |

本文件规定 GitHub Release（tag 说明）与 `CHANGELOG.md` 的写作方式。

## 1. 原则

发布说明是**给使用者看的变更记录**，不是开发对话。三条硬性要求：

1. **只陈述改了什么**。不写「你反馈了…」「这是我的错」「希望你测试…」。
   责任归属、排查过程、心路历程都不属于发布说明。
2. **可扫读**。每条一行，先说结论再说细节。使用者应该能在 10 秒内知道
   这个版本值不值得升。
3. **可核对**。涉及行为改变的条目，写清改前/改后的差异；涉及缺陷的，
   写清症状与触发条件，便于使用者判断自己是否受影响。

## 2. 章节顺序

固定使用以下章节，**存在才写，没有就省略**（不要写「无」占位）：

| 章节 | 内容 |
|---|---|
| `## Added` | 新功能 |
| `## Changed` | 行为变更、界面与配置调整、不兼容变更 |
| `## Fixed` | 缺陷修复 |
| `## Security` | 安全相关（有则优先置顶） |
| `## Notes` | 升级注意、平台支持、数据兼容 |
| `## Verification` | 测试与校验结果（表格） |

## 3. 格式模板

````markdown
# v5.0.4

一句话说明这个版本的性质（常规迭代 / 紧急修复 / 校验版）。

## Fixed

- **一句话结论。** 补充症状、触发条件与影响范围。涉及具体文件或接口时写出路径。
- 第二条……

## Added

- 新增内容。

## Notes

- 平台支持：Windows 10/11（x64）。
- 覆盖安装，用户数据（`%APPDATA%\AutoQuill`）不受影响。

## Verification

| 项目 | 结果 |
|---|---|
| 单元测试 | 978 passed, 2 skipped |
| CI | passed |
| SHA256 一致性 | 安装包 / `.sha256` / GitHub API digest 三处一致 |
````

## 4. 用词规范

**要这样写**：

- 「修复 X。原因是 Y，现改为 Z。」
- 「`/api/update/apply` 使用了不存在的方法名 `stage.file()`（正确为
  `stage.stage_file()`），接口抛出 `AttributeError`。」
- 「单元测试 978 passed, 2 skipped。」

**不要这样写**：

- 「你反馈的问题终于修好了」——不写对话对象。
- 「我犯了个低级错误」——不写自责，只写事实。
- 「这个问题排查了很久」——排查过程留给提交信息与设计文档。
- 「应该没问题了」「大概能用」——不确定的事不写；写清验证到了哪一步。

## 5. 标题规范

- Release 标题：`AutoQuill v5.0.4`
- Release 正文首行：`# v5.0.4`
- tag 名：`v5.0.4`（与 `core/version.py` 严格一致）

## 6. CHANGELOG 与 Release 的关系

| 文件 | 读者 | 详略 |
|---|---|---|
| `CHANGELOG.md` | 开发者、维护者 | 完整，含涉及的文件与接口路径 |
| Release 正文 | 使用者 | 精简，含升级注意与验证结果 |

两者**章节结构一致**，Release 正文由对应版本的 CHANGELOG 条目精简而来。
CHANGELOG 遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## 7. 发布流程

```bash
# 1) 同步版本号（CHANGELOG、README 一并改）
#    core/version.py 是唯一事实来源
# 2) 写 release/release_notes_<VERSION>.md
# 3) 一键发版（测试 → 提交 → 打包 → tag → 推送 → Release → 校验）
.venv/Scripts/python tools/release.py \
    --notes release/release_notes_5.0.4.md \
    --message-file /path/to/commit-message.txt
```

`tools/release.py` 会校验 sha256 三源一致，不一致直接失败。
发布前检查清单见 `docs/QA-PLAYBOOK.md`。
