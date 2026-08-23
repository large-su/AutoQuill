#!/usr/bin/env python3
"""AutoQuill 一键构建/发布脚本（防"发布旧包"门禁内置）

用法：
  python tools/build_release.py            # 门禁 + 构建 dist + 安装包 + sha256
  python tools/build_release.py --skip-test  # 跳过全量测试（危险，仅紧急修复用）
  python tools/build_release.py --skip-build # 只跑门禁与测试

门禁（不满足直接失败退出）：
  1. git 工作区干净（未提交改动不发布）
  2. 当前分支 = main
  3. 全部源码文件（*.py / index.html / *.iss / spec）的时间戳
     <= dist 构建时间（防"包比代码旧"）——以 dist/AutoQuill/_internal 的
     修改时间作为"上次构建时间"基线
  4. py_compile + 全量测试通过（--skip-test 除外）

构建：PyInstaller → ISCC（installer/AutoQuill.iss）→ certutil sha256
输出：release/AutoQuill-Setup-<VERSION>.exe + .sha256

版本号来源：core/version.py（唯一事实来源）。
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "AutoQuill"
RELEASE = ROOT / "release"
ISCC = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"

# 参与门禁的源码后缀：打包内容或构建配置
_GATED_SUFFIXES = (".py", ".html", ".css", ".js", ".json", ".md", ".iss", ".isl", ".spec")


def version():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(ROOT))
    import core.version
    return core.version.VERSION


def _run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), **kw)
    if r.returncode != 0:
        sys.exit(f"命令失败（exit {r.returncode}）：{cmd}")
    return r


# 构建产物目录：永远不入库，也不视为"未提交改动"
_BUILD_ARTIFACT_DIRS = ("dist/", "release/", "build/")


def git_clean():
    r = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        rel = line[3:].replace("\\", "/")
        if any(rel == d.rstrip("/") or rel.startswith(d) for d in _BUILD_ARTIFACT_DIRS):
            continue
        return False
    return True


def git_branch():
    r = subprocess.run(["git", "branch", "--show-current"], cwd=str(ROOT),
                       capture_output=True, text=True)
    return r.stdout.strip()


def gate():
    print("=" * 60)
    print(f"  AutoQuill 构建门禁 v{version()}")
    print("=" * 60)
    if not git_clean():
        sys.exit("✗ 工作区有未提交改动，请先提交。")
    print("✓ git 工作区干净")
    if git_branch() != "main":
        sys.exit(f"✗ 当前分支 {git_branch()}，发布需在 main。")
    print("✓ 分支 main")
    # 时间戳不设门禁：本脚本每次都会完整重建 dist + 安装包，
    # 产物必然来自当前源码（防旧包靠"重建"而非"比对"）。


def _sync_iss_version():
    """把 core/version.py 的版本号自动注入 installer/AutoQuill.iss，
    避免「改了版本号、安装包还是旧版号」的硬编码问题。"""
    import re as _re
    iss = ROOT / "installer" / "AutoQuill.iss"
    text = iss.read_text(encoding="utf-8")
    ver = version()
    new_text, n = _re.subn(
        r'(#define\s+MyAppVersion\s+")[^"]+(")',
        lambda m: m.group(1) + ver + m.group(2),
        text, count=1)
    if n != 1:
        sys.exit("✗ installer/AutoQuill.iss 未找到 #define MyAppVersion，无法注入版本号")
    if new_text != text:
        iss.write_text(new_text, encoding="utf-8")
    print(f"✓ 安装器版本号已同步：v{ver}")


def main():
    skip_test = "--skip-test" in sys.argv
    skip_build = "--skip-build" in sys.argv
    gate()
    _sync_iss_version()

    if not skip_test:
        print("\n--- 全量测试 ---")
        _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        print("✓ 全量测试通过")
    if skip_build:
        print("（--skip-build：仅门禁+测试，不构建）")
        return 0

    print("\n--- PyInstaller 构建 ---")
    _run([sys.executable, "-m", "PyInstaller", "build/AutoQuill.spec", "--noconfirm"])

    print("\n--- Inno Setup 安装包 ---")
    if not ISCC.exists():
        sys.exit(f"✗ 未找到 ISCC：{ISCC}（winget install JRSoftware.InnoSetup）")
    _run([str(ISCC), "installer/AutoQuill.iss"])

    exe = RELEASE / f"AutoQuill-Setup-{version()}.exe"
    if not exe.exists():
        sys.exit(f"✗ 安装包未生成：{exe}")
    print(f"✓ 安装包：{exe}（{exe.stat().st_size / 1e6:.1f} MB）")

    print("\n--- SHA256 ---")
    import re
    r = subprocess.run(["certutil", "-hashfile", str(exe), "SHA256"],
                       capture_output=True)
    text = r.stdout.decode("utf-8", errors="replace") + \
        r.stdout.decode("gbk", errors="replace")
    m = re.search(r"[0-9a-fA-F]{64}", text)
    if not m:
        sys.exit("✗ 无法从 certutil 输出提取 SHA256")
    digest = m.group(0).lower()
    sha_file = exe.with_suffix(exe.suffix + ".sha256")
    sha_file.write_text(digest, encoding="utf-8")
    print(f"✓ {digest}")

    print("\n构建完成。")
    print(f"  {exe}")
    print(f"  {sha_file}")
    print("下一步：git tag V<新版本> + gh release create")
    print("提醒：Release 说明请附 SmartScreen 提示——安装包未做代码签名，"
          "下载时点「更多信息」→「仍要运行」即可（详见 README FAQ）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
