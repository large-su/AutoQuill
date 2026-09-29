# -*- coding: utf-8 -*-
"""真机验证（P1 一键更新）：真下载 → 真校验 → 走一遍 apply（dry-run，不安装）。

★ 在**独立数据目录**里跑（AQ_DATA_DIR），不碰你正在用的 AutoQuill 数据。
★ dry-run：只到「校验通过、参数正确」为止，不执行安装、不重启、不删包。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--release-json", default="",
                    help="release JSON 文件（用 gh api 取；避开匿名 API 限流）")
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["AQ_DATA_DIR"] = args.data_dir
    from core import update_stage as stage, updater

    print("数据目录：%s" % updater.update_dir())

    # ① 取最新 release（优先用外部传进来的 JSON：匿名 API 会限流）
    if args.release_json:
        with open(args.release_json, encoding="utf-8") as f:
            payload = json.load(f)
        print("① 读取 release JSON：%s" % args.release_json)
    else:
        import requests
        r = requests.get(
            "https://api.github.com/repos/%s/releases/latest" % updater.REPO,
            timeout=15, headers={"User-Agent": "AutoQuill",
                                 "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        payload = r.json()
    info = updater.parse_release(payload)
    print("① 解析发布：ok=%s version=%s 安装包=%s（%.1f MB）"
          % (info["ok"], info.get("version"), info["installer"]["name"],
             info["installer"]["size"] / 1e6))
    print("   API digest = %s…" % (info.get("digest") or "")[:16])

    # ② 组装计划（假装当前是上一版，好触发「有更新」）
    current = "4.8.0"
    plan = updater.build_plan(payload, current)
    print("② 更新计划：%s → %s，校验和来源=%s，sha=%s…"
          % (current, plan.version, list(plan.sha_sources), plan.sha256[:16]))
    if plan.error:
        print("   ✗ 计划有错：%s" % plan.error)
        return 1

    # ③ 真下载 + 真校验
    if not args.skip_download:
        t0 = time.time()
        seen = []
        got = updater.download(plan.installer_url, plan.dest,
                               expected_size=plan.installer_size,
                               progress=lambda rd, tt: seen.append(rd))
        if not got["ok"]:
            print("   ✗ 下载失败：%s" % got["error"])
            return 1
        print("③ 下载完成：%.1f MB，用时 %.1fs" % (got["bytes"] / 1e6, time.time() - t0))
        time.sleep(0.2)   # 让上面的进度回调打完
        checked = updater.verify(plan.dest, plan.sha256)
        print("   校验：%s（sha256=%s…）"
              % ("通过 ✓" if checked["ok"] else "失败 ✗ %s" % checked["error"],
                 (checked.get("sha256") or "")[:16]))
        if not checked["ok"]:
            return 1

    # ④ 落 staged 状态（模拟下载成功后界面看到的状态）
    stage.update(stage=stage.STAGE_STAGED, version=plan.version, current=current,
                 installer=plan.dest, sha256=plan.sha256, size=plan.installer_size,
                 bytes=plan.installer_size, sha_sources=list(plan.sha_sources),
                 install_dir=r"D:\AutoQuill")
    st = stage.load()
    print("④ 状态机：stage=%s（%s）installer_exists=%s"
          % (st["stage"], stage.STAGE_TEXT[st["stage"]], Path(st["installer"]).exists()))

    # ⑤ 走一遍换装进程（dry-run：不安装、不重启、不删包）
    script = Path("tools/apply_update.py").resolve()
    cmd = [sys.executable, str(script), "--dry-run"]
    print("⑤ 换装进程（dry-run）：%s" % " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    for line in (p.stdout or "").splitlines():
        print("   | %s" % line)
    if p.returncode != 0:
        print("   ✗ 换装进程返回 %s" % p.returncode)
    st = stage.load()
    print("   状态：%s" % st["stage"])
    print("   日志尾部：\n%s" % stage.tail_log(6))
    print("   安装包是否保留（dry-run 应保留）：%s"
          % Path(st["installer"]).exists())

    # ⑥ 清理本次验证产物（不删用户的任何东西）
    if not args.skip_download:
        try:
            Path(st["installer"]).unlink()
            print("⑥ 已清理验证用的安装包")
        except OSError as exc:
            print("⑥ 清理失败（可忽略）：%s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
