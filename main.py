# ============================================================
# AutoQuill v2.3 — 统一入口
#
# 用法：
#   python main.py                 批量模式（默认）：收集素材 → 生成 → 发布 → 元学习
#   python main.py --single        传统模式：逐轮生成即发布
#   python main.py --calibrate     校准坐标
#   python main.py --test-ocr      测试 OCR
#   python main.py --debug-ocr-region  进入回答页并保存 OCR 区域标注图
#   python main.py --probe-a11y [--url URL]  只读导出当前 Edge 的无障碍树
#   python main.py --test-api      测试 API 连接
#   python main.py --image-gen     图像生成模式（Aizex 绘图）
#   python main.py --use-meta      批量/传统模式中注入元知识到生成 prompt
#   python main.py --no-meta       强制不注入元知识（覆盖 config 默认值）
#
# 架构分层：
#   applications/    → 应用层（知乎故事 zhihu_story、图像生成 image_gen）
#   core/            → 结构化重构核心（perception 感知 / action 动作 / mind 决策）
#   workflows/       → 工作流编排（知乎批量、图像生成）
#   web_drivers/     → LLM 网站驱动（DeepSeek、Aizex 等）
#
# 基础模块：
#   main.py              → 入口（DPI、日志、CLI 分发）
#   desktop_utils.py     → 桌面操作（浏览器、窗口、坐标、进度面板）
#   ocr_utils.py         → 视觉感知（OCR 识别、文字定位、图标匹配）
#   llm_api.py           → LLM API 调用（流式/非流式）
#   llm_token_tracker.py → API 模式 Token 用量追踪
#   config.py / config/  → 全局配置（主配置 + 分层配置目录）
#
# 进化系统：
#   kb_manager.py    → 知识库管理（配方积累、参考文章）
#   meta_learner.py  → 元学习（评分回写、入池、检测蒸馏、story_postmortem）
#   rich_progress.py  → Rich 终端进度面板
# ============================================================

import ctypes

# DPI 感知（Windows 高分屏适配，必须在 pyautogui 之前设置）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import pyautogui
import sys
import os
import time
import logging
from datetime import datetime

from config import (
    PYAUTOGUI_PAUSE, QUESTION_SELECT_MODE, LLM_MODE,
    ENABLE_STORY_FILTER, STORY_MATERIAL_MODE,
    DEFAULT_BATCH_GENERATE_COUNT, DEFAULT_BATCH_PUBLISH_COUNT,
    MAX_TOTAL_ATTEMPTS, LLM_API_KEY, WEB_DRIVER_NAME,
    WAIT_BETWEEN_CYCLES,
    random_delay
)

# ============================================================
# 基础设置
# ============================================================

pyautogui.FAILSAFE = True
pyautogui.PAUSE = PYAUTOGUI_PAUSE

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            f"logs/autoquill_{datetime.now():%Y%m%d_%H%M%S}.log",
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ============================================================
# 交互辅助
# ============================================================

def _ask_int(prompt, default_value, minimum=1):
    """
    询问整数输入，支持默认值（直接回车）和重试。

    参数：
        prompt: 显示给用户的提示前缀（不含默认值提示）
        default_value: 直接回车时使用的默认值
        minimum: 允许的最小值（小于此值会要求重新输入）
    """
    while True:
        raw = input(f"  {prompt}（回车用默认 {default_value}）>> ").strip()
        if not raw:
            return default_value
        try:
            val = int(raw)
            if val >= minimum:
                return val
            print(f"    必须 ≥ {minimum}，请重新输入")
        except ValueError:
            print("    请输入整数")


def ask_batch_params():
    """
    交互式询问本次批量任务的参数。

    返回：
        (gen_count, pub_count, rounds)
        rounds: [{"round": 1, "gen_count": 20, "pub_count": 12}, ...]
    """
    print("\n  ── 本次批量任务参数 ──")
    try:
        from config import (
            BATCH_AUTO_GENERATE_COUNT,
            BATCH_GENERATE_REDUNDANCY_RATIO,
            BATCH_GENERATE_MIN_EXTRA,
            BATCH_ROUND_SPLIT_ENABLE,
            BATCH_MAX_PUBLISH_PER_ROUND,
        )
    except ImportError:
        BATCH_AUTO_GENERATE_COUNT = False
        BATCH_GENERATE_REDUNDANCY_RATIO = 0.0
        BATCH_GENERATE_MIN_EXTRA = 0
        BATCH_ROUND_SPLIT_ENABLE = False
        BATCH_MAX_PUBLISH_PER_ROUND = 0

    def _auto_gen_count(n):
        import math
        ratio_count = math.ceil(n * (1 + BATCH_GENERATE_REDUNDANCY_RATIO))
        extra_count = n + max(0, BATCH_GENERATE_MIN_EXTRA)
        return max(n, ratio_count, extra_count)

    def _split_even(total, max_per_round):
        import math
        if not max_per_round or total <= max_per_round:
            return [total]
        round_count = math.ceil(total / max_per_round)
        base = total // round_count
        rem = total % round_count
        return [base + (1 if i < rem else 0)
                for i in range(round_count)]

    if BATCH_AUTO_GENERATE_COUNT:
        pub_count = _ask_int("要发布多少篇故事？",
                             DEFAULT_BATCH_PUBLISH_COUNT)
        pub_rounds = _split_even(
            pub_count,
            BATCH_MAX_PUBLISH_PER_ROUND
            if BATCH_ROUND_SPLIT_ENABLE else 0
        )
        rounds = []
        for i, pub_n in enumerate(pub_rounds, start=1):
            rounds.append({
                "round": i,
                "gen_count": _auto_gen_count(pub_n),
                "pub_count": pub_n,
            })
        gen_count = sum(r["gen_count"] for r in rounds)
        if len(rounds) > 1:
            plan = " / ".join(str(r["pub_count"]) for r in rounds)
            print(f"  → 总发布 {pub_count} 篇，单轮上限 "
                  f"{BATCH_MAX_PUBLISH_PER_ROUND} 篇，"
                  f"拆分为 {len(rounds)} 轮：{plan}")
            for r in rounds:
                print(f"     第 {r['round']} 轮：生成 {r['gen_count']} 篇 "
                      f"→ 择优发布 {r['pub_count']} 篇")
        else:
            print(f"  → 自动计算生成数：{gen_count} 篇"
                  f"（发布 {pub_count} 篇，冗余 "
                  f"{BATCH_GENERATE_REDUNDANCY_RATIO:.0%}，"
                  f"至少多 {BATCH_GENERATE_MIN_EXTRA} 篇）")
    else:
        gen_count = _ask_int("要生成多少篇故事？",
                             DEFAULT_BATCH_GENERATE_COUNT)
        pub_count = _ask_int("要发布多少篇故事？",
                             DEFAULT_BATCH_PUBLISH_COUNT)
        rounds = [{
            "round": 1,
            "gen_count": gen_count,
            "pub_count": pub_count,
        }]

    print()
    if len(rounds) > 1:
        print(f"  → 合计生成 {gen_count} 篇 → 分轮择优发布 {pub_count} 篇")
    elif pub_count > gen_count:
        print(f"  ⚠ 发布数 {pub_count} > 生成数 {gen_count}，"
              f"将按实际生成数发布，无需评分")
    elif pub_count == gen_count:
        print(f"  → 生成 {gen_count} 篇，全部发布（跳过评分）")
    else:
        print(f"  → 生成 {gen_count} 篇 → 评分择优发布 {pub_count} 篇")

    return gen_count, pub_count, rounds


# ============================================================
# 测试 OCR
# ============================================================

def _draw_region(draw, box, color, label):
    """在调试截图上画区域框。"""
    x1, y1, x2, y2 = [int(v) for v in box]
    draw.rectangle((x1, y1, x2, y2), outline=color, width=5)
    draw.rectangle((x1, max(0, y1 - 22), x1 + 260, y1), fill=color)
    draw.text((x1 + 6, max(0, y1 - 20)), label, fill="white")


def _ocr_image_lines(image):
    """对已截取的同一帧图像 OCR，供区域调试比较。"""
    import numpy as np
    from ocr_utils import _get_engine, _merge_to_lines

    result, _ = _get_engine()(np.array(image))
    if not result:
        return []
    result.sort(key=lambda item: (
        sum(p[1] for p in item[0]) / 4,
        sum(p[0] for p in item[0]) / 4
    ))
    return _merge_to_lines(result)


def debug_ocr_region_mode():
    """
    按真实采集流程进入一个知乎回答页，然后保存 OCR 区域可视化截图。

    红框：正文 OCR 区域
    绿框：正文区域下方的候选赞同栏

    同时对绿框原图、2 倍放大图、左侧赞同按钮图 OCR，并输出严格的赞同数解析结果。
    """
    from desktop_utils import load_coords, get_bounds, ensure_edge, focus_edge

    if not load_coords():
        print("  ❌ 请先 --calibrate")
        return

    from ocr_utils import _get_engine
    _get_engine()

    if not ensure_edge():
        print("  ❌ 无法启动 Edge 浏览器，请手动打开后重试。")
        return

    from workflows.zhihu import ZhihuWorkflow

    workflow = ZhihuWorkflow()
    print("  将按当前选题模式进入一个知乎问题页...")
    url = workflow.select_topic()
    print(f"  当前问题页：{url}")

    focus_edge()
    time.sleep(0.5)

    lx, rx, ty, by = get_bounds()
    sw, sh = pyautogui.size()
    from applications.zhihu_story.perception import (
        get_likes_action_bounds, get_upvote_button_bounds
    )

    content_box = (lx, ty, rx, by)
    likes_screen_bottom_box = get_likes_action_bounds(lx, rx, by)

    raw_img = pyautogui.screenshot()
    img = raw_img.copy()
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    _draw_region(draw, content_box, "red", "CONTENT OCR")
    _draw_region(draw, likes_screen_bottom_box, "green", "SCREEN BOTTOM LIKES")

    os.makedirs("screenshots", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_path = os.path.join("screenshots", f"ocr_regions_{stamp}.png")
    txt_path = os.path.join("screenshots", f"ocr_regions_{stamp}.txt")
    likes_raw_path = os.path.join("screenshots", f"ocr_likes_raw_{stamp}.png")
    upvote_raw_path = os.path.join("screenshots", f"ocr_upvote_raw_{stamp}.png")
    img.save(img_path)

    content_lines = _ocr_image_lines(raw_img.crop(content_box))
    likes_raw_img = raw_img.crop(likes_screen_bottom_box)
    likes_raw_img.save(likes_raw_path)
    likes_native_lines = _ocr_image_lines(likes_raw_img)
    likes_2x_img = likes_raw_img.resize(
        (likes_raw_img.width * 2, likes_raw_img.height * 2)
    )
    likes_2x_lines = _ocr_image_lines(likes_2x_img)
    upvote_button_box = get_upvote_button_bounds(lx, rx, by)
    upvote_raw_img = raw_img.crop(upvote_button_box)
    upvote_raw_img.save(upvote_raw_path)
    upvote_lines = _ocr_image_lines(upvote_raw_img)

    from applications.zhihu_story.perception import parse_likes_only
    likes_variants = [
        ("native", likes_native_lines),
        ("2x", likes_2x_lines),
        ("upvote_button", upvote_lines),
    ]

    sections = [
        ("CONTENT OCR", content_box, content_lines),
        ("SCREEN BOTTOM LIKES (native)", likes_screen_bottom_box,
         likes_native_lines),
        ("SCREEN BOTTOM LIKES (2x)", likes_screen_bottom_box,
         likes_2x_lines),
        ("UPVOTE BUTTON (native)", upvote_button_box, upvote_lines),
    ]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"url: {url}\n")
        f.write(f"screen: {sw}x{sh}\n\n")
        for name, box, lines in sections:
            f.write(f"[{name}] {tuple(int(v) for v in box)}\n")
            for i, line in enumerate(lines, 1):
                f.write(f"{i:02d}. {line}\n")
            f.write("\n")

        f.write("[LIKES PARSE]\n")
        for variant, lines in likes_variants:
            raw_text = " ".join(lines)
            likes = parse_likes_only(raw_text)
            f.write(f"{variant}: {likes if likes is not None else 'NOT_FOUND'}\n")
            f.write(f"  raw: {raw_text}\n")

    print(f"  ✓ 区域截图已保存：{img_path}")
    print(f"  ✓ OCR 文本已保存：{txt_path}")
    print(f"  ✓ 绿框原图已保存：{likes_raw_path}")
    print(f"  ✓ 赞同按钮原图已保存：{upvote_raw_path}")
    for variant, lines in likes_variants:
        likes = parse_likes_only(" ".join(lines))
        label = likes if likes is not None else "未识别"
        print(f"  · 绿框 {variant} OCR 赞同数：{label}")


def test_ocr_mode():
    from desktop_utils import load_coords, get_bounds, focus_edge

    if not load_coords():
        print("  ❌ 请先 --calibrate")
        return

    lx, rx, ty, by = get_bounds()
    print(f"\n  OCR 区域：({lx},{ty})~({rx},{by})")
    print("  请在 Edge 打开知乎问题页。")
    input("  按 Enter 测试...")
    focus_edge()
    time.sleep(0.5)

    from ocr_utils import ocr_region, _is_answer_end_marker
    lines, _ = ocr_region(lx, ty, rx, by)
    for i, l in enumerate(lines):
        marks = []
        if "关注问题" in l:
            marks.append("◀问题结束")
        if "人赞同" in l:
            marks.append("◀回答开始")
        if _is_answer_end_marker(l):
            marks.append("◀回答结束")
        m = f"  {'  '.join(marks)}" if marks else ""
        print(f"  {i+1:2d}. {l}{m}")
    print(f"\n  共 {len(lines)} 行")


def _get_cli_option(argv, option):
    """Return an optional CLI value without introducing a parser dependency."""
    try:
        index = argv.index(option)
    except ValueError:
        return None
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise ValueError(f"{option} 需要提供 URL")
    return argv[index + 1]


def probe_a11y_mode(argv):
    """Run the read-only UI Automation probe against the active Edge window."""
    try:
        target_url = _get_cli_option(argv, "--url")
    except ValueError as exc:
        print(f"  ✗ {exc}")
        print("  用法：python main.py --probe-a11y [--url https://www.zhihu.com/...] ")
        return

    from desktop_utils import focus_edge, navigate_to_url

    if not focus_edge():
        print("  ✗ 未找到可聚焦的 Edge 窗口，请先打开 Edge 后重试。")
        return

    if target_url:
        print(f"  通过浏览器常规导航打开：{target_url}")
        navigate_to_url(target_url)

    print("  正在只读枚举当前 Edge 的 Windows 无障碍树，不会点击或写入页面...")
    try:
        from applications.zhihu_story.a11y_probe import probe_foreground_edge
        result = probe_foreground_edge(source_url=target_url)
    except Exception as exc:
        log.error(f"UIA 探针失败：{exc}")
        print(f"  ✗ UIA 探针失败：{exc}")
        return

    print(f"  ✓ UIA 树已导出：{result['path']}")
    print(f"    共读取 {result['node_count']} 个元素"
          f"{'（达到安全上限，结果已截断）' if result['truncated'] else ''}")
    print("    回答状态：" + " | ".join(
        f"{name}={count}" for name, count in result['actions'].items()
    ))
    print(f"    标题候选={result['question_titles']}，"
          f"互动文本/值={result['interactions']}")


# ============================================================
# 图像生成模式
# ============================================================

def _run_image_gen():
    """--image-gen 入口：开浏览器 → Aizex 绘图 → 下载"""
    import os as _os

    print("  🎨 图像生成模式")

    from applications.image_gen.config import IMAGE_OUTPUT_DIR, SAMPLE_IMAGE_PROMPT
    from workflows.image_gen import ImageGenWorkflow
    workflow = ImageGenWorkflow()

    count = _ask_int("要生成几张图片？", 1)
    prompt_text = SAMPLE_IMAGE_PROMPT  # 测试阶段直接用示例提示词
    print(f"  使用示例提示词（{len(prompt_text)} 字符）")

    # ================================================================
    # 开浏览器 → Aizex 绘图 → 下载
    # ================================================================
    from desktop_utils import ensure_edge
    if not ensure_edge():
        print("  ❌ 无法启动 Edge 浏览器，请手动打开后重试。")
        return

    save_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), IMAGE_OUTPUT_DIR
    )

    for j in range(count):
        print(f"\n  ── 生成图片 {j+1}/{count} ──")
        try:
            filepath = workflow._generate_and_download(prompt_text, save_dir)
            print(f"  ✓ 已保存：{filepath}")
        except Exception as e:
            log.error(f"  图像生成失败：{e}")
            from desktop_utils import take_screenshot
            take_screenshot("image_gen_error")
            print(f"  ✗ 失败：{e}")

    print(f"\n  ✅ 图像生成完成（{count} 张）")


def _run_resume(argv):
    """--resume <story_id>：从已有工作区恢复生成。"""
    from core.story_workspace import StoryWorkspace

    # 解析 story_id
    sid = None
    for i, a in enumerate(argv):
        if a == '--resume' and i + 1 < len(argv):
            sid = argv[i + 1]
            break

    if not sid:
        print("  用法：python main.py --resume <story_id>")
        print("  可用的 story_id：")
        from config import STORY_OUTPUT_DIR
        import os as _os
        root = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), STORY_OUTPUT_DIR
        )
        if _os.path.exists(root):
            for d in sorted(_os.listdir(root)):
                dp = _os.path.join(root, d)
                if _os.path.isdir(dp) and _os.path.exists(
                    _os.path.join(dp, "_progress.json")
                ):
                    print(f"    {d}")
        else:
            print("    （暂无）")
        return

    try:
        ws = StoryWorkspace(story_id=sid)
    except Exception as e:
        print(f"  ❌ 无法加载故事 {sid}：{e}")
        return

    p = ws.progress
    if not p:
        print(f"  ❌ 故事 {sid} 缺少 _progress.json")
        return

    print(f"\n  📖 恢复故事：{p.get('title', sid)}")
    print(f"     进度：{p.get('last_chapter_written', 0)}/"
          f"{p.get('total_chapters', '?')} 章")

    last = p.get('last_chapter_written', 0)
    total = p.get('total_chapters', 0)
    if last >= total:
        print(f"  ✓ 故事已完成，无需恢复")
        return

    # 获取 recipe（从知识库提取）
    title = p.get('title', '')
    recipe = None
    try:
        from config import KB_ENABLE, LLM_API_KEY
        if KB_ENABLE and LLM_API_KEY:
            from kb_manager import load_kb
            kb = load_kb()
            recipes = kb.get("recipes", [])
            if recipes:
                recipe = recipes[-1]  # 使用最近配方
                print(f"  配方：{recipe.get('hook', '?')[:20]}")
    except Exception:
        pass

    from config import LONG_FORM_MODE
    if not LONG_FORM_MODE:
        print("  ❌ 长文模式未启用（LONG_FORM_MODE=False）")
        return

    # 尝试加载元知识
    meta_knowledge = None
    try:
        from config import META_LEARN_ENABLE
        if META_LEARN_ENABLE:
            from meta_learner import load_meta_knowledge
            meta_knowledge = load_meta_knowledge()
            if meta_knowledge:
                print(f"  元知识：已加载（{len(meta_knowledge)} 字符）")
    except Exception:
        pass

    print(f"\n  继续生成...\n")
    try:
        from llm_api import generate_long_form_story
        story = generate_long_form_story(
            title, recipe=recipe, meta_knowledge=meta_knowledge, workspace=ws,
        )
        if story:
            print(f"\n  ✅ 故事恢复完成！"
                  f"共 {len(story)} 字符")
        else:
            print(f"\n  ❌ 恢复生成失败")
    except KeyboardInterrupt:
        print(f"\n  ⏸ 中断。进度已保存，可再次 --resume {sid} 继续")
    except Exception as e:
        log.error(f"恢复生成异常：{e}")


# ============================================================
# 主入口
# ============================================================

def main():
    from desktop_utils import (
        load_coords, get_bounds, focus_edge, take_screenshot,
        calibrate_mode, COORDS
    )

    select_str = "手动" if QUESTION_SELECT_MODE == "manual" else "自动"
    llm_str = "API 流式" if LLM_MODE == "api" else "浏览器"
    filter_str = "开" if ENABLE_STORY_FILTER else "关"

    print(f"""
    ╔══════════════════════════════════════════════╗
    ║       ✒️ AutoQuill v2.3                      ║
    ║                                              ║
    ║  选题：{select_str}  生成：{llm_str}  故事筛选：{filter_str}  ║
    ║                                              ║
    ║  无参数      批量模式（默认）                ║
    ║  --single    传统模式（逐轮生成即发布）      ║
    ║  --use-meta  注入元知识到生成 prompt         ║
    ║  --no-meta   强制不注入元知识                ║
    ║  --calibrate 校准坐标                        ║
    ║  --test-ocr  测试 OCR                        ║
    ║  --debug-ocr-region  进入回答页并标注 OCR 区域║
    ║  --test-api  测试 API 连接                   ║
    ║  --image-gen 图像生成模式                    ║
    ║                                              ║
    ║  v2.3：结构化重构 + 自进化系统                ║
    ║  安全：鼠标左上角 或 Ctrl+C 终止             ║
    ╚══════════════════════════════════════════════╝
    """)

    screen_w, screen_h = pyautogui.size()
    print(f"  屏幕：{screen_w}x{screen_h}")
    print(f"  选题：{QUESTION_SELECT_MODE} | LLM：{LLM_MODE} | "
          f"故事筛选：{filter_str}")

    # 知识库状态
    try:
        from config import KB_ENABLE
        if KB_ENABLE:
            from kb_manager import load_kb
            kb = load_kb()
            kb_count = len(kb.get("recipes", []))
            if kb_count > 0:
                print(f"  知识库：✓ 已启用（{kb_count} 个配方）")
            else:
                print("  知识库：✓ 已启用（空，运行后自动积累）")
        else:
            print("  知识库：关闭（参考文章模式）")
    except Exception:
        print("  知识库：未配置")
    print()

    # CLI 命令分发
    if '--calibrate' in sys.argv:
        calibrate_mode()
        return
    if '--test-ocr' in sys.argv:
        test_ocr_mode()
        return
    if '--debug-ocr-region' in sys.argv:
        debug_ocr_region_mode()
        return
    if '--probe-a11y' in sys.argv:
        probe_a11y_mode(sys.argv)
        return
    if '--test-api' in sys.argv:
        from llm_api import test_api_connection
        test_api_connection()
        return
    if '--image-gen' in sys.argv:
        _run_image_gen()
        return
    if '--resume' in sys.argv:
        _run_resume(sys.argv)
        return

    # 坐标检查
    if not load_coords():
        from desktop_utils import _get_required_keys
        required = _get_required_keys()
        missing = [k for k in required if k not in COORDS]
        print(f"  ❌ 缺少坐标：{[required[k] for k in missing]}")
        print("  请运行：python main.py --calibrate\n")
        return

    lx, rx, ty, by = get_bounds()
    print(f"  OCR 区域：({lx},{ty})~({rx},{by})")

    # API/Web 模式检查
    if LLM_MODE == "api":
        from llm_api import test_api_connection
        if not test_api_connection():
            print("\n  API 连接失败，请检查 config.py 中的 LLM_API_KEY")
            return
        print()
    else:
        from config import WEB_DRIVERS
        drv_cfg = WEB_DRIVERS.get(WEB_DRIVER_NAME, {})
        print(f"  Web 驱动：{WEB_DRIVER_NAME}")

        icon_rel = drv_cfg.get("copy_icon", "")
        if icon_rel:
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), icon_rel
            )
            if os.path.exists(icon_path):
                print(f"  复制按钮：图标匹配（{icon_rel}）")
            else:
                print(f"  ⚠ 复制按钮图片未找到：{icon_rel}")

        if WEB_DRIVER_NAME == "DeepSeek":
            mode_name = ("快速模式" if drv_cfg.get("mode") == "fast"
                         else "专家模式")
            extras = []
            if drv_cfg.get("deep_think"):
                extras.append("深度思考")
            if drv_cfg.get("smart_search"):
                extras.append("智能搜索")
            extras_str = "+".join(extras) if extras else "无"
            print(f"  {WEB_DRIVER_NAME}：{mode_name} | "
                  f"附加功能：{extras_str}")

    # 素材模式
    mat_mode_names = {
        "recipe": "纯配方",
        "reference": "纯参考文章",
        "recipe_and_reference": "配方+参考文章"
    }
    print(f"  素材模式："
          f"{mat_mode_names.get(STORY_MATERIAL_MODE, STORY_MATERIAL_MODE)}")

    print("  加载 OCR...")
    from ocr_utils import _get_engine
    _get_engine()
    print("  ✓ 就绪\n")

    # 创建工作流实例
    from workflows.zhihu import ZhihuWorkflow
    workflow = ZhihuWorkflow()

    # --- 选择运行模式 ---
    # 默认走批量模式（询问生成数/发布数）
    # --single 或 -s 走传统模式（每轮生成即发布）
    use_single_mode = ('--single' in sys.argv) or ('-s' in sys.argv)

    if use_single_mode:
        # 传统模式：逐轮生成→发布，直到成功 target 轮
        try:
            target = int(input("要成功执行几轮？>> ").strip())
        except ValueError:
            target = 1

        print(f"\n  传统模式：每轮生成即发布")
        print(f"  目标：成功 {target} 轮"
              f"（最多尝试 {MAX_TOTAL_ATTEMPTS} 轮）\n")
        input("按 Enter 开始 >> ")

        # ★ 参数确认完毕，启动/聚焦浏览器
        from desktop_utils import ensure_edge
        if not ensure_edge():
            print("  ❌ 无法启动 Edge 浏览器，请手动打开后重试。")
            return

        done = 0
        attempts = 0
        time_start = time.time()

        # ★ 重置 Token 追踪器
        if LLM_MODE == "api":
            try:
                from llm_token_tracker import tracker
                tracker.reset()
            except Exception:
                pass

        while done < target and attempts < MAX_TOTAL_ATTEMPTS:
            attempts += 1
            log.info(f"\n{'='*60}")
            log.info(f"第 {attempts} 次尝试"
                     f"（已成功 {done}/{target}）")
            log.info(f"{'='*60}")
            try:
                if workflow.run_single():
                    done += 1
                    log.info(f"  ✓ 成功！（{done}/{target}）")
                else:
                    log.warning(f"  ✗ 失败"
                                f"（尝试 {attempts}/{MAX_TOTAL_ATTEMPTS}）")
            except KeyboardInterrupt:
                log.info("\n中断。")
                break
            except Exception as e:
                log.error(f"本轮失败: {e}")
                take_screenshot("error")
                log.warning(f"  ✗ 异常"
                            f"（尝试 {attempts}/{MAX_TOTAL_ATTEMPTS}）")

            # ★ 修复：run_single() 结束后重置 Web Driver
            # 避免下次迭代复用已污染的 DeepSeek 会话（旧对话历史累积导致崩溃）
            if LLM_MODE == "web":
                try:
                    from web_drivers import reset_driver
                    reset_driver()
                    log.info("  Web Driver 已重置，下次迭代将使用全新会话")
                except Exception:
                    pass

            if done < target and attempts < MAX_TOTAL_ATTEMPTS:
                random_delay(WAIT_BETWEEN_CYCLES)

        time_total = time.time() - time_start

        if done >= target:
            log.info(f"\n🎉 目标达成！成功 {done}/{target} 轮"
                     f"（共尝试 {attempts} 次）")
        else:
            log.warning(f"\n⚠ 未达目标：成功 {done}/{target} 轮"
                        f"（已用完 {attempts} 次尝试）")
        log.info(f"  总耗时：{time_total:.1f}s"
                 f"（{time_total/60:.1f}分钟）")

        # ★ Token 用量汇总
        if LLM_MODE == "api":
            try:
                from llm_token_tracker import tracker
                tracker.summary()
                tracker.save(run_type="single")
            except Exception:
                pass

        return

    # --- 批量模式（默认） ---
    gen_count, pub_count, batch_rounds = ask_batch_params()

    # --- 元知识注入开关 ---
    # 优先级：--no-meta > --use-meta > config 默认值
    try:
        from config import META_INJECT_DEFAULT
    except ImportError:
        META_INJECT_DEFAULT = False
    if '--no-meta' in sys.argv:
        use_meta = False
    elif '--use-meta' in sys.argv:
        use_meta = True
    else:
        use_meta = META_INJECT_DEFAULT

    gen_mode = "并行" if LLM_MODE == "api" else "Web"
    print(f"\n  🚀 流水线批量模式启动")
    if len(batch_rounds) > 1:
        print(f"     总目标：发布 {pub_count} 篇，拆分为 "
              f"{len(batch_rounds)} 轮")
        for r in batch_rounds:
            print(f"       第 {r['round']} 轮：收集/生成 "
                  f"{r['gen_count']} 篇 → 发布 {r['pub_count']} 篇")
    else:
        print(f"     阶段1：串行收集 {gen_count} 份素材（选题+OCR）")
    print(f"     阶段2：{gen_mode} 生成 {gen_count} 篇故事"
          f"{' ✨注入元知识' if use_meta else ''}")
    if pub_count < gen_count:
        print(f"     阶段3：评分 → 择优发布 {pub_count} 篇")
    else:
        print(f"     阶段3：全部发布（{min(gen_count, pub_count)} 篇，"
              f"不评分）")
    print(f"     阶段3.5：元学习（评分回写+入池+检测蒸馏，自动）")
    print()
    input("按 Enter 开始流水线 >> ")

    # ★ 参数确认完毕，启动/聚焦浏览器
    from desktop_utils import ensure_edge
    if not ensure_edge():
        print("  ❌ 无法启动 Edge 浏览器，请手动打开后重试。")
        return

    total_published = 0
    time_batch_start = time.time()
    for i, r in enumerate(batch_rounds):
        if len(batch_rounds) > 1:
            print(f"\n  ══ 批量分轮 {i+1}/{len(batch_rounds)}："
                  f"生成 {r['gen_count']} 篇 → 发布 {r['pub_count']} 篇 ══")
            log.info(f"\n{'='*60}")
            log.info(f"批量分轮 {i+1}/{len(batch_rounds)}："
                     f"生成 {r['gen_count']} 篇 → 发布 {r['pub_count']} 篇")
            log.info(f"{'='*60}")

        try:
            published = workflow.run_batch(
                r["gen_count"],
                publish_count=r["pub_count"],
                use_meta=use_meta
            )
            total_published += published or 0
        except KeyboardInterrupt:
            print("\n  用户中断批量任务。")
            log.info("用户中断批量任务。")
            break

        if i < len(batch_rounds) - 1:
            print(f"\n  本轮完成，累计发布 {total_published}/{pub_count} 篇。")
            random_delay(WAIT_BETWEEN_CYCLES)

    time_batch_total = time.time() - time_batch_start
    print(f"\n  ✅ 批量任务结束：累计发布 {total_published}/{pub_count} 篇，"
          f"耗时 {time_batch_total/60:.1f} 分钟")
    log.info(f"\n批量任务结束：累计发布 {total_published}/{pub_count} 篇，"
             f"耗时 {time_batch_total:.1f}s")

    # ★ Token 用量汇总
    if LLM_MODE == "api":
        try:
            from llm_token_tracker import tracker
            tracker.summary()
            tracker.save(run_type="batch")
        except Exception:
            pass


if __name__ == "__main__":
    main()
