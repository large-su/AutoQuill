# ============================================================
# core/updater.py — 自动更新的**纯逻辑**层
#
# 只做三件事，且都不碰 UI / 浏览器 / 进程：
#   1. 解析 GitHub Release 的两个 JSON（releases/latest、assets）→ 结构化信息；
#   2. 解析版本号 / 资产名 / sha256 文本；
#   3. 下载（流式 + 进度回调）与校验（sha256）。
#
# 为什么单独一层：更新的判定逻辑最容易出错（下载半截、校验和错、版本比较），
# 而这些全是纯函数，能脱离网络与界面单测。执行换装的部分在 tools/apply_update.py，
# 状态落盘在 core/update_stage.py，接口编排在 webui/update_api.py。
#
# 依赖方向：core 不认识 webui / automation（只依赖 core.paths）。
# ============================================================

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core import paths

log = logging.getLogger(__name__)

REPO = "large-su/AutoQuill"
SETUP_PREFIX = "AutoQuill-Setup-"
SETUP_SUFFIX = ".exe"
SHA_SUFFIX = ".exe.sha256"
CHUNK = 256 * 1024          # 256 KB：进度回调不至于太频繁，也不至于太粗
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


# ------------------------------------------------------------
# 解析（纯函数）
# ------------------------------------------------------------

def version_tuple(v):
    """'4.9.6' / 'v4.9.6' → (4, 9, 6)；解析不了返回 None（宁可说没更新也不误报）。"""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").strip().split("."))
    except (AttributeError, ValueError, TypeError):
        return None


def is_newer(latest, current):
    """latest 是否比 current 新；任一解析不了 → False（不误报有更新）。"""
    a, b = version_tuple(latest), version_tuple(current)
    return bool(a and b and a > b)


def clean_tag(tag):
    """'v4.9.6' → '4.9.6'；空/异常返回 None。"""
    text = str(tag or "").strip().lstrip("vV").strip()
    return text or None


def setup_asset_name(version):
    """版本号 → 安装包资产名（与 tools/build_release.py 的产物命名严格一致）。"""
    return "%s%s%s" % (SETUP_PREFIX, version, SETUP_SUFFIX)


def sha_asset_name(version):
    return setup_asset_name(version) + ".sha256"


def parse_sha256_text(text):
    """`.sha256` 资产内容 → 小写十六进制；找不到返回 None。

    build_release.py 用 certutil 生成，文件里就是 64 个十六进制字符。
    这里放宽（允许前后有空白/杂字符）但不放宽长度：宁可判失败，也不猜。
    """
    m = _SHA256_RE.search(str(text or ""))
    return m.group(1).lower() if m else None


def parse_digest(value):
    """GitHub API 的 asset.digest（形如 'sha256:abc...'）→ 小写十六进制。"""
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        algo, _, hexpart = text.partition(":")
        if algo.strip().lower() != "sha256":
            return None                 # 不是 sha256 就不采信
        text = hexpart
    return parse_sha256_text(text)


def parse_release(payload):
    """GitHub `/releases/latest` 的 JSON → 结构化信息（异常输入返回 ok=False）。

    返回 {ok, version, tag, page_url, notes, installer, sha256, digest, error}
      installer: {"name", "url", "size"}
      sha256   : 期望的安装包 sha256（取自 .sha256 资产内容，需另外下载）
      digest   : GitHub 自己算的 sha256（API 直接给，不用额外下载）
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "发布信息格式异常"}
    tag = payload.get("tag_name") or payload.get("name") or ""
    version = clean_tag(tag)
    if not version:
        return {"ok": False, "error": "发布信息里没有版本号"}
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        assets = []
    want = setup_asset_name(version)
    want_sha = sha_asset_name(version)
    installer = sha_asset = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if not url:
            continue
        if name == want:
            installer = {"name": name, "url": url,
                         "size": int(a.get("size") or 0),
                         "digest": parse_digest(a.get("digest"))}
        elif name == want_sha:
            sha_asset = {"name": name, "url": url}
    if not installer:
        return {"ok": False, "version": version,
                "error": "这个版本没有找到安装包资产（%s）" % want,
                "page_url": payload.get("html_url") or ""}
    return {
        "ok": True,
        "version": version,
        "tag": str(tag),
        "page_url": str(payload.get("html_url") or ""),
        "notes": str(payload.get("body") or "")[:4000],
        "installer": installer,
        "sha_asset": sha_asset,
        "digest": installer.get("digest"),
        "error": None,
    }


# ------------------------------------------------------------
# 落盘位置（更新专用的暂存区，绝不与用户数据混在一起）
# ------------------------------------------------------------

def update_dir() -> Path:
    d = Path(paths.data("data", "update"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def installer_path(version) -> Path:
    return update_dir() / setup_asset_name(version)


# ------------------------------------------------------------
# 下载与校验
# ------------------------------------------------------------

def sha256_file(path, progress: Optional[Callable[[int, int], None]] = None,
                chunk: int = CHUNK) -> str:
    """算文件 sha256；progress(已读字节, 总字节) 可选。"""
    total = 0
    try:
        total = os.path.getsize(path)
    except OSError:
        total = 0
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
            read += len(block)
            if progress:
                try:
                    progress(read, total)
                except Exception:       # noqa: BLE001 进度回调绝不能影响校验
                    pass
    return h.hexdigest()


def download(url, dest, *, progress=None, timeout=(30, 60),
             attempts=3, retry_wait=2.0, chunk=CHUNK, session=None,
             expected_size=0) -> dict:
    """流式下载到 dest（先写 .part 再原子改名），**带重试**。

    ★ 2026-09-29 真机反馈：43MB 安装包在国内网络下一次就成功并不可靠
      （实测 release CDN 读超时）。所以：
        · timeout 拆成 (连接超时, 读超时)：43MB 慢速下载需要更宽的读窗口；
        · 失败自动重试（默认 3 次，退避 1/2/4 秒）；
        · 每次重试都从头写 .part —— 不装断点续传（收益小、易出半截包的坑）。

    返回 {"ok", "bytes", "path", "error", "attempts"}。任何失败都清理 .part，
    绝不留半截文件冒充完整包。
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    getter = session.get if session is not None else requests.get
    last = {"ok": False, "bytes": 0, "path": str(dest), "error": "未开始",
            "attempts": 0}
    tries = max(1, int(attempts))
    for attempt in range(1, tries + 1):
        written = 0
        try:
            with getter(url, stream=True, timeout=timeout,
                        headers={"User-Agent": "AutoQuill"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or expected_size or 0)
                with open(part, "wb") as f:
                    for block in r.iter_content(chunk_size=chunk):
                        if not block:
                            continue
                        f.write(block)
                        written += len(block)
                        if progress:
                            try:
                                progress(written, total)
                            except Exception:   # noqa: BLE001
                                pass
            os.replace(part, dest)
            return {"ok": True, "bytes": written, "path": str(dest),
                    "error": None, "attempts": attempt}
        except Exception as exc:        # noqa: BLE001
            try:
                part.unlink()
            except OSError:
                pass
            last = {"ok": False, "bytes": written, "path": str(dest),
                    "error": "%s: %s" % (exc.__class__.__name__, exc),
                    "attempts": attempt}
            log.warning("更新：下载第 %d/%d 次失败（%s）", attempt, tries, exc)
            if attempt < tries:
                # 进度回退到 0，界面不要显示一个假的"已完成一半"
                if progress:
                    try:
                        progress(0, expected_size)
                    except Exception:       # noqa: BLE001
                        pass
                time.sleep(retry_wait * (2 ** (attempt - 1)))
    return last


def fetch_expected_sha256(info, *, timeout=15, session=None) -> dict:
    """取「期望的 sha256」，两个来源交叉验证。

    来源 A：release 里的 `.sha256` 资产（我们构建时用 certutil 生成）。
    来源 B：GitHub API 的 asset.digest（GitHub 自己算的）。
    两者都有且不一致 → 判失败（宁可拒绝更新，也不装来路不明的东西）。
    返回 {"ok", "sha256", "sources", "error"}
    """
    import requests

    getter = session.get if session is not None else requests.get
    found = {}
    sha_asset = (info or {}).get("sha_asset") or {}
    if sha_asset.get("url"):
        try:
            r = getter(sha_asset["url"], timeout=timeout,
                       headers={"User-Agent": "AutoQuill"})
            r.raise_for_status()
            got = parse_sha256_text(getattr(r, "text", "") or "")
            if got:
                found["asset"] = got
        except Exception as exc:        # noqa: BLE001
            log.warning("更新：下载 .sha256 资产失败（%s）", exc)
    digest = parse_digest((info or {}).get("digest"))
    if digest:
        found["digest"] = digest
    if not found:
        return {"ok": False, "sha256": None, "sources": [],
                "error": "拿不到校验和（既没有 .sha256 资产，也没有 API digest）"}
    values = set(found.values())
    if len(values) > 1:
        return {"ok": False, "sha256": None, "sources": sorted(found),
                "error": "两个来源的校验和不一致，已拒绝更新（%s）"
                         % "、".join("%s=%s" % (k, v[:12]) for k, v in sorted(found.items()))}
    return {"ok": True, "sha256": values.pop(), "sources": sorted(found), "error": None}


def verify(path, expected) -> dict:
    """校验已下载的安装包。返回 {"ok", "sha256", "error"}。"""
    want = parse_sha256_text(expected)
    if not want:
        return {"ok": False, "sha256": None, "error": "期望的校验和无效"}
    got = sha256_file(path)
    if got != want:
        return {"ok": False, "sha256": got,
                "error": "校验和不匹配（期望 %s… 实际 %s…）" % (want[:12], got[:12])}
    return {"ok": True, "sha256": got, "error": None}


# ------------------------------------------------------------
# 安装目录探测（换装必须装回原处）
# ------------------------------------------------------------

def current_install_dir():
    """当前程序所在目录；**冻结态一定是**，源码态返回 None（不该自动换装）。

    用户机器上可能装在任意目录（例如 D:\\AutoQuill），安装器的默认目录是
    %LOCALAPPDATA%\\Programs\\AutoQuill——所以换装时必须显式带 /DIR。
    """
    import sys
    if not getattr(sys, "frozen", False):
        return None
    try:
        return str(Path(sys.executable).resolve().parent)
    except Exception as exc:            # noqa: BLE001
        log.warning("更新：探测安装目录失败（%s）", exc)
        return None


def detect_existing_install_dir_from_registry():
    """从卸载信息里读安装目录（探测失败时的兜底）。找不到返回 None。"""
    try:
        import winreg
    except ImportError:
        return None
    key = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
           r"\{F0D565E3-C1A9-40A6-894E-615014A3A357}_is1")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, "InstallLocation")
            value = str(value or "").strip()
            return value or None
    except OSError:
        return None
    except Exception as exc:            # noqa: BLE001
        log.debug("更新：读注册表安装目录失败（%s）", exc)
        return None


def resolve_install_dir():
    """换装目标目录：优先 sys.executable 的父目录，其次注册表。"""
    return current_install_dir() or detect_existing_install_dir_from_registry()


def installer_args(installer, install_dir, log_path=None):
    """静默换装的命令行参数（Inno Setup 6）。

    /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL：全静默、不弹框、
      不自己重启、不允许取消——用户点过确认了，中途不该再问。
    /CLOSEAPPLICATIONS 不传：关程序由我们自己控制（安装器脚本里已设
      CloseApplications=no），避免它在没准备好时就动手。
    /DIR 必传：装回原目录（用户可能装在 D:\\AutoQuill 而不是默认位置）。
    """
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL"]
    if install_dir:
        args.append('/DIR="%s"' % str(install_dir))
    if log_path:
        args.append('/LOG="%s"' % str(log_path))
    return args


def apply_script_path():
    """定位换装脚本 `tools/apply_update.py`。

    真机踩过两次路径坑，所以把所有可能的位置都试一遍：
      · 源码态：项目根/tools/apply_update.py；
      · 冻结态：程序目录/tools/... 或 PyInstaller 解包目录(_MEIPASS)/tools/...
        （spec 把它作为 datas 打进 `_internal/tools/`，而冻结态下 __file__
         指向 _internal/，只按 exe 同级目录找会扑空）。
    找不到返回空串（调用方必须能优雅失败，不能让更新进程挂住）。
    """
    import sys
    here = Path(__file__).resolve().parent            # <root>/core
    candidates = [here.parent / "tools" / "apply_update.py",          # 源码态
                  here.parent / "_internal" / "tools" / "apply_update.py"]
    meipass = getattr(sys, "_MEIPASS", "") or ""
    if meipass:
        candidates.insert(0, Path(meipass) / "tools" / "apply_update.py")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.insert(0, exe_dir / "tools" / "apply_update.py")
        candidates.insert(1, exe_dir / "_internal" / "tools" / "apply_update.py")
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return ""


def run_apply_script(argv=None):
    """在**当前进程**里执行换装脚本（把 --apply-update 之后的参数转给它）。

    由 launcher（冻结态入口）与 main.py（源码态入口）共同调用——两处入口都要
    能分流，否则打包后点「重启并安装」只会又开一个窗口（真机踩到）。
    返回进程退出码。

    ★ 真机教训：windowed 打包态的 stdout/stderr 是 None，任何 print/异常回溯都
      可能无声无息。所以这里**第一步就把自己的 stdio 接到 apply.log**，
      任何后续异常都能被看见（而不是"点了没反应"）。
    """
    import runpy
    import sys
    rest = [a for a in (argv if argv is not None else sys.argv[1:])
            if a != "--apply-update"]
    try:
        from core import update_stage as _stage
        logf = open(_stage.log_file(), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = logf
    except Exception:                           # noqa: BLE001 接不上也不能中断更新
        pass
    try:
        script = apply_script_path()
        if script:
            sys.argv = [script] + rest
            runpy.run_path(script, run_name="__main__")
            return 0
        from tools import apply_update          # 兜底：仅源码态可用
        sys.argv = ["apply_update"] + rest
        return apply_update.main()
    except SystemExit as exc:                   # 脚本自己 sys.exit
        return int(getattr(exc, "code", 0) or 0)
    except Exception as exc:                    # noqa: BLE001
        import traceback
        try:
            traceback.print_exc()
        except Exception:                       # noqa: BLE001
            pass
        try:
            from core import update_stage as _stage2
            _stage2.mark_failed("换装进程异常：%s" % exc)
        except Exception:                       # noqa: BLE001
            pass
        return 1


def powershell_apply_command(installer, pid, install_dir, log_path,
                             relaunch_exe="", wait_seconds=600):
    """生成换装宿主的启动命令（参数列表，含 -EncodedCommand）。

    ★★ 关键约束（2026-10-01 线上事故的根因，务必别改回去）：
       宿主**必须脱离主程序的进程树**。这一点我踩了两次：
         · v4.9.15：拿 AutoQuill.exe 当宿主 → 启动器把它当「正常启动」，
           黑框循环，主程序始终没退；
         · v4.9.17/18：用 Popen 从主程序里起 PowerShell → 主程序退出时
           **宿主被一起带走**（进程树/作业对象连带结束）。真机日志停在
           「等待主程序退出 pid=…」就没了：安装器根本没跑，版本没变，
           程序也没重启——用户看到的就是「点了重启，程序关了，再也没回来」。
       所以调用方必须用 `detach_via_wmi()`（WMI，宿主挂到 WmiPrvSE 名下）
       或 `vbs_detach_launcher()` 来起它，**绝不能直接 Popen**。

    pid=0 表示不等待（仅在已经退出时用）。
    """
    script = powershell_host_script(installer, pid, install_dir, log_path,
                                    relaunch_exe=relaunch_exe,
                                    wait_seconds=wait_seconds)
    # -EncodedCommand 要求 UTF-16LE + base64：彻底避开引号与中文路径的转义地狱
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def powershell_host_script(installer, pid, install_dir, log_path,
                           relaunch_exe="", wait_seconds=600):
    """换装宿主的 PowerShell 脚本正文（单独暴露，便于单测与人工核对）。

    每一步都写日志：真机出问题时日志是唯一的事实来源
    （上一次事故就是靠「日志停在哪一行」定位到宿主被带走的）。
    安装前后各记一次 exe 时间戳，用来证明**文件真的被覆盖了**。
    """
    args = " ".join(installer_args(installer, install_dir, log_path))
    exe = str(relaunch_exe or "")
    install_dir = str(install_dir or "")
    restart_block = ""
    if exe:
        restart_block = ("  L '正在重启新版本…';"
                         "  Start-Process -FilePath '{exe}';"
                         "  L '已重启 AutoQuill';".format(exe=exe))
    return (
        "$ErrorActionPreference='Continue';"
        "$log='{log}';"
        "function L($m){{ Add-Content -LiteralPath $log -Encoding UTF8 "
        "-Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' ' + $m) }};"
        "L ('=== 换装开始（宿主 pid=' + $PID + '，已脱离主程序进程树）===');"
        "$target={pid};"
        "if ($target -gt 0) {{"
        "  L ('等待主程序退出 pid=' + $target);"
        "  $deadline=(Get-Date).AddSeconds({wait});"
        "  $gone=$false;"
        "  while ((Get-Date) -lt $deadline) {{"
        "    if (-not (Get-Process -Id $target -ErrorAction SilentlyContinue)) "
        "{{ $gone=$true; break }};"
        "    Start-Sleep -Milliseconds 500 }};"
        "  if ($gone) {{ L '主程序已退出' }}"
        "  else {{"
        "    L '主程序超时未退出（上限 {wait} 秒），强制结束';"
        "    Stop-Process -Id $target -Force -ErrorAction SilentlyContinue;"
        "    Start-Sleep -Seconds 2 }}"
        "}};"
        "Start-Sleep -Seconds 2;"
        "$exePath=Join-Path '{idir}' 'AutoQuill.exe';"
        "$before='';"
        "try {{ if (Test-Path $exePath) {{ "
        "$before=(Get-Item $exePath).LastWriteTime.ToString('s') }} }} catch {{}};"
        "L ('安装前 AutoQuill.exe 时间戳=' + $before);"
        "L '运行安装包（静默）…';"
        "$p=Start-Process -FilePath '{installer}' -ArgumentList '{args}' "
        "-Wait -PassThru -WindowStyle Hidden;"
        "L ('安装器返回码=' + $p.ExitCode);"
        "$after='';"
        "try {{ if (Test-Path $exePath) {{ "
        "$after=(Get-Item $exePath).LastWriteTime.ToString('s') }} }} catch {{}};"
        "L ('安装后 AutoQuill.exe 时间戳=' + $after);"
        "if ($before -ne $after) {{ L '文件已更新（时间戳变化）' }}"
        "else {{ L '警告：exe 时间戳未变化，安装可能没有真正覆盖' }};"
        "{restart}"
        "if ($p.ExitCode -eq 0) {{"
        "  Remove-Item -LiteralPath '{installer}' -Force -ErrorAction SilentlyContinue;"
        "  L '已删除安装包' }}"
        "else {{ L '安装器返回非 0，保留安装包供手动安装' }};"
        "L '=== 换装结束 ===';"
    ).format(log=str(log_path), pid=int(pid or 0), wait=int(wait_seconds),
             installer=str(installer), args=args, restart=restart_block,
             idir=install_dir)


def _with_env(command, env):
    """把环境变量内联进命令行：`cmd /c set K=V && <真正的命令>`。

    ★ 为什么不用 VBS 的 Environment("PROCESS")（实测无效，2026-10-01）：
      WMI 创建的新进程由 **WmiPrvSE 服务**派生，继承的是**服务**的环境块，
      不是本进程的。所以只能在命令行里显式设好——交给 cmd.exe 设环境再拉起
      子进程，子进程就一定能拿到。
    """
    items = []
    for k, v in dict(env or {}).items():
        key, val = str(k), str(v)
        if key and val:
            items.append((key, val))
    if not items:
        return command
    # set "K=V" 形式最安全（引号包住整个赋值，值里有 & | ^ 也不会被解释）
    sets = " && ".join('set "%s=%s"' % (k, v) for k, v in items)
    return '%s /c %s && %s' % (os.environ.get("COMSPEC") or "cmd.exe", sets, command)


def _split_args(cmdline):
    """把命令行拆成参数列表（处理引号），供 VBScript 逐个加引号重建。"""
    out, buf, in_q = [], [], False
    for ch in str(cmdline):
        if ch == '"':
            in_q = not in_q
        elif ch in " \t" and not in_q:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [p for p in out if p != ""]


def _ascii_safe_path(path):
    """把可能含非 ASCII 的路径换成 8.3 短路径（脚本宿主只认 ANSI）。

    取不到短路径就原样返回——调用方要能接受"可能仍然非 ASCII"。
    """
    text = str(path)
    if text.isascii():
        return text
    if os.name != "nt":
        return text
    try:
        import ctypes
        from ctypes import wintypes
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = get_short(text, buf, 1024)
        if n and buf.value:
            return buf.value
    except Exception:                       # noqa: BLE001 取不到就原样返回
        pass
    return text


def _ascii_safe_command(cmdline):
    """命令行整体转 ASCII 安全：对其中每个"看起来像路径"的片段取短路径。"""
    import re
    def repl(m):
        return _ascii_safe_path(m.group(0))
    # 引号包起来的片段，或形如 X:\\... 的裸路径
    out = re.sub(r'"[^"]*"', lambda m: '"%s"' % _ascii_safe_path(m.group(0)[1:-1]),
                 cmdline)
    return re.sub(r'[A-Za-z]:\\[^\s"]+', repl, out)


def vbs_detach_launcher(command, log_path="", env=None):
    """生成一段 VBScript：用 WMI 起进程，**自身立刻退出**。

    ★ 为什么必须绕这一道（实测结论，2026-10-01）：
      从主程序里直接 `subprocess.Popen` 起的宿主是**子进程**，主程序退出时
      会被一起结束（进程树/作业对象）→ 「程序关了、更新没装、也没重启」。
      而 `Win32_Process.Create` 创建出来的进程挂在 WMI 服务（WmiPrvSE.exe）
      名下，**不在主程序进程树里**。实测：宿主父进程被强杀后，
      它仍继续存活并写满日志。
      wscript.exe 是系统自带、无额外依赖，用它跑这段 VBS 最省事。

    command: 要执行的完整命令行（字符串）。返回 VBS 脚本文本。
    env: 需要额外传给子进程的环境变量（如 AQ_DATA_DIR）。
         ★ 必须显式传（2026-10-01 实测）：WMI 创建的子进程**不继承**调用方的
           环境变量（`WshShell.Environment("PROCESS")` 也无效，因为新进程由
           WmiPrvSE 服务创建）。这里改成把变量内联进命令行：用
           `cmd /c set K=V && 真正的命令`，由 cmd 自己设好环境再拉起子进程。

    ★ 编码坑（2026-10-01 实测）：Windows 脚本宿主（wscript）是 **ANSI** 引擎，
      读不了 UTF-8 的 .vbs —— 文件里的路径会被解成乱码，于是「WMI 报 rc=0
      但目标程序根本没跑」。所以这里生成的脚本**只用 ASCII**：
      非 ASCII 路径会先转成 8.3 短路径。日志正文由被启动的宿主自己写。
    """
    command = _with_env(command, env)
    cmd = str(command)
    if not cmd.isascii():
        # 命令行含非 ASCII（例如中文用户名目录）：转成 8.3 短路径后再交给 ANSI 脚本宿主
        cmd = _ascii_safe_command(cmd)
    # ★★ 引号处理（真机踩到两次）：
    #    ① VBScript 字面量里放双引号要写 ""，但 Win32_Process.Create 需要**看见**
    #       引号，嵌套转义会算错（`""""x""""` → `""x""`），路径直接废掉；
    #       → 改成运行时用 Chr(34) 拼引号，源码里不出现引号，零歧义。
    #    ② 但**只给含空格的片段加引号**：把 `--pid` 也包成 `"--pid"` 会让
    #       argparse 收不到开关；而 `set "K=V"` 这类片段本来就带引号，
    #       必须原样保留（剥掉就把命令弄坏了）。
    pieces = []
    for part in _split_args(cmd):
        if part.startswith('"') and part.endswith('"') and len(part) > 1:
            pieces.append('Q & "%s" & Q' % part[1:-1])     # 保留原有引号
        elif " " in part:
            pieces.append('Q & "%s" & Q' % part)
        else:
            pieces.append('"%s"' % part)
    create_expr = ' & " " & '.join(pieces)
    log = str(log_path or "")
    if log and not log.isascii():
        log = _ascii_safe_path(log)
    log = log.replace('"', '""')
    # WMI 的 moniker 要写成 winmgmts:\\.\root\cimv2（VBScript 里反斜杠不转义）
    moniker = "winmgmts:" + "\\\\" + "." + "\\root\\cimv2"
    lines = [
        'Option Explicit',
        'Dim wmi, proc, rc, Q, en, ed, proc2',
        'Q = Chr(34)',                        # 运行时的双引号：源码里不出现嵌套引号
        'Set wmi = GetObject("%s")' % moniker,
        'On Error Resume Next',
    ]
    lines += [
        'Set proc = wmi.Get("Win32_Process")',
        'Set proc2 = wmi.Get("Win32_Process")',
        'rc = proc.Create(%s, Null, Null, 0)' % create_expr,
        'en = Err.Number',
        'ed = Err.Description',
        'On Error GoTo 0',
    ]
    if log:
        lines += [
            'Dim fso, f',
            'Set fso = CreateObject("Scripting.FileSystemObject")',
            'Set f = fso.OpenTextFile("%s", 8, True)' % log,
            'If rc = 0 Then',
            '  f.WriteLine "WMI spawn ok pid=" & proc2.ProcessId',
            'Else',
            '  f.WriteLine "WMI spawn FAILED rc=" & rc & " err=" & en & " " & ed',
            'End If',
            'f.Close',
        ]
    lines.append('WScript.Quit 0')
    return "\r\n".join(lines) + "\r\n"


@dataclass
class UpdatePlan:
    """一次更新的完整计划（检查阶段的产物，UI 与执行都只看它）。"""

    version: str = ""
    current: str = ""
    installer_url: str = ""
    installer_name: str = ""
    installer_size: int = 0
    sha256: str = ""
    sha_sources: tuple = ()
    page_url: str = ""
    notes: str = ""
    dest: str = ""
    error: str = ""
    release_info: dict = field(default_factory=dict)   # 解析后的 release（取校验和用）
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        d = {
            "version": self.version, "current": self.current,
            "installer_url": self.installer_url,
            "installer_name": self.installer_name,
            "installer_size": int(self.installer_size or 0),
            "sha256": self.sha256, "sha_sources": list(self.sha_sources or ()),
            "page_url": self.page_url, "notes": self.notes,
            "dest": self.dest, "error": self.error,
        }
        d.update(self.extra or {})
        return d


def build_plan(release_payload, current_version, *, sha_lookup=None) -> UpdatePlan:
    """把 release JSON + 当前版本 → UpdatePlan（纯函数，便于单测）。

    sha_lookup: 可注入的「取期望校验和」函数（默认走网络）；
                单测里传 lambda info: {"ok": True, "sha256": "..."} 即可脱网。
    """
    info = parse_release(release_payload)
    plan = UpdatePlan(current=str(current_version or ""))
    if not info.get("ok"):
        plan.error = info.get("error") or "发布信息不可用"
        plan.page_url = info.get("page_url") or ""
        return plan
    plan.version = info["version"]
    plan.page_url = info.get("page_url") or ""
    plan.notes = info.get("notes") or ""
    plan.installer_url = info["installer"]["url"]
    plan.installer_name = info["installer"]["name"]
    plan.installer_size = int(info["installer"]["size"] or 0)
    plan.dest = str(installer_path(plan.version))
    if not is_newer(plan.version, plan.current):
        plan.error = ""                 # 不是错误：只是没有更新
        return plan
    lookup = sha_lookup or fetch_expected_sha256
    got = lookup(info) or {}
    plan.sha_sources = tuple(got.get("sources") or ())
    if got.get("ok") and got.get("sha256"):
        plan.sha256 = got["sha256"]
    else:
        # 拿不到校验和 → 不许更新（宁可不更新，也不装来路不明的东西）
        plan.error = got.get("error") or "拿不到校验和"
    return plan


def load_release_from_disk(path) -> dict:
    """读本地保存的 release JSON（离线/单测用）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:            # noqa: BLE001
        return {"__error": str(exc)}
