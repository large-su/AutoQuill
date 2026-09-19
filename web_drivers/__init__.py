# ============================================================
# web_drivers/__init__.py — 驱动工厂
#
# get_driver()   → 单例（串行流程用，复用同一会话）
# create_driver() → 新实例（并行调度每 slot 一个，各自独立页面）
# 新增 LLM 网站时在 _DRIVER_REGISTRY 注册即可。
#
# 注：旧 OCR/坐标驱动（Aizex 等）已于 V4.0.4 整体归档（见 archive/），
# 主链路仅剩 DOM 驱动。
# ============================================================

_driver_instance = None

# ---- 驱动注册表 ----
# 格式：{ 名称: (模块路径, 类名) }
# 新增网站时在此添加一行（并同步在 config.WEB_DRIVERS 加配置条目）
_DRIVER_REGISTRY = {
    "DeepSeek": ("web_drivers.deepseek", "DeepSeekDriver"),
    "Doubao": ("web_drivers.doubao", "DoubaoDriver"),
}


def _impl_available(name):
    """该名称是否已有实现（config 层做切换前校验用，避免循环导入）。"""
    return name in _DRIVER_REGISTRY


def _current_driver_module():
    """当前 WEB_DRIVER_NAME 对应的驱动模块（惰性 import）。"""
    from config import WEB_DRIVER_NAME
    if WEB_DRIVER_NAME not in _DRIVER_REGISTRY:
        raise ValueError(f"未实现的 Web 驱动：{WEB_DRIVER_NAME}"
                         f"（可用 {list(_DRIVER_REGISTRY)}）")
    import importlib
    module_path, _cls = _DRIVER_REGISTRY[WEB_DRIVER_NAME]
    return importlib.import_module(module_path)


def web_llm_logged_in():
    """当前网页版大模型是否已登录（按当前驱动分发）。"""
    try:
        return bool(_current_driver_module().web_llm_logged_in())
    except Exception:
        return False


def login_web_flow(timeout=300):
    """拉起可见 Edge 引导登录当前网页版大模型（按当前驱动分发）。"""
    mod = _current_driver_module()
    return mod.login_web_flow(timeout=timeout)


def get_driver():
    """获取当前 Web 驱动实例（单例）"""
    global _driver_instance
    if _driver_instance is None:
        _driver_instance = create_driver()

    return _driver_instance


def create_driver():
    """创建一个新的驱动实例（非单例）。

    并行场景每 slot 一个实例（web_drivers/parallel.py）；
    不修改 _driver_instance 单例，与 get_driver() 互不影响。
    惰性：构造时不启动浏览器，首次页面交互才开。
    """
    from config import WEB_DRIVER_NAME, WEB_DRIVERS
    import importlib

    if WEB_DRIVER_NAME not in WEB_DRIVERS:
        raise ValueError(f"未知的 Web 驱动：{WEB_DRIVER_NAME}，"
                         f"可用：{list(WEB_DRIVERS.keys())}")

    if WEB_DRIVER_NAME not in _DRIVER_REGISTRY:
        raise ValueError(f"未实现的 Web 驱动：{WEB_DRIVER_NAME}")

    module_path, class_name = _DRIVER_REGISTRY[WEB_DRIVER_NAME]
    module = importlib.import_module(module_path)
    driver_cls = getattr(module, class_name)
    return driver_cls(WEB_DRIVERS[WEB_DRIVER_NAME])


def reset_driver(delete_session=True):
    """关闭当前驱动会话并重置单例。

    delete_session=True（默认）：先删除本次网页会话再关页——单链路
    完成后在网页端把该会话删掉（减少会话堆积触发平台风控）。
    仅删除本驱动自己创建/使用过的会话，绝不误删用户已有会话。
    """
    global _driver_instance
    if _driver_instance:
        if delete_session:
            try:
                _driver_instance.delete_current_session()
            except Exception:
                pass
        _driver_instance.close_session()
    _driver_instance = None
