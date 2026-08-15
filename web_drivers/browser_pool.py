# ============================================================
# web_drivers/browser_pool.py — 浏览器基础设施（共享单例 + 取消钩子 + 有界交互）
#
# 架构位置：Layer 3 (web_drivers) — 只依赖 config / 标准库，
# 禁止 import applications。浏览器创建工厂由应用层注册：
#   applications/zhihu_story/browser_adapter 模块级调用
#   register_browser_factory()（工厂内引用 ZhihuBrowser 模块全局，
#   mock.patch.object(mod, "ZhihuBrowser") 拦截链不断）。
#
# 职责：
#   - 全局共享浏览器单例（profile 锁下懒启动，线程安全）
#   - 用户取消钩子（Web 控制台「停止」按钮）
#   - safe_evaluate：Promise.race 自限时哨兵的有界页面交互（唯一实现，
#     原 web_drivers/base.py 与 browser_adapter 两处合并于此）
# ============================================================

import logging
import threading

log = logging.getLogger(__name__)

# 页面交互超时（毫秒）：所有 evaluate 必须有界。
# 历史事故：风控页/加载中页面会让 evaluate 无限阻塞（进程挂死数
# 分钟无日志），任何交互都不允许无界等待。
EVAL_TIMEOUT = 15000


class WorkflowCancelled(Exception):
    """用户取消操作（Web 控制台「停止」按钮）。"""


_cancel_hook = None


def set_cancel_hook(fn):
    """设置取消检查钩子；fn() 返回 True 时后续浏览器操作抛 WorkflowCancelled。

    仅 Web 控制台设置（stop 置标志）；CLI 模式下无 hook，零影响。
    检查只允许在 Python 层（浏览器阻塞调用自带超时），绝不能跨线程
    注入异常——那会破坏 Playwright 协议层导致 close 挂起。"""
    global _cancel_hook
    _cancel_hook = fn


def _check_cancel():
    if _cancel_hook is not None and _cancel_hook():
        raise WorkflowCancelled("已由用户停止")


def safe_evaluate(page, js, *args, timeout=EVAL_TIMEOUT):
    """执行页面 JS，失败返回 None；JS 内部带自限时哨兵。

    所有页面交互都必须走这里。Playwright 1.62 的 evaluate 不支持
    timeout 参数（协议层无超时），且 sync API 有线程亲和性（不能
    从其他线程调用）。对策：把调用 JS 包进 Promise.race 自限时
    哨兵——页面主线程存活时，任何挂起的 evaluate（fetch 不返回、
    慢导航等）都会在 timeout 后被哨兵截断返回 None，不阻塞流程。
    渲染进程彻底卡死（极端风控）时此层无效，由调用方（E2E runner）
    的进程级看门狗兜底。"""
    wrapped = (
        "async function() {"
        "  const _fn = " + js + ";"
        "  const _timeout = new Promise(_r => setTimeout("
        f"() => _r({{__aq_timeout__: true}}), {int(timeout)}));"
        "  const _result = await Promise.race("
        "    [Promise.resolve(_fn.apply(null, arguments)), _timeout]);"
        "  if (_result && _result.__aq_timeout__) return null;"
        "  return _result;"
        "}"
    )
    _check_cancel()
    try:
        return page.evaluate(wrapped, *args)
    except WorkflowCancelled:
        raise
    except Exception as exc:
        log.warning("browser_pool: evaluate 失败：%s", exc)
        return None


# ---------------- 共享浏览器单例（工厂由应用层注册） ----------------

_shared_browser = None
_browser_lock = threading.Lock()  # 懒启动串行化：并发 get_browser 不再互抢 profile
_factory = None


def register_browser_factory(fn):
    """注册浏览器创建工厂 fn(headless) -> 未启动的浏览器实例。

    工厂返回未启动实例：get_browser 在锁内 start() 成功后才落盘
    （失败实例绝不复用）；登录引导等独立实例用 with create_browser()
    经 __enter__ 启动。未注册时调用显式报错，防止层间隐式依赖。"""
    global _factory
    _factory = fn


def create_browser(headless):
    """经注册工厂创建浏览器实例（登录引导等独立实例用）。"""
    if _factory is None:
        raise RuntimeError(
            "浏览器工厂未注册：请先 import "
            "applications.zhihu_story.browser_adapter"
            "（browser_pool 不依赖 applications，工厂由应用层注册）")
    return _factory(headless)


def get_browser():
    """获取全局共享浏览器（懒启动，线程安全）。

    headless 每次动态读取 config.BROWSER_HEADLESS——Web 控制台切换
    「调试/工作模式」后下一次任务启动即生效。
    锁内先 start() 成功才落盘 _shared_browser：并发调用时后到的线程
    不会拿半初始化实例（线上：并发启动互抢同一 profile，坏实例
    context=None 永久复用于登录引导 → 'NoneType' new_page）。"""
    global _shared_browser
    with _browser_lock:
        if _shared_browser is None or _shared_browser.context is None:
            from config import BROWSER_HEADLESS
            candidate = create_browser(BROWSER_HEADLESS)
            candidate.start()  # 失败抛异常，_shared_browser 保持 None 可重试
            _shared_browser = candidate
            log.info("browser_pool: 浏览器模式：%s",
                     "无头（工作模式）" if BROWSER_HEADLESS
                     else "前台（调试模式）")
        return _shared_browser


def close_shared_browser():
    global _shared_browser
    with _browser_lock:
        if _shared_browser is not None:
            _shared_browser.close()
            _shared_browser = None
