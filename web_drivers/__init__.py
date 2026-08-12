# ============================================================
# web_drivers/__init__.py — 驱动工厂
#
# get_driver()   → 单例（串行流程用，复用同一会话）
# create_driver() → 新实例（并行调度每 slot 一个，各自独立页面）
# 新增 LLM 网站时在 _DRIVER_REGISTRY 注册即可。
#
# 注：旧 OCR/坐标驱动（Aizex 等）已迁至 web_drivers/legacy/，
# 仅 --image-gen 的旧 Aizex 驱动使用，不再注册到此工厂。
# ============================================================

_driver_instance = None

# ---- 驱动注册表 ----
# 格式：{ 名称: (模块路径, 类名) }
# 新增网站时在此添加一行
_DRIVER_REGISTRY = {
    "DeepSeek": ("web_drivers.deepseek", "DeepSeekDriver"),
}


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


def reset_driver():
    """关闭当前驱动会话并重置单例"""
    global _driver_instance
    if _driver_instance:
        _driver_instance.close_session()
    _driver_instance = None
