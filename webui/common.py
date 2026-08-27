# ============================================================
# webui/common.py — 各路由域共享的守卫/工具（P0 拆分下沉件）
# ============================================================

from fastapi import HTTPException
from pathlib import Path
import logging
import os

log = logging.getLogger(__name__)

def _llm_configured():
    """当前服务商/模型的 API Key 是否就绪（非空、非占位符、非掩码）。"""
    from config import LLM_MODEL_ID, LLM_PROVIDER, _load_provider_config
    try:
        key = (_load_provider_config(LLM_PROVIDER, LLM_MODEL_ID)
               .get("apiKey") or "").strip()
    except Exception:
        return False
    return bool(key) and key != "密" and not key.startswith("sk-your-")


def _require_llm_ready(detail="API Key 未配置或仍是占位符"):
    """API 通道硬校验：未就绪抛 400（切换通道 / 测试连接用）。"""
    if not _llm_configured():
        raise HTTPException(400, detail)
    return True


def _require_zhihu_url(url):
    """采集 URL 域名白名单：仅接受 https 的 zhihu.com 及其子域。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "zhihu.com"
                                        or host.endswith(".zhihu.com")):
        raise HTTPException(400, "采集地址仅支持知乎域名"
                                "（https://*.zhihu.com/...）")



def _current_log_file():
    """定位当前进程的业务日志文件（main.py basicConfig 的 FileHandler）。"""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            p = Path(h.baseFilename)
            if p.name.startswith("autoquill_"):
                return p
    return None
