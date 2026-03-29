# ============================================================
# 知乎自动化 v1.0
#
# 新特性：
# - 所有等待时间集中在 config.py，方便调试
# - 手动/自动选题模式切换（config.QUESTION_SELECT_MODE）
# - 智能检测回答加载（OCR 轮询，不再固定等待）
# ============================================================

import ctypes
import json

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import pyautogui
import pyperclip
import time
import random
import sys
import os
import subprocess
import logging
from datetime import datetime

from config import (
    ZHIHU_RECOMMEND_URL, DEEPSEEK_URL,
    PYAUTOGUI_PAUSE, DEEPSEEK_SYSTEM_PROMPT,
    QUESTION_SELECT_MODE,
    MIN_ANSWER_LENGTH, MAX_ANSWER_RETRIES,
    DEEPSEEK_POLL_INTERVAL, DEEPSEEK_STABLE_COUNT, DEEPSEEK_MAX_WAIT,
    WAIT_HOTKEY, WAIT_PASTE, WAIT_PAGE_LOAD, WAIT_TAB_OPEN,
    WAIT_RECOMMEND_PAGE, WAIT_QUESTION_ENTER,
    WAIT_ANSWER_LOAD_POLL, WAIT_ANSWER_LOAD_MAX,
    WAIT_BEFORE_OCR, WAIT_DEEPSEEK_LOAD,
    WAIT_DS_INPUT_CLICK, WAIT_DS_AFTER_PASTE, WAIT_DS_AFTER_SEND,
    WAIT_DS_FIRST_REPLY,
    WAIT_ZHIHU_PAGE_LOAD, WAIT_WRITE_ANSWER_CLICK,
    WAIT_EDITOR_CLICK, WAIT_AFTER_PASTE, WAIT_CONFIRM_CLICK,
    WAIT_DRAFT_SAVE, WAIT_BETWEEN_CYCLES,
    random_delay, random_mouse_duration
)

# ============================================================
# 基础设置
# ============================================================

pyautogui.FAILSAFE = True
pyautogui.PAUSE = PYAUTOGUI_PAUSE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"zhihu_auto_{datetime.now():%Y%m%d_%H%M%S}.log",
                            encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# 坐标管理
# ============================================================

COORDS_FILE = "coordinates.json"
COORD_KEYS = {
    "ocr_content_left":   "知乎正文区域的左边界",
    "ocr_content_right":  "知乎正文区域的右边界（侧边栏左侧）",
    "ocr_content_top":    "知乎正文区域的上边界（标签栏下方）",
    "ocr_content_bottom": "知乎正文区域的下边界",
    "deepseek_copy_btn":  "DeepSeek 回复底部的复制图标按钮",
}
COORDS = {}

def load_coords():
    global COORDS
    if not os.path.exists(COORDS_FILE):
        return False
    try:
        with open(COORDS_FILE, 'r', encoding='utf-8') as f:
            COORDS = json.load(f).get("coordinates", {})
        return all(k in COORDS for k in COORD_KEYS)
    except Exception:
        return False

def save_coords(c):
    with open(COORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump({"coordinates": c, "screen_size": list(pyautogui.size()),
                    "at": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)

def get_coord(key):
    if key not in COORDS:
        raise RuntimeError(f"'{key}' 未校准！--calibrate")
    return COORDS[key][0], COORDS[key][1]

def get_bounds():
    lx, _ = get_coord("ocr_content_left")
    rx, _ = get_coord("ocr_content_right")
    _, ty = get_coord("ocr_content_top")
    _, by = get_coord("ocr_content_bottom")
    return lx, rx, ty, by

# ============================================================
# 窗口焦点
# ============================================================

def focus_edge():
    ps = '''
    Add-Type @"
    using System; using System.Runtime.InteropServices;
    public class W { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h); }
"@
    $p = Get-Process msedge -EA 0 | ? { $_.MainWindowHandle -ne 0 } | Select -First 1
    if($p){ $h=$p.MainWindowHandle; if([W]::IsIconic($h)){[W]::ShowWindow($h,9)}; [W]::SetForegroundWindow($h) }
    '''
    try:
        subprocess.run(['powershell','-EP','Bypass','-C',ps],
                       capture_output=True, text=True, timeout=5)
    except Exception:
        pyautogui.hotkey('alt', 'tab')
    time.sleep(0.3)

def wait_for_user(prompt="按 Enter >> ", auto_focus=True):
    input(prompt)
    if auto_focus:
        focus_edge()
        time.sleep(0.3)

# ============================================================
# 工具函数
# ============================================================

def countdown(s=5):
    for i in range(s, 0, -1):
        print(f"  {i}...")
        time.sleep(1)

def navigate_to_url(url):
    log.info(f"导航: {url}")
    pyautogui.hotkey('ctrl', 'l')
    random_delay(WAIT_HOTKEY)
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.15)
    pyperclip.copy(url)
    pyautogui.hotkey('ctrl', 'v')
    random_delay(WAIT_HOTKEY)
    pyautogui.press('enter')
    random_delay(WAIT_PAGE_LOAD)

def open_new_tab(url=None):
    pyautogui.hotkey('ctrl', 't')
    random_delay(WAIT_TAB_OPEN)
    if url:
        navigate_to_url(url)

def close_current_tab():
    pyautogui.hotkey('ctrl', 'w')
    random_delay(WAIT_HOTKEY)

def paste_text(text=None):
    if text is not None:
        pyperclip.copy(text)
        time.sleep(0.15)
    pyautogui.hotkey('ctrl', 'v')
    random_delay(WAIT_PASTE)

def take_screenshot(name="debug"):
    os.makedirs("screenshots", exist_ok=True)
    fn = f"screenshots/{name}_{datetime.now():%H%M%S}.png"
    pyautogui.screenshot().save(fn)

def grab_current_url():
    """从地址栏抓取当前页面 URL"""
    pyautogui.hotkey('ctrl', 'l')
    time.sleep(0.2)
    pyautogui.hotkey('ctrl', 'c')
    time.sleep(0.2)
    url = pyperclip.paste()
    pyautogui.press('escape')
    time.sleep(0.1)
    return url

# ============================================================
# 步骤 1：选题（手动 / 自动）
# ============================================================

def step1_select_question():
    log.info("=" * 50)
    mode = QUESTION_SELECT_MODE
    log.info(f"步骤 1：选题（模式：{mode}）")
    log.info("=" * 50)

    focus_edge()
    time.sleep(0.3)
    navigate_to_url(ZHIHU_RECOMMEND_URL)
    time.sleep(WAIT_RECOMMEND_PAGE)

    lx, rx, ty, by = get_bounds()

    if mode == "auto":
        question_url = _step1_auto(lx, rx, ty, by)
    else:
        question_url = _step1_manual(lx, rx, ty, by)

    return question_url

def _step1_auto(lx, rx, ty, by):
    """全自动选题"""
    from ocr_utils import parse_recommend_questions, wait_for_answer_load

    log.info("OCR 解析推荐问题...")
    questions = parse_recommend_questions(lx, ty, rx, by)

    if not questions:
        log.error("未识别到问题！")
        raise RuntimeError("推荐页解析失败")

    has_hot = any(q['is_hot'] for q in questions)
    log.info(f"模式：{'飙升优先' if has_hot else '综合评分'} | {len(questions)} 个候选")

    for i, q in enumerate(questions[:5]):
        hot = " 🔥飙升" if q['is_hot'] else ""
        log.info(f"  {i+1}. {q['title'][:40]}{hot}")
        log.info(f"     {q['views']:.0f}浏览 {q['answers']:.0f}回答 {q['followers']:.0f}关注")

    best = questions[0]
    log.info(f"✓ 选择：{best['title'][:40]}...")

    pyautogui.click(best['click_x'], best['click_y'],
                    duration=random_mouse_duration())
    time.sleep(WAIT_QUESTION_ENTER)

    # 智能等待回答加载
    wait_for_answer_load(lx, ty, rx, by,
                         poll_interval=WAIT_ANSWER_LOAD_POLL,
                         max_wait=WAIT_ANSWER_LOAD_MAX)

    pyautogui.hotkey('ctrl', 'Home')
    time.sleep(0.5)

    return grab_current_url()

def _step1_manual(lx, rx, ty, by):
    """手动选题"""
    from ocr_utils import parse_recommend_questions, wait_for_answer_load

    # 还是显示评分信息给用户参考
    log.info("OCR 解析推荐问题（供参考）...")
    questions = parse_recommend_questions(lx, ty, rx, by)
    if questions:
        for i, q in enumerate(questions[:5]):
            hot = " 🔥飙升" if q['is_hot'] else ""
            log.info(f"  {i+1}. {q['title'][:35]}{hot} | "
                     f"{q['views']:.0f}浏览 {q['answers']:.0f}回答 score={q['score']:.0f}")

    log.info(">>> 请手动选择一个问题并点击进入。")
    wait_for_user("选好后按 Enter >> ", auto_focus=True)

    # 智能等待回答加载
    wait_for_answer_load(lx, ty, rx, by,
                         poll_interval=WAIT_ANSWER_LOAD_POLL,
                         max_wait=WAIT_ANSWER_LOAD_MAX)

    pyautogui.hotkey('ctrl', 'Home')
    time.sleep(0.5)

    return grab_current_url()

# ============================================================
# 步骤 2：自动提取
# ============================================================

def step2_auto_extract():
    log.info("=" * 50)
    log.info("步骤 2：自动提取标题和回答")
    log.info("=" * 50)

    from ocr_utils import extract_zhihu_question_and_answer

    lx, rx, ty, by = get_bounds()

    focus_edge()
    time.sleep(WAIT_BEFORE_OCR)
    pyautogui.hotkey('ctrl', 'Home')
    time.sleep(WAIT_BEFORE_OCR)

    title, answer = extract_zhihu_question_and_answer(
        lx, rx, ty, by,
        min_length=MIN_ANSWER_LENGTH,
        max_retries=MAX_ANSWER_RETRIES
    )

    if not title or not answer or len(answer) < MIN_ANSWER_LENGTH:
        log.error(f"提取失败：标题={len(title or '')}字 回答={len(answer or '')}字")
        raise RuntimeError("内容提取不合格")

    log.info(f"提取成功！标题：{title[:50]}... | 回答：{len(answer)}字符")

    focus_edge()
    pyautogui.hotkey('ctrl', 'Home')
    time.sleep(0.3)

    return title, answer

# ============================================================
# 步骤 3：DeepSeek 全自动
# ============================================================

def step3_auto_deepseek(question_title, top_answer):
    log.info("=" * 50)
    log.info("步骤 3：DeepSeek 全自动生成")
    log.info("=" * 50)

    from ocr_utils import click_by_text, wait_for_deepseek_complete, copy_deepseek_by_position

    open_new_tab(DEEPSEEK_URL)
    time.sleep(WAIT_DEEPSEEK_LOAD)

    sw, sh = pyautogui.size()

    # --- 发送提示词 ---
    log.info("定位输入框...")
    if not click_by_text("DeepSeek", log_name="DS输入框", retries=5, wait=1.0):
        pyautogui.click(sw // 2, int(sh * 0.5), duration=random_mouse_duration())
    time.sleep(WAIT_DS_INPUT_CLICK)

    paste_text(DEEPSEEK_SYSTEM_PROMPT)
    time.sleep(WAIT_DS_AFTER_PASTE)
    pyautogui.press('enter')
    time.sleep(WAIT_DS_AFTER_SEND)
    log.info("提示词已发送。")

    # 等"收到"
    log.info("等待'收到'回复...")
    time.sleep(WAIT_DS_FIRST_REPLY)

    # --- 发送参考材料 ---
    log.info("定位追问输入框...")
    if not click_by_text("DeepSeek", log_name="DS追问框", retries=5, wait=1.0):
        pyautogui.click(sw // 2, int(sh * 0.9), duration=random_mouse_duration())
    time.sleep(WAIT_DS_INPUT_CLICK)

    reference = f"""以下是"全新文章主题"（知乎问题）：
{question_title}

以下是"高赞文章"（参考风格）：
{top_answer}

请根据以上内容，按照之前的要求，开始创作全新的故事。"""

    paste_text(reference)
    time.sleep(WAIT_DS_AFTER_PASTE)
    pyautogui.press('enter')
    time.sleep(WAIT_DS_AFTER_SEND)
    log.info("参考材料已发送。")

    # --- 轮询检测完成 ---
    ds_left = int(sw * 0.1)
    ds_right = int(sw * 0.9)
    ds_top = int(sh * 0.1)
    ds_bottom = int(sh * 0.85)

    completed = wait_for_deepseek_complete(
        ds_left, ds_top, ds_right, ds_bottom,
        poll_interval=DEEPSEEK_POLL_INTERVAL,
        stable_count=DEEPSEEK_STABLE_COUNT,
        max_wait=DEEPSEEK_MAX_WAIT
    )
    if not completed:
        log.warning("超时，尝试继续...")

    # --- 复制 ---
    copy_x, copy_y = get_coord("deepseek_copy_btn")
    copied = copy_deepseek_by_position(copy_x, copy_y)

    if copied:
        story = pyperclip.paste()
        log.info(f"获取故事：{len(story)} 字符")
        return story

    # 重试
    log.info("首次失败，微调后重试...")
    pyautogui.scroll(3)
    time.sleep(0.5)
    copied = copy_deepseek_by_position(copy_x, copy_y)
    if copied:
        return pyperclip.paste()

    # 兜底
    log.warning("自动复制失败，请手动复制。")
    input("手动复制后按 Enter >> ")
    focus_edge()
    return pyperclip.paste()

# ============================================================
# 步骤 4：自动粘贴到知乎
# ============================================================

def step4_auto_paste(generated_story, question_title, question_url):
    log.info("=" * 50)
    log.info("步骤 4：自动粘贴到知乎")
    log.info("=" * 50)

    from ocr_utils import click_by_text

    navigate_to_url(question_url)
    time.sleep(WAIT_ZHIHU_PAGE_LOAD)

    pyautogui.hotkey('ctrl', 'Home')
    time.sleep(0.5)

    # 1. 写回答
    log.info("OCR 定位「写回答」...")
    if not click_by_text("写回答", retries=5, wait=1.0):
        log.error("未找到'写回答'！")
        raise RuntimeError("无法定位写回答")
    time.sleep(WAIT_WRITE_ANSWER_CLICK)

    # 2. 粘贴
    log.info("粘贴故事...")
    lx, rx, ty, by = get_bounds()
    pyautogui.click((lx + rx) // 2, (ty + by) // 2, duration=random_mouse_duration())
    time.sleep(WAIT_EDITOR_CLICK)
    paste_text(generated_story)
    time.sleep(WAIT_AFTER_PASTE)

    # 3. 确认并解析
    log.info("OCR 定位「确认并解析」...")
    if not click_by_text("确认并解析", retries=5, wait=0.5):
        log.warning("未找到确认按钮")
    time.sleep(WAIT_CONFIRM_CLICK)

    time.sleep(WAIT_DRAFT_SAVE)
    log.info("草稿已保存")
    close_current_tab()
    log.info(f"完成：「{question_title[:30]}...」")

# ============================================================
# 主流程
# ============================================================

def run_single_cycle():
    url = step1_select_question()
    title, answer = step2_auto_extract()
    story = step3_auto_deepseek(title, answer)

    if not story or len(story) < 500:
        log.error(f"故事过短（{len(story or '')}字符），跳过")
        return False

    step4_auto_paste(story, title, url)
    close_current_tab()

    log.info("本轮完成！")
    return True

def main():
    mode_str = "手动选题" if QUESTION_SELECT_MODE == "manual" else "全自动"
    print(f"""
    ╔══════════════════════════════════════════════╗
    ║       知乎故事自动化 v1.0                    ║
    ║                                              ║
    ║  选题模式：{mode_str}                          ║
    ║  （在 config.py 中修改 QUESTION_SELECT_MODE）║
    ║                                              ║
    ║  --calibrate  校准 5 个点                    ║
    ║  --test-ocr   测试 OCR                       ║
    ║                                              ║
    ║  安全：鼠标左上角 或 Ctrl+C 终止             ║
    ╚══════════════════════════════════════════════╝
    """)

    screen_w, screen_h = pyautogui.size()
    print(f"  屏幕：{screen_w}x{screen_h}\n")

    if '--calibrate' in sys.argv:
        calibrate_mode()
        return
    if '--test-ocr' in sys.argv:
        test_ocr_mode()
        return

    if not load_coords():
        print("  ❌ 请先：python zhihu_auto.py --calibrate\n")
        return

    lx, rx, ty, by = get_bounds()
    cx, cy = get_coord("deepseek_copy_btn")
    print(f"  OCR 区域：({lx},{ty})~({rx},{by})")
    print(f"  DS 复制按钮：({cx},{cy})\n")

    print("  加载 OCR...")
    from ocr_utils import _get_engine
    _get_engine()
    print("  ✓ 就绪\n")

    focus_edge()
    time.sleep(0.5)

    try:
        cycles = int(input("执行几轮？>> ").strip())
    except ValueError:
        cycles = 1

    input("按 Enter 开始 >> ")

    done = 0
    for i in range(cycles):
        log.info(f"\n{'='*60}")
        log.info(f"第 {i+1}/{cycles} 轮")
        log.info(f"{'='*60}")
        try:
            if run_single_cycle():
                done += 1
        except KeyboardInterrupt:
            log.info("\n中断。")
            break
        except Exception as e:
            log.error(f"本轮失败: {e}")
            take_screenshot("error")
            if i < cycles - 1:
                log.info("跳过，继续下一轮...")

        if i < cycles - 1:
            random_delay(WAIT_BETWEEN_CYCLES)

    log.info(f"\n执行完毕：{done}/{cycles} 轮成功")

# ============================================================
# 校准
# ============================================================

_HINTS = {
    "ocr_content_left":   "将鼠标移到知乎回答正文最左边（文字起始位置）",
    "ocr_content_right":  "将鼠标移到知乎回答正文最右边（侧边栏之前）",
    "ocr_content_top":    "将鼠标移到知乎页面内容上边界（标签栏下方）",
    "ocr_content_bottom": "将鼠标移到知乎页面内容下边界",
    "deepseek_copy_btn":  "在 DeepSeek 中让它生成一段回复，然后将鼠标移到\n"
                          "    回复底部左下角的复制图标上（第一个小图标）",
}

def calibrate_mode():
    print("""
    ===== 校准模式 =====
    需要校准 5 个点（增量式，已有的可跳过）
    """)

    existing = {}
    if os.path.exists(COORDS_FILE):
        try:
            with open(COORDS_FILE, 'r', encoding='utf-8') as f:
                existing = json.load(f).get("coordinates", {})
        except Exception:
            pass

    if existing:
        print("  已有坐标：")
        for k, d in COORD_KEYS.items():
            if k in existing:
                print(f"    ✓ {d}: ({existing[k][0]}, {existing[k][1]})")
            else:
                print(f"    ✗ {d}: 未校准")

        missing = [k for k in COORD_KEYS if k not in existing]
        have = [k for k in COORD_KEYS if k in existing]

        if not missing:
            print("\n  全部已校准。要重新校准哪些？")
            for i, k in enumerate(have):
                print(f"    {i+1}. {COORD_KEYS[k]}")
            redo = input("\n  编号（如 1,3,5）或回车跳过 >> ").strip()
            if not redo:
                return
            try:
                keys_to_cal = [have[int(x.strip())-1] for x in redo.split(',')]
            except (ValueError, IndexError):
                return
        else:
            keys_to_cal = list(missing)
            if have:
                print(f"\n  需校准 {len(missing)} 个。已有的要重新校准吗？")
                for i, k in enumerate(have):
                    print(f"    {i+1}. {COORD_KEYS[k]}")
                redo = input("  编号（直接回车跳过）>> ").strip()
                if redo:
                    try:
                        keys_to_cal += [have[int(x.strip())-1] for x in redo.split(',')]
                    except (ValueError, IndexError):
                        pass
    else:
        keys_to_cal = list(COORD_KEYS.keys())

    input("\n  按 Enter 开始校准...")
    c = dict(existing)
    for k in keys_to_cal:
        print(f"\n--- {COORD_KEYS[k]} ---")
        print(f"    {_HINTS[k]}")
        input("    按 Enter 后 5 秒倒计时...")
        countdown(5)
        x, y = pyautogui.position()
        c[k] = [x, y]
        print(f"    ✓ ({x}, {y})")

    save_coords(c)
    print("\n  校准完成！")

def test_ocr_mode():
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

if __name__ == "__main__":
    main()
