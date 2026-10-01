#!/usr/bin/env python3
"""Destructive-in-a-private-temp-dir smoke test for a frozen Windows update.

It never downloads a release and never targets the user's installation.  Supply a
new, already-built ``dist/AutoQuill`` and either an already-built old frozen
copy or a source snapshot, then upgrades that copy with a private Inno installer.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_VERSION = "5.0.4"


def say(message: str) -> None:
    print("[verify-update] " + message, flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, method: str = "GET", timeout: float = 5) -> dict:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("local API request failed: %s" % exc) from exc
    require(isinstance(payload, dict), "local API returned non-object JSON")
    return payload


def wait_until(description: str, predicate, seconds: int = 60):
    deadline = time.monotonic() + seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # service is expected to disappear during restart
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError("timed out waiting for %s%s" %
                       (description, (": " + last_error) if last_error else ""))


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    # Windows os.kill(pid, 0) TERMINATES the process; use a query handle instead.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel.CloseHandle(handle)


def tracked_snapshot(destination: Path) -> None:
    """Copy only Git-tracked files, so no local configuration or user data leaks in."""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            check=True, capture_output=True)
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = Path(os.fsdecode(raw_name))
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # The current host template is intentionally copied even if it has not yet
    # been committed when this verifier is invoked locally.
    host = ROOT / "core" / "update_host.ps1"
    target_host = destination / "core" / "update_host.ps1"
    target_host.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(host, target_host)


def replace_version(snapshot: Path, version: str) -> None:
    path = snapshot / "core" / "version.py"
    text = path.read_text(encoding="utf-8")
    require('VERSION = "' in text, "snapshot version source has unexpected format")
    import re
    updated, count = re.subn(r'VERSION\s*=\s*"[^"]+"', 'VERSION = "%s"' % version,
                             text, count=1)
    require(count == 1, "could not set snapshot version")
    path.write_text(updated, encoding="utf-8")


def build_old_fixture(snapshot: Path, build_root: Path, old_version: str) -> Path:
    say("building isolated old frozen fixture")
    dist_root = build_root / "dist"
    work_root = build_root / "work"
    command = [sys.executable, "-m", "PyInstaller", "installer/AutoQuill.spec", "--noconfirm",
               "--distpath", str(dist_root), "--workpath", str(work_root)]
    subprocess.run(command, cwd=snapshot, check=True)
    result = dist_root / "AutoQuill"
    require((result / "AutoQuill.exe").is_file(), "old frozen AutoQuill.exe was not built")
    info = result / "_internal" / "build_info.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"version": old_version}, indent=2), encoding="utf-8")
    return result


def inno_escape(path: Path) -> str:
    return str(path).replace('"', '""')


def compile_private_installer(target_dist: Path, output: Path, target_version: str) -> Path:
    iscc = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    require(iscc.is_file(), "Inno Setup 6 ISCC.exe was not found at %s" % iscc)
    language = ROOT / "installer" / "languages" / "ChineseSimplified.isl"
    icon = ROOT / "assets" / "AutoQuill.ico"
    require(language.is_file() and icon.is_file(), "production Inno language/icon assets are missing")
    output.mkdir(parents=True, exist_ok=True)
    app_id = "{{%s}" % str(uuid.uuid4()).upper()
    iss = output / "private-update.iss"
    # No icons, tasks, run section, registry uninstall entry, or user-facing target.
    iss.write_text("""#define MyAppName \"AutoQuill Update Smoke\"
#define MyAppVersion \"%s\"
[Setup]
AppId=%s
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName=\"%s\\unused-default\"
DisableDirPage=yes
DisableProgramGroupPage=yes
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
OutputDir=\"%s\"
OutputBaseFilename=private-update
SetupIconFile=\"%s\"
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no
RestartApplications=no
[Languages]
Name: \"chinesesimplified\"; MessagesFile: \"%s\"
[Files]
Source: \"%s\\*\"; DestDir: \"{app}\"; Flags: recursesubdirs ignoreversion
""" % (target_version, app_id, inno_escape(output), inno_escape(output),
         inno_escape(icon), inno_escape(language), inno_escape(target_dist)), encoding="utf-8")
    say("compiling private Inno installer")
    subprocess.run([str(iscc), str(iss)], cwd=output, check=True)
    installer = output / "private-update.exe"
    require(installer.is_file(), "private Inno installer was not generated")
    return installer


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def status_url(port: int) -> str:
    return "http://127.0.0.1:%d/api/update/status" % port


def wait_status(port: int, version: str, install_dir: Path, forbidden_pid: int = 0) -> dict:
    def ready():
        status = request_json(status_url(port))
        if (status.get("running_version") == version and
                Path(str(status.get("running_install_dir") or "")).resolve() == install_dir.resolve() and
                int(status.get("running_pid") or 0) != forbidden_pid):
            return status
        return None
    return wait_until("local service v%s" % version, ready)


def terminate_private(pid: int, install_dir: Path) -> None:
    """Fallback only after querying the live executable's exact private directory."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1001, False, pid)
    if not handle:
        return
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            if Path(path.value).resolve() == (install_dir / "AutoQuill.exe").resolve():
                kernel.TerminateProcess(handle, 1)
    finally:
        kernel.CloseHandle(handle)


def quit_owned(port: int, pids: set[int], install_dir: Path) -> None:
    try:
        status = request_json(status_url(port), timeout=3)
        if Path(status.get("running_install_dir") or "").resolve() == install_dir.resolve():
            request_json("http://127.0.0.1:%d/api/launcher/quit" % port, "POST", timeout=3)
    except Exception:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
        time.sleep(0.25)
    for pid in list(pids):
        if pid_alive(pid):
            terminate_private(pid, install_dir)
    for pid in pids:
        wait_until("owned process %s exit" % pid, lambda p=pid: not pid_alive(p), seconds=5)


def run(target_dist: Path, report: Path | None, old_dist: Path | None = None,
        old_version: str | None = None) -> None:
    target_dist = target_dist.resolve()
    require((target_dist / "AutoQuill.exe").is_file(), "target dist has no AutoQuill.exe: %s" % target_dist)
    manifest = target_dist / "_internal" / "build_info.json"
    require(manifest.is_file(), "target dist is missing _internal/build_info.json")
    target_version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    require(isinstance(target_version, str) and target_version, "target dist manifest has no version")
    if old_dist is not None:
        require(old_version is not None, "--old-version is required with --old-dist")
        old_dist = old_dist.resolve()
        require((old_dist / "AutoQuill.exe").is_file(), "old dist has no AutoQuill.exe: %s" % old_dist)
        old_manifest = old_dist / "_internal" / "build_info.json"
        require(old_manifest.is_file(), "old dist is missing _internal/build_info.json")
        old_manifest_version = json.loads(old_manifest.read_text(encoding="utf-8")).get("version")
        require(old_manifest_version == old_version,
                "old dist must contain version %s, got %r" % (old_version, old_manifest_version))
    else:
        old_version = old_version or OLD_VERSION
    results: dict[str, object] = {"target": str(target_dist), "target_version": target_version}
    owned: set[int] = set()
    port = free_port()
    temporary = tempfile.TemporaryDirectory(prefix="aq-installed-update-")
    install_dir = Path(temporary.name) / "安装 空格 'private'"
    try:
        raw = temporary.name
        if raw:
            root = Path(raw)
            if old_dist is None:
                snapshot, build_root = root / "source", root / "build"
                snapshot.mkdir(); build_root.mkdir()
                tracked_snapshot(snapshot)
                replace_version(snapshot, old_version)
                old_dist_for_install = build_old_fixture(snapshot, build_root, old_version)
            else:
                old_dist_for_install = old_dist
            installer = compile_private_installer(target_dist, root / "installer", target_version)
            install_dir = root / "安装 空格 'private'"
            shutil.copytree(old_dist_for_install, install_dir)
            data_dir = root / "private-data"
            data_dir.mkdir()
            sentinel = data_dir / "must-survive.txt"
            sentinel.write_text("private user data", encoding="utf-8")
            environment = os.environ.copy()
            environment.update({"AQ_DATA_DIR": str(data_dir), "AQ_WEB_PORT": str(port)})
            say("starting private old application")
            base = subprocess.Popen([str(install_dir / "AutoQuill.exe"), "--tray"], cwd=install_dir, env=environment)
            owned.add(base.pid)
            old_status = wait_status(port, old_version, install_dir)
            old_service = int(old_status["running_pid"])
            owned.add(old_service)
            stage = data_dir / "data" / "update" / "stage.json"
            stage.parent.mkdir(parents=True, exist_ok=True)
            staged_installer = stage.parent / installer.name
            shutil.copy2(installer, staged_installer)
            stage.write_text(json.dumps({"stage": "staged", "version": target_version,
                                         "installer": str(staged_installer), "sha256": sha256(staged_installer),
                                         "install_dir": str(install_dir), "log": str(stage.parent / "apply.log")}, indent=2), encoding="utf-8")
            say("calling real local update API")
            reply = request_json("http://127.0.0.1:%d/api/update/apply" % port, "POST")
            require(reply.get("ok") is True, "apply API refused update: %s" % reply.get("message"))
            wait_until("old launcher exit", lambda: not pid_alive(base.pid))
            wait_until("old service exit", lambda: not pid_alive(old_service))
            new_status = wait_status(port, target_version, install_dir, old_service)
            new_service = int(new_status["running_pid"])
            owned.add(new_service)
            wait_until("completed update state", lambda: (json.loads(stage.read_text(encoding="utf-8")) if stage.exists() else {}).get("stage") == "done")
            final_state = json.loads(stage.read_text(encoding="utf-8"))
            new_launcher = int(final_state.get("restarted_pid") or 0)
            require(new_launcher > 0, "completed update has no restarted launcher PID")
            owned.add(new_launcher)
            require(not staged_installer.exists(), "installer was not removed after successful update")
            require(sentinel.read_text(encoding="utf-8") == "private user data", "private user-data sentinel changed")
            installed = json.loads((install_dir / "_internal" / "build_info.json").read_text(encoding="utf-8"))
            require(installed.get("version") == target_version, "installed manifest is not target version")
            results.update(old_version=old_status["running_version"], old_launcher=base.pid,
                           old_service=old_service, new_service=new_service, new_launcher=new_launcher,
                           state=final_state.get("stage"))
            say("calling real local restart API")
            reply = request_json("http://127.0.0.1:%d/api/update/restart" % port, "POST")
            require(reply.get("ok") is True, "restart API refused: %s" % reply.get("message"))
            restarted = wait_status(port, target_version, install_dir, new_service)
            require(int(restarted["running_pid"]) != new_service, "restart retained old service PID")
            wait_until("previous launcher exit after restart", lambda: not pid_alive(new_launcher))
            wait_until("previous service exit after restart", lambda: not pid_alive(new_service))
            wait_until("restart completion", lambda: json.loads(stage.read_text(encoding="utf-8")).get("stage") == "done")
            results["restart_service"] = int(restarted["running_pid"])
            owned.add(results["restart_service"])
            restart_state = json.loads(stage.read_text(encoding="utf-8"))
            if restart_state.get("restarted_pid"):
                owned.add(int(restart_state["restarted_pid"]))
            # An old updater may have left a failed/applying record before a manual bootstrap.
            restart_state.update(stage="failed", operation="update", host_pid=0,
                                 version=old_version, error="old interrupted update")
            stage.write_text(json.dumps(restart_state), encoding="utf-8")
            reconciled = request_json(status_url(port))
            require(reconciled.get("stage") == "done" and not reconciled.get("error"),
                    "obsolete update failure was not reconciled against the running installed version")
            results["manual_bootstrap_state"] = reconciled["stage"]
            say("PASS: update and restart stayed inside the private temporary installation")
    except Exception as exc:
        # The optional report remains outside TemporaryDirectory and is therefore
        # the durable, secret-free diagnostic after private artifacts are removed.
        results["error"] = str(exc)
        for name in ("apply.log", "stage.json"):
            path = Path(temporary.name) / "private-data" / "data" / "update" / name
            if path.exists():
                results[name] = path.read_text(encoding="utf-8-sig", errors="replace")[-3000:]
        raise
    finally:
        # Quit first; only PIDs obtained from this temporary run are ever waited on.
        try:
            instance = Path(temporary.name) / "private-data" / "config" / "launcher_instance.json"
            if instance.exists():
                launcher_pid = int(json.loads(instance.read_text(encoding="utf-8")).get("pid") or 0)
                if launcher_pid > 0:
                    owned.add(launcher_pid)
            quit_owned(port, owned, install_dir)
        except Exception as exc:
            results["cleanup_error"] = str(exc)
        try:
            temporary.cleanup()
        except OSError as exc:
            results["cleanup_error"] = str(exc)
        if report:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dist", type=Path, default=ROOT / "dist" / "AutoQuill")
    parser.add_argument("--old-dist", type=Path,
                        help="optional already-built old frozen dist; requires --old-version")
    parser.add_argument("--old-version",
                        help="version expected in --old-dist (or source fallback)")
    parser.add_argument("--report", type=Path, help="optional JSON result path (no secrets are recorded)")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This verifier requires Windows, a frozen target, and Inno Setup 6.")
    try:
        run(args.target_dist, args.report, args.old_dist, args.old_version)
    except Exception as exc:
        say("FAILED: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
