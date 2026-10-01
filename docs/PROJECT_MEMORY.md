# AutoQuill 项目速览

| 项目 | 内容 |
|---|---|
| **适用版本** | v5.0.8 起 |
| **最后更新** | 2026-10-01 |
| **状态** | 活跃 |
| **说明** | 新会话定位入口、数据目录与权威文档 |

AutoQuill 是 Windows 上的知乎故事创作助手，包含生成、草稿、已发布内容、自动化和本地 Web 控制台。

## 按任务找文档

| 任务 | 权威入口 |
|---|---|
| 用户使用与安装 | [README](../README.md) |
| 版本与变更 | `core/version.py`、[CHANGELOG](../CHANGELOG.md) |
| 提交、构建与发布 | [发布规范](RELEASE-GUIDE.md) |
| 模块关系与开发 | [开发文档](DEVELOPER.md) |
| 测试选择与失败排查 | [QA 手册](QA-PLAYBOOK.md) |
| 文件归属与清理 | [目录职责](REPOSITORY-GUIDE.md) |
| 更新、安装与重启 | [更新设计](AUTO-UPDATE-DESIGN.md) |

## 运行入口与数据

- 源码控制台：`main.py --web`；独立窗口：`tools/launcher.py`。
- 端口来源：`core/ports.py`。API 在 `webui/`，自动化在 `automation/`，浏览器适配在 `applications/zhihu_story/` 和 `web_drivers/`。
- `core/paths.py` 决定程序目录和数据目录；安装版默认在 `%APPDATA%/AutoQuill` 存放用户数据，`AQ_DATA_DIR` 可覆盖。
- 登录态、草稿、生成记录与业务台账属于用户数据。源码清理和测试使用隔离目录。
- 旧工作记忆与旧工具环境经验位于 `docs/archive/`，用于历史查证。当前规则从仓库根 `AGENTS.md` 按任务进入，不要求每次加载全部文档。
