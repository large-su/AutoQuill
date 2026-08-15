# -*- mode: python ; coding: utf-8 -*-
# AutoQuill V4 正式版打包：onedir + launcher 冻结模式
#
# 产物：dist/AutoQuill/AutoQuill.exe（启动器，内置完整服务代码）
#   - datas 打包程序文件：webui/static（前端页面）、
#     llm_providers.example.json（首启复制为真实配置的模板）、
#     model_pricing.json（只读定价表）
#   - 敏感/用户数据不进包：llm_providers.json（真实 API key）、
#     browser_state.json（登录态）、webui_model.json（运行时状态）、
#     data/、output/
#   - playwright 驱动由 pyinstaller-hooks-contrib 的 hook 打包；
#     浏览器本体用系统 Edge（browser_adapter 经 executable_path 直连）
#
# 构建：python -m PyInstaller build/AutoQuill.spec --noconfirm

a = Analysis(
    ['../tools/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('../webui/static', 'webui/static'),
        ('../config/llm_providers.example.json', 'config'),
        ('../config/model_pricing.json', 'config'),
        ('../config/builtin_general_profile.json', 'config'),
    ],
    hiddenimports=[
        # web_drivers/__init__.py 用 importlib.import_module 动态加载驱动
        # （_DRIVER_REGISTRY），PyInstaller 静态分析抓不到 —— 漏了会导致
        # 安装版 Web 通道报 No module named 'web_drivers.deepseek'
        'web_drivers.deepseek',
        # uvicorn 用动态 import 加载内部实现，静态分析抓不全
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # pywebview（独立窗口）：平台后端动态加载；clr 为 pythonnet
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'webview.util',
        'clr',
        'bottle',
        'proxy_tools',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 兜底排除：OCR/坐标时代依赖已从代码移除（requirements 同步删除），
    # 万一 import 图残留引用也能强制不打包
    excludes=[
        'matplotlib', 'pandas', 'tkinter',
        'cv2', 'onnxruntime', 'numpy', 'PIL', 'shapely', 'pyclipper',
        'rapidocr_onnxruntime', 'rapidocr',
        'pyautogui', 'pyscreeze', 'pygetwindow', 'pyperclip',
        'aizex', 'ocr_utils', 'perception', 'a11y_probe', 'image_gen',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoQuill',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AutoQuill',
)
