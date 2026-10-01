# 应用内更新与重启

| 项目 | 内容 |
|---|---|
| **适用版本** | v5.0.7 起 |
| **最后更新** | 2026-10-01 |
| **状态** | 活跃 |
| **说明** | 应用内更新、重启交接与本地安装验证 |

启动后静默检查一次更新，有新版时通过顶部按钮提醒。用户确认更新一次后，后台完成下载、校验、退出旧实例、静默覆盖原安装目录及启动目标版本，不再追加下载完成确认。顶部的“重启”按钮使用同一套进程交接。

## 修复依据

v5.0.4 的 VBScript 把 Win32_Process.Create 的第四个输出参数写成常量 0，随后忽略异常。未赋值的返回值被当成成功，接口又以 WMI 启动脚本写过日志作为就绪依据。结果是应用退出，但实际安装进程没有启动。

此外，旧状态接口会把任何 applying 状态立即改为失败；旧 PowerShell 路径未维护 stage.json，而且安装失败仍会启动旧版本。仅检查生成命令字符串的测试无法验证这些行为。

## 实际执行链路

1. core/updater.py 解析 Release、下载和校验 SHA-256。core/update_stage.py 持久化 stage.json。
2. webui/update_api.py 在安装前再次校验安装包，检查当前安装目录，将本次状态写入磁盘。
3. 更新目录中的独立 PowerShell 脚本通过 WMI 创建，避开启动器的 kill-on-close Job Object。VBS 保留完整命令引号，用正确的输出变量接收 PID，并检查实际异常和返回码。
4. 宿主校验安装包并持久化状态，创建并验证更新目录下的 installer-temp，再写入本次随机 token 的 ready JSON。只有实际宿主完成此握手，接口才请求应用退出。WMI 日志不代表就绪。
5. 宿主等待服务 PID 和启动器 PID 全部退出。超时或取消会失败并保留安装包，不强杀进程。
6. Inno Setup 使用 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL，明确指定 /DIR 原目录；安装器日志与宿主日志分开。仅安装器子进程的 TEMP/TMP 指向 installer-temp，宿主恢复原环境后再启动应用。
7. 安装返回 0 后，宿主检查 _internal/build_info.json 中的目标版本，再启动 AutoQuill.exe。新服务报告的实际运行版本、安装目录和新 PID 均正确后，才落盘 done 并删除安装包。

普通重启跳过安装环节，同样等待旧实例释放单实例锁，再启动并确认新实例。不会提前拉起一个被旧实例拦截的进程。

## 文件与状态

安装态更新目录：%APPDATA%\AutoQuill\data\update。AQ_DATA_DIR 可覆盖数据根。

- stage.json：阶段、目标版本、宿主 PID、操作类型、自动继续安装意图（auto_install）、错误和新启动器 PID。
- apply.log：交接、安装、启动与错误日志。
- apply.log.installer.log：Inno Setup 的安装日志。
- apply_host_<id>.ps1：当次宿主脚本，位于安装树之外，避免覆盖自身。
- ready_<token>.json：实际宿主就绪凭据，接口确认后删除。
- _internal/build_info.json：构建脚本生成的安装版本清单，随安装包覆盖。

状态为 idle → downloading → staged → applying → done；任何实质失败进入 failed。dry-run 校验与握手后回到 staged，不安装、不退出、不重启、不删除安装包。状态接口在活跃宿主运行时只读取进度，发现宿主中断才报告失败。

Windows PowerShell 5.1 的脚本使用 UTF-8 BOM；参数通过 UTF-8 JSON 编码传入，环境通过 PowerShell 设置，不经过 cmd.exe。新服务的 JSON 原始字节明确按 UTF-8 解码，支持中文、空格和单引号路径。

## 本地验证与构建

专项回归：

    .venv\Scripts\python.exe -m unittest tests.test_updater tests.test_update_host_integration tests.test_update_lifecycle

本地安装包构建（允许验证未提交的工作区，不会发布）：

    .venv\Scripts\python.exe tools\build_release.py --local --skip-browser

完整冻结版验证：

    .venv\Scripts\python.exe tools\verify_installed_update.py --report logs\installed-update-report.json

验证器使用隔离的数据目录、端口、安装目录和无注册表/快捷方式的私有 Inno 安装器，实际运行旧版冻结夹具、应用内更新、版本确认与普通重启，并检查用户数据哨兵保留。旧版夹具包含本次修复后的更新器，用于验证未来升级链路。它不修改用户的安装目录，也不需要 GitHub 发布。

CI 同时运行 Linux 和 Windows；Windows 执行真实 PowerShell/WMI 回归，Linux 保留平台无关测试。

## 从旧版切换

旧版自身的更新交接已经损坏，无法依靠尚未安装的新代码修复自己。首次切换到 v5.0.5 需要运行修复版安装包并选择原目录；用户数据仍在独立的数据目录。之后使用修复后的应用内更新链路。正式 Release 仍按现有发布流程，在验证完成后独立发布。

`tools/build_release.py` 保留按需生成 `Install-AutoQuill-<版本>.cmd` 的本地诊断能力，供默认系统 Temp 拒绝创建安装临时目录时排查；普通构建和 Release 不生成或上传该 helper，也不提供安装 ZIP。辅助入口通过 setlocal 仅为本次安装设置同目录 installer-temp，并保存安装日志。
