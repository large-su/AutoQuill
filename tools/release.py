#!/usr/bin/env python3
"""Prepare, build, tag, and publish a two-file AutoQuill release."""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "large-su/AutoQuill"
CI_WORKFLOW = "test.yml"


def capture(cmd, timeout=None):
    options = dict(capture_output=True, text=True, encoding="utf-8")
    if timeout is not None:
        options["timeout"] = timeout
    return subprocess.run([str(c) for c in cmd], cwd=str(ROOT), **options)


def timed(label, action):
    started = time.monotonic()
    print(label, flush=True)
    result = action()
    print(f"✓ {label}（{time.monotonic() - started:.1f}s）", flush=True)
    return result

def run(cmd, **kw):
    print("$ " + " ".join(map(str, cmd)))
    result = subprocess.run([str(c) for c in cmd], cwd=str(ROOT), **kw)
    if result.returncode:
        raise RuntimeError(f"命令失败（exit {result.returncode}）：{' '.join(map(str, cmd))}")
    return result

def out(cmd):
    result = capture(cmd)
    if result.returncode:
        raise RuntimeError(f"命令失败：{' '.join(map(str, cmd))}")
    return (result.stdout or "").strip()

def version():
    sys.path.insert(0, str(ROOT))
    import core.version
    return core.version.VERSION

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

def changed_paths():
    # Include committed changes since the last ancestor release and current edits.
    paths = set(filter(None, out(["git", "diff", "--name-only", "HEAD"]).splitlines()))
    tag = capture(["git", "describe", "--tags", "--match", "v*", "--abbrev=0", "HEAD"])
    if tag.returncode == 0:
        paths.update(filter(None, out(["git", "diff", "--name-only", tag.stdout.strip(), "HEAD"]).splitlines()))
    elif tag.returncode != 128:
        raise RuntimeError("无法确定上一发布 tag")
    paths.update(filter(None, out(["git", "ls-files", "--others", "--exclude-standard"]).splitlines()))
    return sorted(paths)

def select_tests(paths, full=False):
    if full:
        return ["tests"]
    selected, unknown = set(), False
    for raw in paths:
        path = raw.replace("\\", "/")
        name = Path(path).name
        if path == "requirements.txt":
            unknown = True
            continue
        if path.startswith("tests/test_") and path.endswith(".py"):
            if (ROOT / path).is_file():
                selected.add(path[:-3].replace("/", "."))
            continue
        if path in {"core/update_host.ps1", "launcher.py", "tools/apply_update.py"} or path.startswith("installer/") or path.endswith(".spec"):
            selected.update(("tests.test_updater", "tests.test_update_host_integration", "tests.test_update_lifecycle"))
            continue
        if path.endswith(".py"):
            stem = Path(path).stem
            if stem in {"update_api", "update_stage", "updater"}:
                selected.add("tests.test_updater")
            elif path == "core/version.py" or path.startswith("docs/"):
                continue
            elif stem in {"release", "build_release"}:
                selected.add("tests.test_release_flow")
            elif stem in {"evolution", "evolution_api"}:
                selected.add("tests.test_evolution")
            elif stem == "evolution_history":
                selected.add("tests.test_evolution_history")
            elif (ROOT / "tests" / f"test_{stem}.py").is_file():
                selected.add(f"tests.test_{stem}")
            else:
                unknown = True
        elif path.endswith(".js"):
            if name == "evolution.js":
                selected.add("tests.test_evolution")
            elif path.endswith("webui/static/app.js") or name == "automation.js":
                selected.add("tests.test_update_ui" if name == "app.js" else "tests.test_automation_ui")
                if name == "automation.js":
                    selected.add("tests.test_checkin")
            else:
                unknown = True
        elif path == "webui/static/index.html":
            selected.add("tests.test_update_ui")
        elif path == "core/evolution_history.json":
            selected.add("tests.test_evolution_history")
        elif path.endswith(".json") and not path.startswith("docs/"):
            unknown = True
    return ["tests"] if unknown else sorted(selected)

def normalize_message(message, ver):
    lines = message.strip().splitlines() or ["release"]
    first = lines[0].strip()
    prefix = r"^(?:v?\d+(?:\.\d+){1,3}\s*[:：-]\s*|(?:feat|fix|chore|docs|refactor|build|release|test|perf)(?:\([^)]*\))?\s*:\s*)"
    for _ in range(2):
        first = re.sub(prefix, "", first, flags=re.I)
    lines[0] = f"v{ver}: {first or 'release'}"
    return "\n".join(lines)

def _preflight(tag, plan=False):
    if out(["git", "branch", "--show-current"]) != "main":
        raise RuntimeError("发布必须在 main 分支")
    if out(["git", "tag", "-l", tag]):
        raise RuntimeError(f"目标 tag 已存在：{tag}（不会删除既有 tag；先设置新版本）")
    if plan:
        return
    remote = capture(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    if remote.returncode:
        raise RuntimeError("无法检查远程 tag，未开始发布")
    if remote.stdout.strip():
        raise RuntimeError(f"远程目标 tag 已存在：{tag}（不会删除既有 tag）")
    identity = [capture(["git", "config", "--get", field]) for field in ("user.name", "user.email")]
    if any(result.returncode or not result.stdout.strip() for result in identity):
        raise RuntimeError("未配置 Git 身份，请设置 git config user.name 与 user.email")

def _validate_manifest():
    info = ROOT / "dist" / "AutoQuill" / "_internal" / "build_info.json"
    if not info.is_file():
        raise RuntimeError("--skip-build 要求存在 build_info.json")
    data = json.loads(info.read_text(encoding="utf-8"))
    if data.get("version") != version() or data.get("source_commit") != out(["git", "rev-parse", "HEAD"]):
        raise RuntimeError("构建 manifest 的 version/source_commit 与当前 HEAD 不一致")


def validate_local_assets(exe, sha):
    if not exe.is_file() or not sha.is_file():
        raise RuntimeError("本地发布资产缺失")
    if _sha256(exe) != sha.read_text(encoding="utf-8").strip():
        raise RuntimeError("本地发布资产 sha256 不匹配")

def validate_published_assets(tag, exe, sha, verify_download=False):
    """Check release API metadata and the tiny sidecar without downloading the exe."""
    validate_local_assets(exe, sha)
    payload = json.loads(out(["gh", "api", f"repos/{REPO}/releases/tags/{tag}"]))
    assets = {item["name"]: item for item in payload.get("assets", [])}
    expected = {exe.name: exe, sha.name: sha}
    if set(assets) != set(expected):
        raise RuntimeError("Release 必须恰好包含 exe 与 .sha256 两个资产")
    exe_asset = assets[exe.name]
    if exe_asset.get("size") != exe.stat().st_size or exe_asset.get("digest", "").removeprefix("sha256:") != _sha256(exe):
        raise RuntimeError("发布 exe 的 size/digest 与本地构建产物不一致")
    sidecar = assets[sha.name]
    if sidecar.get("size") != sha.stat().st_size:
        raise RuntimeError("发布 .sha256 的 size 与本地资产不一致")
    published_sha = out(["gh", "api", f"repos/{REPO}/releases/assets/{sidecar['id']}", "--header", "Accept: application/octet-stream"]).strip()
    if published_sha != sha.read_text(encoding="utf-8").strip():
        raise RuntimeError("发布 .sha256 内容与本地资产不一致")
    if verify_download:
        with tempfile.TemporaryDirectory(prefix="aq-release-check-") as directory:
            run(["gh", "release", "download", tag, "--pattern", exe.name, "--dir", directory])
            if _sha256(Path(directory) / exe.name) != _sha256(exe):
                raise RuntimeError("回下载的安装包 sha256 不匹配")


def _positive_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("必须是正数") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return seconds


def _ci_run_key(run):
    """Order runs by creation, run number, and attempt without trusting list order."""
    def timestamp(value):
        if not value:
            return ""
        return str(value)
    def number(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return (timestamp(run.get("created_at")), number(run.get("run_number")),
            number(run.get("run_attempt")), number(run.get("id")))


def _ci_api_runs(commit):
    query = (f"repos/{REPO}/actions/workflows/{CI_WORKFLOW}/runs"
             f"?head_sha={commit}&branch=main&event=push&per_page=10")
    try:
        result = capture(["gh", "api", query], timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("查询 GitHub CI 超时（30s）") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "未知 gh api 错误").strip()
        raise RuntimeError(f"查询 GitHub CI 失败：{detail}")
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub CI API 返回了无效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise RuntimeError("GitHub CI API 返回格式异常：缺少 workflow_runs")
    if any(not isinstance(run, dict) for run in payload["workflow_runs"]):
        raise RuntimeError("GitHub CI API 返回格式异常：workflow_runs 项不是对象")
    return payload["workflow_runs"]


def wait_for_ci(commit, timeout=900, poll_interval=20):
    """Wait for the push CI run for exactly *commit* to finish successfully."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("CI 检查要求 40 位完整提交 SHA")
    if (not math.isfinite(float(timeout)) or not math.isfinite(float(poll_interval))
            or timeout <= 0 or poll_interval <= 0):
        raise RuntimeError("CI 检查 timeout 和 poll_interval 必须大于 0")
    timeout = float(timeout)
    poll_interval = float(poll_interval)
    started = time.monotonic()
    deadline = started + timeout
    last_state = None
    while True:
        runs = _ci_api_runs(commit)
        matches = [run for run in runs
                   if isinstance(run, dict)
                   and run.get("head_sha", "").lower() == commit.lower()
                   and run.get("head_branch") == "main"
                   and run.get("event") == "push"]
        if matches:
            run = max(matches, key=_ci_run_key)
            status = run.get("status")
            conclusion = run.get("conclusion")
            url = run.get("html_url") or f"https://github.com/{REPO}/actions/runs/{run.get('id', '')}"
            state = (status, conclusion, run.get("id"), run.get("run_attempt"))
            if state != last_state:
                print(f"CI：{status or 'unknown'}{(' / ' + conclusion) if conclusion else ''}（{url}）",
                      flush=True)
                last_state = state
            if status == "completed":
                if conclusion == "success":
                    return run
                if conclusion in {"failure", "cancelled", "timed_out"}:
                    raise RuntimeError(f"CI 未通过（{conclusion}）：{url}")
                raise RuntimeError(f"CI 返回未知结论（{conclusion!r}）：{url}")
            if status not in {"queued", "in_progress", "waiting", "requested", "pending"}:
                raise RuntimeError(f"CI 返回未知状态（{status!r}）：{url}")
        else:
            if last_state != ("not_found",):
                print("CI：尚未登记匹配的 workflow run", flush=True)
                last_state = ("not_found",)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"CI 检查超时（提交 {commit} 尚未完成）")
        time.sleep(min(30, poll_interval, remaining))

def run_tests(command):
    """Keep routine test output in a log; surface the result or failure context."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(command, cwd=str(ROOT), env=environment,
                            capture_output=True, text=True, encoding="utf-8")
    output = (result.stdout or "") + (result.stderr or "")
    log = ROOT / "logs" / f"release-checks-{version()}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("测试失败，日志：%s\n%s" % (log, "\n".join(output.splitlines()[-40:])))
    summary = [line for line in (result.stderr or "").splitlines()
               if line.startswith(("Ran ", "OK", "共执行"))]
    print("；".join(summary) or "相关测试通过")


def run_checks(paths, tests):
    if tests == ["tests"]:
        run_tests([sys.executable, str(ROOT / "tests" / "run_all.py")])
    elif tests:
        run_tests([sys.executable, "-m", "unittest", "-q", *tests])
    python_files = [path for path in paths if path.endswith(".py") and (ROOT / path).is_file()]
    if python_files:
        run([sys.executable, "-m", "py_compile", *python_files])
    javascript = [path for path in paths if path.endswith(".js") and (ROOT / path).is_file()]
    if javascript and not shutil.which("node"):
        raise RuntimeError("修改了 JavaScript，需要安装 Node 以完成语法检查")
    for path in javascript:
        run(["node", "--check", path])
    run(["git", "diff", "--check"])


def build_installer():
    """Keep verbose packaging output off the normal release summary."""
    log = ROOT / "logs" / f"release-build-{version()}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("w", encoding="utf-8") as stream:
            run([sys.executable, str(ROOT / "tools" / "build_release.py"), "--skip-test"],
                stdout=stream, stderr=subprocess.STDOUT)
    except RuntimeError as exc:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
        raise RuntimeError(f"构建失败，日志：{log}\n{tail}") from exc
    print(f"构建日志：{log}")


def commit_release(message, ver):
    run(["git", "add", "-A"])
    dirty = bool(out(["git", "status", "--porcelain"]))
    if not dirty and out(["git", "log", "-1", "--format=%s"]).startswith(f"v{ver}:"):
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(normalize_message(message, ver))
        msgfile = handle.name
    try:
        command = ["git", "commit", "-F", msgfile]
        if not dirty:
            command.append("--allow-empty")
        run(command)
    finally:
        os.unlink(msgfile)


def release_assets(ver):
    exe = ROOT / "release" / f"AutoQuill-Setup-{ver}.exe"
    return exe, exe.with_suffix(exe.suffix + ".sha256")


def create_release(tag, notes, assets):
    run(["gh", "release", "create", tag, "--verify-tag", "--title",
         f"{tag} — AutoQuill", "--notes-file", str(notes), *map(str, assets)])


def main(argv=None):
    started = time.monotonic()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", default="", help="默认 release/release_notes_<版本>.md")
    ap.add_argument("-m", "--message", default="", help="提交摘要，脚本自动加版本前缀")
    ap.add_argument("--message-file", default="", help="UTF-8 提交信息文件")
    ap.add_argument("--skip-test", action="store_true", help="复用已完成的测试与语法检查")
    ap.add_argument("--full-test", action="store_true", help="执行 run_all 完整回归")
    ap.add_argument("--skip-build", action="store_true", help="复用 manifest 与当前 HEAD 一致的构建")
    ap.add_argument("--plan", action="store_true", help="只显示计划，不写文件或访问网络")
    ap.add_argument("--verify-download", action="store_true", help="额外回下载完整 EXE 校验")
    ap.add_argument("--check-ci", metavar="GIT_REF", help="只检查指定本地 ref 的 push CI，不执行发布")
    ap.add_argument("--ci-timeout", type=_positive_seconds, default=900,
                    help="CI 等待秒数（默认 900）")
    args = ap.parse_args(argv)
    if args.check_ci:
        commit = out(["git", "rev-parse", "--verify", f"{args.check_ci}^{{commit}}"])
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise RuntimeError(f"无法将 ref 解析为完整提交 SHA：{args.check_ci}")
        wait_for_ci(commit, timeout=args.ci_timeout)
        print(f"CI 检查通过：{commit}")
        return 0
    ver = version()
    tag = "v" + ver
    paths = changed_paths()
    tests = select_tests(paths, args.full_test)
    assets = release_assets(ver)
    print(f"版本：{tag}\n提交前缀：{tag}:\n资产：{', '.join(path.name for path in assets)}")
    print("改动文件：" + (", ".join(paths) or "无"))
    print("测试：" + (", ".join(tests) if tests else "文档/版本调整，无运行时单测"))
    if args.plan:
        existing = out(["git", "tag", "-l", tag])
        if existing:
            print(f"目标 {tag} 已存在，实际发布前需设置新版本号")
        return 0
    message = (Path(args.message_file).read_text(encoding="utf-8-sig")
               if args.message_file else args.message)
    notes = ROOT / (args.notes or f"release/release_notes_{ver}.md")
    if not message.strip():
        raise RuntimeError("提交信息为空：用 --message-file 或 -m")
    if not notes.is_file():
        raise RuntimeError(f"发布说明不存在：{notes}")
    timed("1. 发布前检查", lambda: _preflight(tag))
    if not args.skip_build:
        from tools.build_release import sync_release_metadata
        from tools.evolution_history import refresh_history

        def sync_metadata():
            sync_release_metadata()
            refresh_history(ROOT, current_version=ver,
                            pending_summary=normalize_message(message, ver).splitlines()[0])

        timed("2. 同步版本元数据与演进历史", sync_metadata)
    if not args.skip_test:
        timed("3. 相关测试与语法检查", lambda: run_checks(paths, tests))
    timed("4. 提交代码", lambda: commit_release(message, ver))
    if args.skip_build:
        _validate_manifest()
    else:
        timed("5. 构建安装包", build_installer)
    validate_local_assets(*assets)
    run(["git", "tag", tag])
    commit = out(["git", "rev-parse", f"{tag}^{{commit}}"])
    timed("6. 推送 main 和 tag", lambda: run(["git", "push", "--atomic", "origin", "main", tag]))
    timed("7. 创建 Release", lambda: create_release(tag, notes, assets))
    timed("8. 校验发布资产", lambda: validate_published_assets(tag, *assets, args.verify_download))
    try:
        timed("9. 等待精确提交的 GitHub CI", lambda: wait_for_ci(commit, timeout=args.ci_timeout))
    except RuntimeError as exc:
        raise RuntimeError(
            f"CI gate 未通过；资产/tag 可能已经发布。请使用 --check-ci {commit} 独立检查，"
            f"不要重跑发布或覆盖 tag：{exc}") from exc
    print(f"发布完成：https://github.com/{REPO}/releases/tag/{tag}")
    print(f"发布命令总用时：{time.monotonic() - started:.1f}s")
    print("main 的完整 CI 已确认通过；安装演练与完整回下载按改动需要执行。")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        sys.exit(f"✗ {exc}")
