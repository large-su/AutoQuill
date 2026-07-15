# ============================================================
# workflows/zhihu.py — 知乎工作流
#
# 实现知乎平台专属的 4 个步骤：
#   步骤1：从知乎推荐页选题
#   步骤2：OCR 提取知乎问答内容
#   步骤3：（继承基类）生成故事
#   步骤4：通过知乎编辑器导入发布
#
# 以及批量素材收集逻辑。
# ============================================================

import pyautogui
import time
import os
import logging

from workflows.base import WorkflowBase

log = logging.getLogger(__name__)


class ZhihuWorkflow(WorkflowBase):
    """知乎平台工作流"""

    name = "zhihu"

    # ============================================================
    # 步骤1：选题
    # ============================================================

    def select_topic(self):
        """从知乎推荐页选题，返回问题页 URL"""
        from config import (
            ZHIHU_RECOMMEND_URL, QUESTION_SELECT_MODE,
            WAIT_RECOMMEND_PAGE, WAIT_FOCUS_SETTLE
        )
        from desktop_utils import (
            focus_edge, navigate_to_url, grab_current_url,
            get_bounds
        )
        from config import random_mouse_duration

        log.info("=" * 50)
        log.info(f"步骤 1：选题（{QUESTION_SELECT_MODE}）")
        log.info("=" * 50)

        focus_edge()
        time.sleep(WAIT_FOCUS_SETTLE)
        navigate_to_url(ZHIHU_RECOMMEND_URL)
        time.sleep(WAIT_RECOMMEND_PAGE)

        lx, rx, ty, by = get_bounds()

        if QUESTION_SELECT_MODE == "auto":
            return self._select_auto(lx, rx, ty, by)
        else:
            return self._select_manual(lx, rx, ty, by)

    def _scan_recommend_page(self, lx, rx, ty, by):
        """扫描推荐页：OCR 解析 + 飙升补标 + 分离飙升/普通。

        返回: (all_questions, hot_questions, normal_questions)
              解析失败返回 (None, None, None)
        """
        from ocr_utils import parse_recommend_questions

        all_qs = parse_recommend_questions(lx, ty, rx, by)
        if not all_qs:
            return None, None, None

        self._scan_hot_labels(all_qs, lx, rx, ty, by)

        hot_qs = [q for q in all_qs if q['is_hot']]
        normal_qs = [q for q in all_qs if not q['is_hot']]

        return all_qs, hot_qs, normal_qs

    def _select_auto(self, lx, rx, ty, by):
        """全自动选题：OCR 解析 → 飙升检测 → 规则筛选 → 评分选最优"""
        from config import (
            WAIT_QUESTION_ENTER, WAIT_ANSWER_LOAD_TRIGGER, WAIT_AFTER_HOME
        )
        from config import random_mouse_duration
        from desktop_utils import grab_current_url

        all_qs, hot_qs, normal_qs = self._scan_recommend_page(lx, rx, ty, by)
        if not all_qs:
            raise RuntimeError("推荐页解析失败")

        log.info(f"  OCR 解析到 {len(all_qs)} 个问题"
                 f"（飙升 {len(hot_qs)} / 普通 {len(normal_qs)}）")
        for i, q in enumerate(all_qs):
            hot_flag = " [飙升]" if q['is_hot'] else ""
            log.info(f"    {i+1}. {q['title'][:35]}{hot_flag} "
                     f"score={q['score']:.0f} "
                     f"click=({q['click_x']},{q['click_y']})")

        best = self._pick_best(all_qs, hot_qs, normal_qs)

        log.info("")
        log.info("最终候选（前5）：")
        final_pool = hot_qs if hot_qs else all_qs
        final_pool.sort(key=lambda q: q['score'], reverse=True)
        for i, q in enumerate(final_pool[:5]):
            hot = " 🔥飙升" if q['is_hot'] else ""
            story = " [故事]" if q.get('is_story') else ""
            marker = " ← 选择" if q is best else ""
            log.info(f"  {i+1}. {q['title'][:40]}{hot}{story}{marker}")
            log.info(f"     {q['views']:.0f}浏览 {q['answers']:.0f}回答 "
                     f"{q['followers']:.0f}关注 score={q['score']:.0f}")

        log.info("")
        log.info(f"✓ 最终选择：{best['title'][:50]}...")
        log.info(f"  点击坐标：({best['click_x']}, {best['click_y']})")

        pyautogui.click(best['click_x'], best['click_y'],
                        duration=random_mouse_duration())
        time.sleep(WAIT_QUESTION_ENTER)

        log.info("触发回答加载（PageDown）...")
        pyautogui.press('pagedown')
        time.sleep(WAIT_ANSWER_LOAD_TRIGGER)
        pyautogui.hotkey('ctrl', 'Home')
        time.sleep(WAIT_AFTER_HOME)
        return grab_current_url()

    def _select_manual(self, lx, rx, ty, by):
        """手动选题：OCR 解析供参考，用户自行点击"""
        from desktop_utils import wait_for_user, grab_current_url
        from config import WAIT_ANSWER_LOAD_TRIGGER, WAIT_AFTER_HOME

        all_qs, hot_qs, normal_qs_raw = self._scan_recommend_page(lx, rx, ty, by)

        # 规则筛选（替代 LLM 筛选）
        normal_qs_filtered = self._apply_story_filter(normal_qs_raw or [])

        if hot_qs:
            log.info("  🔥 飙升问题（优先）：")
            for i, q in enumerate(hot_qs):
                story = " [故事]" if q.get('is_story') else ""
                log.info(f"    {i+1}. {q['title'][:40]} 🔥{story} | "
                         f"{q['views']:.0f}浏览 {q['answers']:.0f}回答")

        # 展示普通问题：优先用筛选结果，被清空则回退展示原始列表
        display_qs = normal_qs_filtered if normal_qs_filtered else normal_qs_raw
        if display_qs:
            log.info("  普通问题：")
            for i, q in enumerate(display_qs[:5]):
                story = " [故事]" if q.get('is_story') else ""
                log.info(f"    {i+1}. {q['title'][:40]}{story} | "
                         f"{q['views']:.0f}浏览 {q['answers']:.0f}回答")

        if hot_qs:
            log.info("  💡 建议优先选择飙升问题")

        log.info(">>> 请手动选择问题并点击进入。")
        wait_for_user("选好后按 Enter >> ", auto_focus=True)

        log.info("触发回答加载（PageDown）...")
        pyautogui.press('pagedown')
        time.sleep(WAIT_ANSWER_LOAD_TRIGGER)
        pyautogui.hotkey('ctrl', 'Home')
        time.sleep(WAIT_AFTER_HOME)
        return grab_current_url()

    def _scan_hot_labels(self, questions, lx, rx, ty, by):
        """独立扫描屏幕上的飙升/火爆标签并补标到对应问题"""
        from ocr_utils import find_text_on_screen

        left_col_right = lx + int((rx - lx) * 0.55)
        extend_left = max(lx - 100, 0)

        for keyword in ["飙升", "火爆", "热门"]:
            pos = find_text_on_screen(
                keyword,
                region=(extend_left, ty,
                        left_col_right - extend_left, by - ty)
            )
            if pos:
                log.info(f"  独立扫描发现「{keyword}」标签 "
                         f"at ({pos[0]}, {pos[1]})")
                for q in questions:
                    if abs(pos[1] - q['click_y']) < 50 and not q['is_hot']:
                        q['is_hot'] = True
                        log.info(f"  补标飙升：{q['title'][:30]}...")

    def _apply_story_filter(self, questions):
        """规则筛选：用关键词白/黑名单过滤非故事类问题（替代 LLM 筛选）。"""
        if not questions:
            return questions
        from config import ENABLE_STORY_FILTER
        if not ENABLE_STORY_FILTER:
            return questions

        try:
            from applications.zhihu_story.config import STORY_INCLUDE_KEYWORDS
        except ImportError:
            return questions

        filtered = []
        for q in questions:
            title = q.get('title', '')
            if any(kw in title for kw in STORY_INCLUDE_KEYWORDS):
                q['is_story'] = True
                filtered.append(q)

        if filtered:
            log.info(f"  规则筛选：{len(questions)}→{len(filtered)}"
                     f"（保留 {len(filtered)} 个）")
        else:
            log.info("  规则筛选后无可用问题")

        return filtered

    def _pick_best(self, all_questions, hot_questions, normal_questions):
        """从候选中选出最优问题"""
        from config import ENABLE_STORY_FILTER

        if hot_questions:
            log.info("走飙升优先分支")

            if len(hot_questions) == 1:
                best = hot_questions[0]
                log.info(f"  唯一飙升问题：{best['title'][:40]}...")

                if ENABLE_STORY_FILTER:
                    filtered_hot = self._apply_story_filter(hot_questions)
                    if filtered_hot:
                        log.info("  ✓ 规则确认可写故事")
                        best = filtered_hot[0]
                    else:
                        log.warning("  飙升问题被规则排除，回退到普通问题")
                        if normal_questions:
                            filtered_normal = self._apply_story_filter(normal_questions)
                            if filtered_normal:
                                filtered_normal.sort(
                                    key=lambda q: q['score'], reverse=True
                                )
                                best = filtered_normal[0]
                            else:
                                best = all_questions[0]
                        else:
                            best = hot_questions[0]
            else:
                log.info(f"  {len(hot_questions)} 个飙升问题，评分选最优")
                filtered = self._apply_story_filter(hot_questions)
                if filtered:
                    hot_questions = filtered
                hot_questions.sort(
                    key=lambda q: q['score'], reverse=True
                )
                best = hot_questions[0]
        else:
            log.info("无飙升，走综合评分分支")
            candidates = self._apply_story_filter(all_questions)
            if not candidates:
                candidates = all_questions
            candidates.sort(key=lambda q: q['score'], reverse=True)
            best = candidates[0]

        return best

    # ============================================================
    # 步骤2：提取回答
    # ============================================================

    def _check_question_answerable(self):
        """
        快速 OCR 检测当前问题页面是否可回答。

        在耗时较长的 extract_zhihu_question_and_answer 之前调用，
        避免在不可回答的问题上浪费采集时间。

        检测逻辑：
        - 全屏检测「撤销删除」→ 曾删过回答，绝对不能回答 → 返回 False
        - 否则默认可回答 → 返回 True（「写回答」可能因字体/颜色/遮挡被 OCR 漏检）

        返回: (can_answer: bool, reason: str)
        """
        from ocr_utils import find_text_on_screen

        # 全屏检测「撤销删除」——唯一硬信号，一旦出现绝对不能回答
        pos = find_text_on_screen("撤销删除")
        if pos:
            return False, "检测到「撤销删除」——此问题下曾删除过回答，跳过"

        return True, "未检测到「撤销删除」，默认可回答"

    def _extract_answer_with_fallback(self, left_x, right_x, top_y, bottom_y):
        """优先读取已渲染的 UIA 首答；不完整时回退到旧 OCR 滚屏。"""
        from config import (
            ENABLE_UIA_ANSWER_EXTRACTION,
            UIA_ANSWER_WAIT_TIMEOUT,
            UIA_ANSWER_POLL_INTERVAL,
            MIN_ANSWER_LENGTH,
            MAX_ANSWER_RETRIES,
            ENABLE_MATERIAL_LIKES_GATE,
        )

        if ENABLE_UIA_ANSWER_EXTRACTION:
            try:
                from applications.zhihu_story.a11y_probe import (
                    extract_live_primary_answer,
                )
                title, answer, footer, reason = extract_live_primary_answer(
                    min_length=MIN_ANSWER_LENGTH,
                    wait_timeout=UIA_ANSWER_WAIT_TIMEOUT,
                    poll_interval=UIA_ANSWER_POLL_INTERVAL,
                )
                likes_missing = (
                    ENABLE_MATERIAL_LIKES_GATE
                    and (not footer or footer.get("likes") is None)
                )
                if title and answer and not likes_missing:
                    log.info(
                        "  UIA 首答采集成功：%s 字符，赞同=%s",
                        len(answer), footer.get("likes") if footer else None,
                    )
                    return title, answer, footer
                if likes_missing:
                    reason = "UIA 未读取到赞同数，转 OCR 保底"
                log.info("  UIA 首答未采用：%s，回退 OCR 滚屏", reason)
            except Exception as exc:
                log.warning("  UIA 首答采集异常，回退 OCR：%s", exc)

        from ocr_utils import extract_zhihu_question_and_answer
        return extract_zhihu_question_and_answer(
            left_x, right_x, top_y, bottom_y,
            min_length=MIN_ANSWER_LENGTH,
            max_retries=MAX_ANSWER_RETRIES,
        )

    def extract_content(self, fast_mode=False):
        """OCR 提取知乎问题标题和最佳回答"""
        from config import MIN_ANSWER_LENGTH, WAIT_BEFORE_OCR
        from desktop_utils import focus_edge, get_bounds
        from config import WAIT_FOCUS_SETTLE

        log.info("=" * 50)
        log.info("步骤 2：自动提取标题和回答")
        log.info("=" * 50)

        lx, rx, ty, by = get_bounds()

        focus_edge()
        pyautogui.hotkey('ctrl', 'Home')
        time.sleep(WAIT_BEFORE_OCR)

        # ★ 先快速检测本问题是否可回答，避免在不可回答的问题上浪费采集时间
        can_answer, reason = self._check_question_answerable()
        if not can_answer:
            raise RuntimeError(f"问题不可回答：{reason}")

        title, answer, footer = self._extract_answer_with_fallback(
            lx, rx, ty, by
        )

        if not title or not answer or len(answer) < MIN_ANSWER_LENGTH:
            raise RuntimeError(
                f"提取失败：标题={len(title or '')}字 "
                f"回答={len(answer or '')}字"
            )

        log.info(f"提取成功！标题：{title[:50]}... | "
                 f"回答：{len(answer)}字符")
        if footer:
            log.info(f"  footer: 赞={footer.get('likes')} "
                     f"评={footer.get('comments')} "
                     f"藏={footer.get('collects')} "
                     f"喜={footer.get('hearts')} "
                     f"发表={footer.get('publish_time')}")
        else:
            log.info("  footer 未采集（不影响单条流程）")
        focus_edge()
        pyautogui.hotkey('ctrl', 'Home')
        time.sleep(WAIT_FOCUS_SETTLE)
        return title, answer, footer

    # ============================================================
    # 步骤4：发布到知乎
    # ============================================================

    def publish(self, story, title, url, md_path=None):
        """导入故事到知乎编辑器"""
        from config import (
            WAIT_ZHIHU_PAGE_LOAD, WAIT_WRITE_ANSWER_CLICK,
            WAIT_DRAFT_SAVE, WAIT_FOCUS_SETTLE, WAIT_AFTER_HOME,
            WAIT_IMPORT_MENU_SETTLE, WAIT_IMPORT_DOC_PANEL,
            WAIT_UPLOAD_DIALOG_OPEN, WAIT_FILE_PATH_PASTE,
            WAIT_FILE_CONFIRM, WAIT_DOC_IMPORT_DONE,
            WAIT_FALLBACK_CLOSE_DIALOG,
            OCR_CLICK_WRITE_ANSWER_RETRIES, OCR_CLICK_WRITE_ANSWER_WAIT,
            WAIT_WRITE_ANSWER_RETRY_HOME,
            OCR_CLICK_IMPORT_RETRIES, OCR_CLICK_IMPORT_WAIT,
            OCR_CLICK_MORE_RETRIES, OCR_CLICK_MORE_WAIT,
            OCR_CLICK_IMPORT_DOC_RETRIES, OCR_CLICK_IMPORT_DOC_WAIT,
            OCR_CLICK_UPLOAD_RETRIES, OCR_CLICK_UPLOAD_WAIT
        )
        from desktop_utils import (
            focus_edge, navigate_to_url, paste_text,
            ocr_click_text, get_bounds
        )
        from config import random_mouse_duration

        log.info("=" * 50)
        log.info("步骤 4：导入故事到知乎")
        log.info("=" * 50)

        # 准备 .md 文件
        if md_path and os.path.exists(md_path):
            md_abs_path = os.path.abspath(md_path)
            log.info(f"使用已有文件：{md_abs_path}")
        else:
            md_abs_path = self.save_story_file(story)
            log.info(f"故事已保存：{md_abs_path}")

        focus_edge()
        time.sleep(WAIT_FOCUS_SETTLE)
        navigate_to_url(url)
        time.sleep(WAIT_ZHIHU_PAGE_LOAD)
        pyautogui.hotkey('ctrl', 'home')
        time.sleep(WAIT_AFTER_HOME)

        # 定位「写回答」
        log.info("定位「写回答」...")
        write_found = ocr_click_text(
            "写回答",
            retries=OCR_CLICK_WRITE_ANSWER_RETRIES,
            wait=OCR_CLICK_WRITE_ANSWER_WAIT
        )
        if not write_found:
            log.warning('未定位"写回答"，回到顶部快速重试一次')
            pyautogui.hotkey('ctrl', 'home')
            time.sleep(WAIT_WRITE_ANSWER_RETRY_HOME)
            write_found = ocr_click_text(
                "写回答",
                retries=OCR_CLICK_WRITE_ANSWER_RETRIES,
                wait=OCR_CLICK_WRITE_ANSWER_WAIT
            )
        if not write_found:
            raise RuntimeError('无法定位"写回答"按钮')
        time.sleep(WAIT_WRITE_ANSWER_CLICK)

        # 定位「导入」
        log.info("定位工具栏「导入」...")
        import_found = ocr_click_text(
            "导入",
            retries=OCR_CLICK_IMPORT_RETRIES,
            wait=OCR_CLICK_IMPORT_WAIT
        )
        if not import_found:
            log.warning('未找到"导入"，尝试寻找"更多"')
            ocr_click_text(
                "更多",
                retries=OCR_CLICK_MORE_RETRIES,
                wait=OCR_CLICK_MORE_WAIT
            )
            time.sleep(WAIT_IMPORT_MENU_SETTLE)
            import_found = ocr_click_text(
                "导入",
                retries=OCR_CLICK_IMPORT_RETRIES,
                wait=OCR_CLICK_IMPORT_WAIT
            )

        if not import_found:
            log.warning("导入按钮未找到，降级为直接粘贴方式")
            self._fallback_paste(story)
            time.sleep(WAIT_DRAFT_SAVE)
            log.info(f"草稿已保存，完成（粘贴模式）："
                     f"「{title[:30]}...」")
            return

        time.sleep(WAIT_IMPORT_MENU_SETTLE)

        # 定位「导入文档」
        log.info("定位「导入文档」...")
        ocr_click_text(
            "导入文档",
            retries=OCR_CLICK_IMPORT_DOC_RETRIES,
            wait=OCR_CLICK_IMPORT_DOC_WAIT
        )
        time.sleep(WAIT_IMPORT_DOC_PANEL)

        # 定位上传区域
        log.info("定位上传区域...")
        upload_found = False
        for t in ["点击选择本地文档", "选择本地文档", "本地文档", "拖动文件"]:
            if ocr_click_text(t, retries=OCR_CLICK_UPLOAD_RETRIES,
                              wait=OCR_CLICK_UPLOAD_WAIT,
                              log_name=f"上传区域:{t}"):
                upload_found = True
                break

        if not upload_found:
            log.warning("未找到上传区域，降级为直接粘贴")
            pyautogui.press('escape')
            time.sleep(WAIT_FALLBACK_CLOSE_DIALOG)
            self._fallback_paste(story)
            time.sleep(WAIT_DRAFT_SAVE)
            log.info(f"草稿已保存，完成（粘贴模式）："
                     f"「{title[:30]}...」")
            return

        # 文件选择对话框
        log.info("等待文件选择对话框...")
        time.sleep(WAIT_UPLOAD_DIALOG_OPEN)
        log.info(f"输入文件路径：{md_abs_path}")
        import pyperclip
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(WAIT_FILE_CONFIRM)
        pyperclip.copy(md_abs_path)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(WAIT_FILE_PATH_PASTE)
        pyautogui.press('enter')
        time.sleep(WAIT_FILE_CONFIRM)
        pyautogui.press('enter')

        log.info(f"等待文件导入完成...（{WAIT_DOC_IMPORT_DONE}s）")
        time.sleep(WAIT_DOC_IMPORT_DONE)

        log.info(f"等待草稿保存...（{WAIT_DRAFT_SAVE}s）")
        time.sleep(WAIT_DRAFT_SAVE)
        log.info(f"草稿已保存，完成（导入模式）："
                 f"「{title[:30]}...」")

    def _fallback_paste(self, story):
        """降级：直接粘贴内容到编辑器"""
        from config import WAIT_EDITOR_CLICK, WAIT_AFTER_PASTE
        from desktop_utils import paste_text, get_bounds
        from config import random_mouse_duration

        log.info("降级：直接粘贴到编辑器...")
        lx, rx, ty, by = get_bounds()
        pyautogui.click((lx + rx) // 2, (ty + by) // 2,
                        duration=random_mouse_duration())
        time.sleep(WAIT_EDITOR_CLICK)
        paste_text(story)
        time.sleep(WAIT_AFTER_PASTE)

    # ============================================================
    # 批量素材收集
    # ============================================================

    def collect_materials_batch(self, target):
        """
        批量素材收集：逐屏下翻 + 推荐页刷新循环。

        流程：
        1. 打开推荐页
        2. OCR → 规则筛选 → 取前 N 个进入提取
        3. PageDown 翻下一屏，重复步骤 2
        4. 翻满 SCROLLS_PER_REFRESH 轮后重新打开推荐页刷新内容
        5. 循环直到采够 target 篇
        """
        from ocr_utils import parse_recommend_questions
        from config import (
            ZHIHU_RECOMMEND_URL,
            MIN_ANSWER_LENGTH, MAX_ANSWER_RETRIES,
            WAIT_RECOMMEND_PAGE, WAIT_QUESTION_ENTER,
            WAIT_ZHIHU_PAGE_LOAD, BATCH_QUESTIONS_PER_PAGE,
            WAIT_FOCUS_SETTLE, WAIT_NEXT_SCREEN, WAIT_CLOSE_TAB,
            WAIT_ANSWER_LOAD_TRIGGER, WAIT_AFTER_HOME,
            ENABLE_MATERIAL_LIKES_GATE, MATERIAL_MIN_LIKES,
            MATERIAL_UNKNOWN_LIKES_POLICY, MAX_TOTAL_ATTEMPTS
        )
        try:
            from applications.zhihu_story.config import SCROLLS_PER_REFRESH
        except ImportError:
            SCROLLS_PER_REFRESH = 5
        from config import random_mouse_duration
        from desktop_utils import (
            focus_edge, navigate_to_url, close_current_tab,
            grab_current_url, get_bounds
        )

        lx, rx, ty, by = get_bounds()

        # ★ 初始化异步配方提炼（采集过程中边采边炼）
        try:
            from config import KB_ENABLE
        except ImportError:
            KB_ENABLE = False
        if KB_ENABLE:
            try:
                from config import LLM_API_KEY as _api_key_cfg
                if _api_key_cfg and _api_key_cfg != "密":
                    self._init_async_recipe_extraction()
                    log.info("  ♻ 异步配方提炼已就绪（边采边炼）")
            except Exception:
                pass

        materials = []
        visited_titles = set()
        refresh_count = 0
        total_scrolls = 0
        total_attempts = 0
        unknown_likes_policy = str(MATERIAL_UNKNOWN_LIKES_POLICY).lower()

        # ── 外层：刷新循环 ──
        while len(materials) < target and total_attempts < MAX_TOTAL_ATTEMPTS:
            refresh_count += 1
            log.info(f"\n{'='*40}")
            log.info(f"  🔄 推荐页第 {refresh_count} 次加载"
                     f"（已采集 {len(materials)}/{target}）")
            log.info(f"{'='*40}")

            focus_edge()
            time.sleep(WAIT_FOCUS_SETTLE)
            navigate_to_url(ZHIHU_RECOMMEND_URL)
            time.sleep(WAIT_RECOMMEND_PAGE)

            # ── 内层：逐屏下翻 ──
            for page in range(SCROLLS_PER_REFRESH):
                if len(materials) >= target:
                    break
                if total_attempts >= MAX_TOTAL_ATTEMPTS:
                    break

                total_scrolls += 1
                log.info(f"\n  ── 第 {total_scrolls} 屏"
                         f"（已采集 {len(materials)}/{target}）──")

                def _advance_to_next_screen(reason):
                    """当前屏无可采内容时下翻，避免重复 OCR 同一屏。"""
                    if len(materials) >= target:
                        return
                    if page < SCROLLS_PER_REFRESH - 1:
                        log.info(f"  {reason}，翻到下一屏")
                        pyautogui.press('pagedown')
                        time.sleep(WAIT_NEXT_SCREEN)
                    else:
                        log.info(f"  {reason}，本轮推荐页扫描结束")

                # OCR 解析当前屏
                all_questions = parse_recommend_questions(lx, ty, rx, by)
                if not all_questions:
                    log.warning("  未识别到问题")
                    _advance_to_next_screen("未识别到问题")
                    continue

                # 飙升补标
                self._scan_hot_labels(all_questions, lx, rx, ty, by)

                # 去重
                new_qs = [q for q in all_questions
                          if q['title'] not in visited_titles]
                if not new_qs:
                    msg = f"当前屏 {len(all_questions)} 个问题全部已访问"
                    log.info(f"  {msg}")
                    _advance_to_next_screen(msg)
                    continue

                log.info(f"  可见 {len(all_questions)} 个，"
                         f"新问题 {len(new_qs)} 个")

                # 规则筛选
                candidates = self._apply_story_filter(new_qs)
                candidates.sort(
                    key=lambda q: (q.get('is_hot', False),
                                   q.get('score', 0)),
                    reverse=True
                )
                if not candidates:
                    log.info("  筛选后无可用问题")
                    _advance_to_next_screen("筛选后无可用问题")
                    continue

                # 取前 N 个进入提取
                remaining = target - len(materials)
                if ENABLE_MATERIAL_LIKES_GATE:
                    pick = min(BATCH_QUESTIONS_PER_PAGE, len(candidates))
                else:
                    pick = min(BATCH_QUESTIONS_PER_PAGE,
                               len(candidates), remaining)
                to_enter = candidates[:pick]

                log.info(f"  本轮进入 {pick} 个问题：")
                for i, q in enumerate(to_enter):
                    hot = " [飙升]" if q.get('is_hot') else ""
                    log.info(f"    {i+1}. {q['title'][:40]}...{hot}")

                for i, q in enumerate(to_enter):
                    if len(materials) >= target:
                        break
                    if total_attempts >= MAX_TOTAL_ATTEMPTS:
                        log.warning("  已达到最大采集尝试数 "
                                    f"{MAX_TOTAL_ATTEMPTS}，停止采集")
                        break

                    visited_titles.add(q['title'])
                    total_attempts += 1
                    log.info(f"\n  进入 {i+1}/{pick}："
                             f"{q['title'][:40]}...")

                    try:
                        pyautogui.moveTo(
                            q['click_x'], q['click_y'],
                            duration=random_mouse_duration()
                        )
                        time.sleep(WAIT_FOCUS_SETTLE / 2)
                        pyautogui.click(button='middle')
                        time.sleep(WAIT_QUESTION_ENTER)

                        pyautogui.hotkey('ctrl', 'Tab')
                        time.sleep(WAIT_ZHIHU_PAGE_LOAD)

                        pyautogui.press('pagedown')
                        time.sleep(WAIT_ANSWER_LOAD_TRIGGER)
                        pyautogui.hotkey('ctrl', 'Home')
                        time.sleep(WAIT_AFTER_HOME)

                        can_answer, reason = self._check_question_answerable()
                        if not can_answer:
                            log.info(f"  ⏭ {reason}")
                            continue

                        url = grab_current_url()
                        title, answer, footer = self._extract_answer_with_fallback(
                            lx, rx, ty, by
                        )

                        if (title and answer
                                and len(answer) >= MIN_ANSWER_LENGTH):
                            likes = None
                            if footer:
                                likes = footer.get('likes')

                            if ENABLE_MATERIAL_LIKES_GATE:
                                if likes is None:
                                    if unknown_likes_policy == "drop":
                                        log.info("    ✗ 赞同数未识别，"
                                                 "按配置跳过素材")
                                        continue
                                    log.info("    · 赞同数未识别，按配置保留")
                                elif likes < MATERIAL_MIN_LIKES:
                                    log.info("    ✗ 赞同数不足："
                                             f"{likes} < {MATERIAL_MIN_LIKES}，"
                                             "跳过素材")
                                    continue

                            materials.append({
                                'title': title,
                                'answer': answer,
                                'url': url,
                                'index': len(materials) + 1,
                                'footer': footer,
                            })
                            footer_tag = ""
                            if footer:
                                footer_tag = (
                                    f"｜赞{footer.get('likes', 0)} "
                                    f"评{footer.get('comments', 0)} "
                                    f"藏{footer.get('collects', 0)} "
                                    f"喜{footer.get('hearts', 0)}"
                                )
                            log.info(f"    ✓ 素材 {len(materials)}/{target}"
                                     f"（{len(answer)}字{footer_tag}）")
                            self._fire_recipe_extraction(
                                title, answer, len(materials) - 1
                            )
                        else:
                            log.warning(f"    ✗ 提取失败或过短"
                                        f"（{len(answer or '')}字）")

                    except Exception as e:
                        log.error(f"    ✗ 出错：{e}")

                    finally:
                        close_current_tab()
                        time.sleep(WAIT_CLOSE_TAB)

                # 本屏提取完毕，翻到下一屏
                if len(materials) < target and total_attempts < MAX_TOTAL_ATTEMPTS:
                    pyautogui.press('pagedown')
                    time.sleep(WAIT_NEXT_SCREEN)

        log.info(f"\n  素材收集完成：{len(materials)}/{target}"
                 f"（共 {total_scrolls} 屏，"
                 f"刷新推荐页 {refresh_count} 次，"
                 f"访问 {len(visited_titles)} 个问题，"
                 f"尝试提取 {total_attempts} 次）")
        return materials
