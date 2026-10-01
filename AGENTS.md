# AutoQuill Agent 入口

- **提交、打包、tag 或 Release**：先读 [发布规范](docs/RELEASE-GUIDE.md)，通过 `tools/release.py` 执行。发布提交以版本号开头，资产只有 EXE 与 SHA256。
- **文档与目录调整**：读 [贡献规范](CONTRIBUTING.md) 和 [目录职责](docs/REPOSITORY-GUIDE.md)。当前说明保留在 `docs/`，历史复盘在 `docs/archive/`。
- **功能开发**：按需查 [架构文档](docs/DEVELOPER.md)；测试范围按 [QA 手册](docs/QA-PLAYBOOK.md) 选择。普通小改执行相关测试一次。
- **CI 失败**：查看失败的 job、attempt 与日志中的最终异常；以提交 SHA 关联修复和重跑，区分测试问题与运行时代码问题。
- **源码清理**：核对 import、动态加载、CLI、测试和打包引用。用户配置、登录态和业务台账留在本机，运行数据通过 `.gitignore` 排除。

在仓库根目录使用 `.venv/Scripts/python.exe`；中文输出设 `PYTHONIOENCODING=utf-8`。版本以 `core/version.py` 为准。

非平凡任务中的明确子任务可交给成本较低的 Agent；优先使用 Luna 做发现、机械编辑和专项验证，不确定时升级 Terra。最多两个子 Agent 并发，分配互不重叠的文件范围。主 Agent 负责方案、外部操作和最终审核。
