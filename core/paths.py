# ============================================================
# core/paths.py — 程序/数据目录解析（正式版打包支持）
#
# 原则：只读程序文件走 PROGRAM_ROOT，可写用户数据走 DATA_ROOT。
#   - 源码运行态：两者相同（项目根）——行为与 V3.x 完全一致
#   - 打包运行态（sys.frozen）：PROGRAM_ROOT = 打包程序文件目录
#     （PyInstaller 6.x onedir 为 _internal/，即 sys._MEIPASS；只读），
#     DATA_ROOT = %APPDATA%/AutoQuill（用户可写）
#   - AQ_DATA_DIR 环境变量可强制指定 DATA_ROOT（测试模拟冻结态 /
#     便携模式）
#
# 用法：
#   from core.paths import data, program, migrate_legacy_data
#   data("config", "llm_providers.json")   # 可写文件
#   program("webui", "static", "index.html")  # 只读程序文件
# ============================================================

import os
import shutil
import sys


def _is_frozen():
    return bool(getattr(sys, "frozen", False))


def _program_root():
    """程序根：冻结态 = 打包程序文件目录（PyInstaller 6.x onedir 把
    代码与 datas 都放进 _internal/，sys._MEIPASS 指向它；兜底 exe 所在
    目录）；源码态 = 项目根。"""
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _legacy_root():
    """旧版解压目录（升级迁移数据源）：冻结态 = exe 所在目录——V3 解压包
    的 data/config 与启动器同级，V4 安装目录本身是全新的；源码态 = 项目根。"""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return PROGRAM_ROOT


def _data_root():
    """数据根：AQ_DATA_DIR 环境变量 > 冻结态 %APPDATA%/AutoQuill >
    源码态项目根（开发零感知）。"""
    env = os.environ.get("AQ_DATA_DIR", "").strip()
    if env:
        return os.path.abspath(env)
    if _is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AutoQuill")
    return _program_root()


PROGRAM_ROOT = _program_root()
DATA_ROOT = _data_root()


def program(*parts):
    """只读程序文件路径（打包进安装目录）。"""
    return os.path.join(PROGRAM_ROOT, *parts)


def data(*parts):
    """可写用户数据路径（源码态 = 项目根，冻结态 = %APPDATA%）。"""
    return os.path.join(DATA_ROOT, *parts)


def ensure_data_dirs():
    for rel in ("data", "output", "logs", "config"):
        os.makedirs(data(rel), exist_ok=True)


def ensure_provider_file():
    """安装态首启：DATA_ROOT/config/llm_providers.json 缺失时从
    example 复制（含占位 key），保证服务可启动、引导页可填 key。

    源码态（无 AQ_DATA_DIR）保持原「缺失即报错」的响亮提示，
    不自动复制——开发人员需要明确感知配置未就绪。
    返回文件路径；无 example 可复制时返回 None。
    """
    if not _is_frozen() and "AQ_DATA_DIR" not in os.environ:
        return None
    dst = data("config", "llm_providers.json")
    if os.path.exists(dst):
        return dst
    src = program("config", "llm_providers.example.json")
    if os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return dst
    return None


def migrate_legacy_data():
    """旧版（V3.x 解压目录）数据 → DATA_ROOT，一次性迁移。

    源码态 DATA_ROOT == PROGRAM_ROOT → 无操作。
    冻结态：若 DATA_ROOT 尚无数据而 exe 旁（旧解压目录）存在
    data、output、logs 或关键配置文件（升级安装场景），整目录/
    文件复制过去。
    迁移失败不抛异常（返回 error 描述，webui 引导页可提示重试），
    绝不阻塞启动。
    """
    if DATA_ROOT == PROGRAM_ROOT:
        return {"migrated": False, "error": None}
    ensure_provider_file()
    moved = 0
    legacy = _legacy_root()
    for rel in ("data", "output", "logs"):
        src, dst = os.path.join(legacy, rel), data(rel)
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                shutil.copytree(src, dst)
                moved += 1
            except OSError as exc:
                return {"migrated": False, "error": f"{rel}: {exc}"}
    for rel in ("config/llm_providers.json", "config/browser_state.json",
                "config/webui_model.json"):
        src, dst = os.path.join(legacy, rel), data(rel)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                moved += 1
            except OSError as exc:
                return {"migrated": False, "error": f"{rel}: {exc}"}
    return {"migrated": bool(moved), "error": None}
