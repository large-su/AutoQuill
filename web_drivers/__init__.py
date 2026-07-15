# ============================================================
# web_drivers/__init__.py — 驱动工厂
#
# 根据 config.WEB_DRIVER_NAME 返回对应的驱动单例。
# 新增 LLM 网站时在此注册即可。
# ============================================================

_driver_instance = None

# ---- 驱动注册表 ----
# 格式：{ 名称: (模块路径, 类名) }
# 新增网站时在此添加一行
_DRIVER_REGISTRY = {
    "DeepSeek": ("web_drivers.deepseek", "DeepSeekDriver"),
    "Aizex":    ("web_drivers.aizex",    "AizexDriver"),
}


def get_driver():
    """获取当前 Web 驱动实例（单例）"""
    global _driver_instance
    if _driver_instance is None:
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
        _driver_instance = driver_cls(WEB_DRIVERS[WEB_DRIVER_NAME])

    return _driver_instance


def reset_driver():
    """关闭当前驱动会话并重置单例"""
    global _driver_instance
    if _driver_instance:
        _driver_instance.close_session()
    _driver_instance = None


def create_driver():
    """
    创建一个新的驱动实例（非单例）。

    供并行场景使用：每个 tab 需要独立的 driver 实例以隔离 _session_url
    等状态。不修改 _driver_instance 单例，与 get_driver() 互不影响。
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
