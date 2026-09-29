#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发版：测试 → 提交 → 打包 → 打 tag → 推送 → 建 Release → 校验 sha256。

为什么要有它（用户 2026-09-29 反馈「最终测试和发布太慢」）：
  原来一次发版要我在会话里手动编排十来条命令，其中
    · 全量测试跑两遍（我先跑一遍，build_release.py 里又跑一遍）；
    · 提交信息含中文/引号，用命令行传会被 PowerShell 打断，只能写临时文件；
    · 打 tag、推送、建 Release、回下载校验各一条命令，还经常漏步。
  现在收敛成一条命令，并且**测试只跑一次**（结果传给构建脚本复用）。

用法：
  python tools/release.py --notes release/release_notes_4.9.17.md -m "v4.9.17: ..."
  python tools/release.py --notes ... -m "..." --skip-test   # 紧急情况（自己先跑过）

退出码非 0 表示中途失败（哪一步失败会打印得很清楚，不会留下半成品 tag）。
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "large-su/AutoQuill"


def run(cmd, **kw):
    """跑一条命令；失败直接退出（带上下文），不吞错误。"""
    printable = " ".join(str(c) for c in cmd)
    print("$ %s" % printable)
    r = subprocess.run([str(c) for c in cmd], cwd=str(ROOT), **kw)
    if r.returncode != 0:
        sys.exit("✗ 命令失败（exit %d）：%s" % (r.returncode, printable))
    return r


def out(cmd):
    r = subprocess.run([str(c) for c in cmd], cwd=str(ROOT),
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def version():
    sys.path.insert(0, str(ROOT))
    import core.version
    return core.version.VERSION


def step(msg):
    print("\n" + "=" * 62)
    print("  " + msg)
    print("=" * 62)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", required=True, help="Release 说明文件（markdown）")
    ap.add_argument("-m", "--message", default="", help="提交信息（可多行）")
    ap.add_argument("--message-file", default="",
                    help="从文件读提交信息（**推荐**）：中文经命令行传输会被控制台"
                         "编码转成乱码，真机上踩到过提交记录变乱码")
    ap.add_argument("--skip-test", action="store_true",
                    help="跳过全量测试（仅在你已单独跑过时用）")
    ap.add_argument("--skip-build", action="store_true",
                    help="只提交/推送，不打包（用于纯文档改动）")
    args = ap.parse_args()

    # ★ 中文提交信息一律按 UTF-8 读文件，绕开控制台编码（PowerShell 管道会转码）
    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8")
    else:
        message = args.message
    if not message.strip():
        sys.exit("✗ 提交信息为空：用 --message-file（推荐）或 -m 给一段说明")

    ver = version()
    tag = "v" + ver
    notes = ROOT / args.notes
    if not notes.exists():
        sys.exit("✗ 发布说明不存在：%s" % notes)
    exe = ROOT / "release" / ("AutoQuill-Setup-%s.exe" % ver)
    if not args.skip_build and not (ROOT / "dist").exists():
        print("（dist 不存在，稍后会由构建脚本生成）")

    # ① 全量测试（**只跑这一次**，构建脚本用 --skip-test 复用结果）
    if not args.skip_test:
        step("① 全量测试")
        t0 = time.time()
        run([sys.executable, str(ROOT / "tests" / "run_all.py")])
        print("✓ 测试通过（%.0fs）" % (time.time() - t0))

    # ② 提交（先提交再构建：构建门禁要求工作区干净）
    step("② 提交代码")
    run(["git", "add", "-A"])
    dirty = out(["git", "status", "--porcelain"])
    if dirty:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(message)
            msgfile = f.name
        try:
            run(["git", "-c", "user.name=AutoQuill",
                 "-c", "user.email=autoquill@local",
                 "commit", "-q", "-F", msgfile])
        finally:
            os.unlink(msgfile)
        print("✓ 已提交")
    else:
        print("（无改动，跳过提交）")

    # ③ 构建（安装包 + sha256）。构建脚本会回写 iss 版本号，再补一次提交。
    if args.skip_build:
        step("③ 跳过构建（--skip-build）")
    else:
        step("③ 打包（PyInstaller + Inno Setup）")
        t0 = time.time()
        run([sys.executable, str(ROOT / "tools" / "build_release.py"),
             "--skip-test"])
        print("✓ 打包完成（%.0fs）" % (time.time() - t0))
        run(["git", "add", "-A"])
        if out(["git", "status", "--porcelain"]):
            run(["git", "-c", "user.name=AutoQuill",
                 "-c", "user.email=autoquill@local",
                 "commit", "-q", "-m", "chore: 安装器版本号同步 %s" % tag])

    if not exe.exists():
        sys.exit("✗ 安装包不存在：%s" % exe)

    # ④ 打 tag + 推送
    step("④ 打 tag 并推送")
    if out(["git", "tag", "-l", tag]):
        run(["git", "tag", "-d", tag])
    run(["git", "tag", tag])
    run(["git", "push", "origin", "main"])
    run(["git", "push", "origin", tag])

    # ⑤ 建 Release（两个资产）
    step("⑤ 创建 GitHub Release")
    existing = out(["gh", "release", "view", tag, "--json", "tagName"])
    if existing:
        run(["gh", "release", "delete", tag, "--yes"])
    run(["gh", "release", "create", tag,
         "--title", "AutoQuill %s" % tag,
         "--notes-file", str(notes),
         str(exe), str(exe) + ".sha256"])
    print("✓ Release：https://github.com/%s/releases/tag/%s" % (REPO, tag))

    # ⑥ 回下载校验（三源一致才敢说"能一键更新"）
    step("⑥ 回下载校验 sha256")
    tmp = Path(tempfile.mkdtemp(prefix="aq_rel_"))
    try:
        run(["gh", "release", "download", tag, "--dir", str(tmp),
             "--pattern", "*.exe"])
        got = _sha256(tmp / exe.name)
        want = (exe.with_suffix(exe.suffix + ".sha256")
                .read_text(encoding="utf-8").strip())
        api = out(["gh", "api", "repos/%s/releases/latest" % REPO,
                   "--jq", '.assets[] | select(.name | endswith(".exe")) | .digest'])
        api = api.replace("sha256:", "").strip()
        print("下载包 : %s" % got)
        print("侧车   : %s" % want)
        print("API    : %s" % api)
        if not (got == want == api):
            sys.exit("✗ 三源 sha256 不一致——一键更新会被拒绝，请检查")
        print("✓ 三源一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    step("完成")
    print("  %s" % exe)
    print("  https://github.com/%s/releases/tag/%s" % (REPO, tag))
    print("  （CI 在后台跑，稍后 `gh run list` 看结果）")
    return 0


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
