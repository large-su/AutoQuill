# AutoQuill 本地服务网络端口唯一来源。
# webui/server.py 与 tools/launcher.py 共用，避免魔数散落多处。
import os

# Isolated packaged update tests use a private data root and port.
WEB_PORT = int(os.environ.get("AQ_WEB_PORT") or 8787)
if not 1 <= WEB_PORT <= 65535:
    raise ValueError("AQ_WEB_PORT must be between 1 and 65535")
