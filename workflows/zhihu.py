# ============================================================
# workflows/zhihu.py — 知乎工作流（DOM 驱动）
#
# 实现知乎平台专属的 4 个步骤。所有浏览器操作与检测均经
# browser_adapter 的 DOM 语义接口完成，与物理鼠标/屏幕坐标/OCR
# 完全解绑（运行期间用户可干其他事，换分辨率/换电脑不受影响）：
#   步骤1：选题  —— 推荐页 DOM 卡片解析（含热度标签）+ 规则筛选 + 评分
#   步骤2：提取  —— 问题页首答 DOM 提取 + 「撤销删除」DOM 检测
#   步骤3：（继承基类）生成故事（可注入作者技能，见 AUTHOR_PROFILE）
#   步骤4：发布  —— 写回答 → 编辑器直接写入故事全文；成功判定走
#                    服务端草稿 API 轮询（前端 toast 在程序化写入后
#                    不可靠，以草稿内容为准——可验证）
#
# 降级通道（仅 DOM 主通道失败时启用）：
#   - 提取：UIA/OCR 旧屏幕通道（_extract_answer_with_fallback）
#   - 发布：无 —— 导入文档上传（set_input_files）不可靠（上传 API 全
#     200 但服务端草稿不更新），编辑器直接写入是唯一可验证通道
#
# 批量素材收集：滚动推荐页 → 解析卡片 → 新开 tab 提取，
# 结构与原 OCR 版一致（外层刷新循环 / 内层逐屏滚动）。
# ============================================================

import os
import time
import logging

from workflows.base import WorkflowBase

log = logging.getLogger(__name__)


class ZhihuWorkflow(WorkflowBase):
    """知乎平台工作流：浏览器操作全部走 DOM 通道。"""

    name = "zhihu"

    def __init__(self):
        from applications.zhihu_story.config import AUTHOR_PROFILE
        # 作者技能注入：生成时把该作者的蒸馏技能 profile 注入 prompt
        self.author = AUTHOR_PROFILE or None

    # ============================================================
    # 浏览器通道（全局共享单例）
    # ============================================================

    def _browser(self):
        from applications.zhihu_story.browser_adapter import get_browser
        return get_browser()

    def _require_login(self, browser):
        if not browser.is_logged_in():
            raise RuntimeError(
                "知乎登录态失效，请先运行：\n"
                "  python -m applications.zhihu_story.browser_adapter --login")

    # ============================================================
    # 步骤1：选题（DOM）
    # ============================================================

    def select_topic(self):
        from applications.zhihu_story.config import QUESTION_SELECT_MODE

        log.info("=" * 50)
        log.info(f"步骤 1：选题（{QUESTION_SELECT_MODE}，DOM 通道）")
        log.info("=" * 50)

        browser = self._browser()
        self._require_login(browser)

        if QUESTION_SELECT_MODE == "auto":
            return self._select_auto(browser)
        return self._select_manual(browser)

    def _scan_recommend(self, browser, retries=2):
        """DOM 扫描推荐页：卡片解析（含热度标签）→ 评分。

        返回 (all_qs, hot_qs, normal_qs)；重试仍失败返回 (None, None, None)。
        推荐页偶尔风控/加载抖动导致卡片为空，重试可自愈。
        """
        for attempt in range(retries + 1):
            browser.open_recommend_page()
            all_qs = browser.get_recommend_questions(max_cards=40)
            if all_qs:
                for q in all_qs:
                    q["score"] = self._dom_score(q)
                hot_qs = [q for q in all_qs if q.get("is_hot")]
                normal_qs = [q for q in all_qs if not q.get("is_hot")]
                return all_qs, hot_qs, normal_qs
            log.warning(f"  推荐页解析为空（第 {attempt + 1}/"
                        f"{retries + 1} 次），等待后重试")
            time.sleep(3)
        return None, None, None

    @staticmethod
    def _dom_score(q):
        """DOM 卡片评分：主信号×(次信号+1)，热度标签 ×2。

        两种候选页信号不同，自适应：
          - 创作中心推荐页：关注/回答（followers/answers）
          - 首页推荐流：赞/评论（likes/comments）
        含义一致——互动越强越优先。"""
        main = q.get("likes") or q.get("followers") or 0
        sec = q.get("comments") or q.get("answers") or 0
        score = main * (sec + 1)
        if q.get("is_hot"):
            score *= 2
        return score

    def _select_auto(self, browser):
        """全自动选题：DOM 解析 → 热度检测 → 规则筛选 → 评分选最优。

        筛选为空（整页无故事类问题）时滚动推荐页扩池重扫，最多
        MAX_SELECT_SCREENS 屏；仍无命中则明确报错——绝不静默选非
        故事热门话题（线上曾因此选到「美伊战争」）。
        """
        from applications.zhihu_story.config import (
            ENABLE_STORY_FILTER, MAX_SELECT_SCREENS)

        all_qs, hot_qs, normal_qs = self._scan_recommend(browser)
        if not all_qs:
            raise RuntimeError(
                "推荐页 DOM 解析为空（登录态/网络/页面结构，可重试）")

        log.info(f"  DOM 解析到 {len(all_qs)} 个问题"
                 f"（飙升 {len(hot_qs)} / 普通 {len(normal_qs)}）")
        for i, q in enumerate(all_qs[:15]):
            hot_flag = " [飙升]" if q.get("is_hot") else ""
            sig = (f"赞={q.get('likes')} 评={q.get('comments')}"
                   if q.get("likes") is not None else
                   f"关注={q.get('followers')} 答={q.get('answers')}")
            log.info(f"    {i+1}. {q['title'][:35]}{hot_flag} "
                     f"score={q['score']:.0f} {sig}")

        best = self._pick_best(all_qs, hot_qs, normal_qs)

        # 筛选为空 → 滚动扩池重扫（瀑布流要滚动才渲染更多卡片）
        seen = {q["href"] for q in all_qs}
        screen = 0
        while (best is None and ENABLE_STORY_FILTER
               and screen < MAX_SELECT_SCREENS):
            screen += 1
            log.warning(f"  规则筛选无命中，滚动第 {screen}/"
                        f"{MAX_SELECT_SCREENS} 屏找故事类问题")
            browser.scroll_feed()
            fresh = [q for q in browser.get_recommend_questions(max_cards=60)
                     if q.get("href") not in seen]
            if not fresh:
                continue
            seen.update(q["href"] for q in fresh)
            for q in fresh:
                q["score"] = self._dom_score(q)
            all_qs = all_qs + fresh
            hot_qs = hot_qs + [q for q in fresh if q.get("is_hot")]
            normal_qs = normal_qs + [q for q in fresh
                                     if not q.get("is_hot")]
            best = self._pick_best(all_qs, hot_qs, normal_qs)

        if best is None:
            raise RuntimeError(
                "推荐页扫描多屏均未发现故事类问题（关键词白名单无"
                "命中）。可重试（推荐页内容会刷新），或把 "
                "QUESTION_SELECT_MODE 改为 manual 手动选题。")

        log.info("")
        log.info("最终候选（前5）：")
        final_pool = hot_qs if hot_qs else all_qs
        final_pool.sort(key=lambda q: q["score"], reverse=True)
        for i, q in enumerate(final_pool[:5]):
            hot = " [飙升]" if q.get("is_hot") else ""
            story = " [故事]" if q.get("is_story") else ""
            marker = " ← 选择" if q is best else ""
            sig = (f"赞={q.get('likes')} 评={q.get('comments')}"
                   if q.get("likes") is not None else
                   f"关注={q.get('followers')} 答={q.get('answers')}")
            log.info(f"  {i+1}. {q['title'][:40]}{hot}{story}{marker}")
            log.info(f"     {sig} score={q['score']:.0f}")

        log.info("")
        log.info(f"✓ 最终选择：{best['title'][:50]}...")
        browser.open_question(best["href"])
        return best["href"]

    def _select_manual(self, browser):
        """手动选题：DOM 解析供参考，输入编号进入（无需鼠标）。"""
        all_qs, hot_qs, normal_qs = self._scan_recommend(browser)
        if not all_qs:
            raise RuntimeError(
                "推荐页 DOM 解析为空（登录态/网络/页面结构，可重试）")

        # 规则筛选（替代 LLM 筛选）
        normal_filtered = self._apply_story_filter(normal_qs) or normal_qs
        candidates = hot_qs + normal_filtered
        candidates.sort(key=lambda q: (bool(q.get("is_hot")), q["score"]),
                        reverse=True)

        log.info("  候选问题（按热度/评分排序）：")
        for i, q in enumerate(candidates[:15]):
            hot = " [飙升]" if q.get("is_hot") else ""
            story = " [故事]" if q.get("is_story") else ""
            log.info(f"    {i+1}. {q['title'][:40]}{hot}{story} "
                     f"| 赞={q.get('likes')} 评={q.get('comments')}")

        log.info(">>> 请输入要进入的问题编号（回车默认第 1 个）")
        choice = input(">> ").strip()
        idx = int(choice) - 1 if choice.isdigit() else 0
        if not 0 <= idx < len(candidates):
            raise RuntimeError(f"编号超出范围（1-{len(candidates)}）")
        best = candidates[idx]
        browser.open_question(best["href"])
        return best["href"]

    def _material_likes_pass(self, likes, min_likes):
        """点赞门槛判定（batch 与 single 共用）。返回 (通过?, 原因)。

        - gate 关闭 → 恒通过
        - likes 未识别 → 按 MATERIAL_UNKNOWN_LIKES_POLICY
          （keep=保留 / drop=跳过，drop 也记「未识别」原因便于排查）
        - likes < min_likes → 不通过，附数字原因
        """
        from applications.zhihu_story.config import (
            ENABLE_MATERIAL_LIKES_GATE,
            MATERIAL_UNKNOWN_LIKES_POLICY,
        )
        if not ENABLE_MATERIAL_LIKES_GATE:
            return True, ""
        if likes is None:
            if str(MATERIAL_UNKNOWN_LIKES_POLICY).lower() == "keep":
                return True, "likes 未识别（keep 策略保留）"
            return False, "likes 未识别（drop 策略跳过）"
        if likes < min_likes:
            return False, f"likes {likes} < {min_likes}"
        return True, ""

    def _apply_story_filter(self, questions):
        """规则筛选：用关键词白名单过滤非故事类问题（替代 LLM 筛选）。"""
        if not questions:
            return questions
        from applications.zhihu_story.config import (
            ENABLE_STORY_FILTER, STORY_INCLUDE_KEYWORDS)
        if not ENABLE_STORY_FILTER:
            return questions

        filtered = []
        for q in questions:
            title = q.get("title", "")
            if any(kw in title for kw in STORY_INCLUDE_KEYWORDS):
                q["is_story"] = True
                filtered.append(q)

        if filtered:
            log.info(f"  规则筛选：{len(questions)}→{len(filtered)}"
                     f"（保留 {len(filtered)} 个）")
        else:
            log.info("  规则筛选后无可用问题")

        return filtered

    def _pick_best(self, all_questions, hot_questions, normal_questions):
        """从候选中选出最优问题；无合格候选返回 None（绝不回退）。

        ★ 规则筛选强制生效：筛选为空时不能回退到未筛选列表——否则
        会选到「美伊战争」这类与故事写作无关的热门话题（线上翻车
        正是这个回退：日志「规则筛选后无可用问题」后按分数选了
        美伊战争）。返回 None 由调用方滚动扩池或明确报错。
        """
        from applications.zhihu_story.config import ENABLE_STORY_FILTER

        def keep(qs):
            return self._apply_story_filter(qs) if ENABLE_STORY_FILTER else qs

        if hot_questions:
            log.info("走飙升优先分支")
            if len(hot_questions) == 1:
                log.info(f"  唯一飙升问题：{hot_questions[0]['title'][:40]}...")
            kept = keep(hot_questions)
            if kept:
                kept.sort(key=lambda q: q["score"], reverse=True)
                return kept[0]
            log.warning("  飙升问题均被规则排除，回退到普通问题")
            kept_normal = keep(normal_questions) if normal_questions else []
            if kept_normal:
                kept_normal.sort(key=lambda q: q["score"], reverse=True)
                return kept_normal[0]
            return None

        log.info("无飙升，走综合评分分支")
        kept = keep(all_questions)
        if not kept:
            log.info("  规则筛选后无可用问题")
            return None
        kept.sort(key=lambda q: q["score"], reverse=True)
        return kept[0]

    # ============================================================
    # 步骤2：提取回答（DOM 主通道 + UIA/OCR 降级）
    # ============================================================

    def _extract_answer_with_fallback(self):
        """降级通道：UIA 首答 → OCR 滚屏（DOM 主通道失败时启用）。

        注意：此通道读取屏幕，需要 playwright Edge 窗口可见。
        """
        from applications.zhihu_story.config import (
            ENABLE_UIA_ANSWER_EXTRACTION,
            UIA_ANSWER_WAIT_TIMEOUT,
            UIA_ANSWER_POLL_INTERVAL,
            MIN_ANSWER_LENGTH,
            MAX_ANSWER_RETRIES,
            ENABLE_MATERIAL_LIKES_GATE,
        )
        from applications.zhihu_story.extractors import (
            UiaAnswerExtractor,
            OcrAnswerExtractor,
            FallbackAnswerExtractor,
        )
        from desktop_utils import get_bounds

        primary = None
        if ENABLE_UIA_ANSWER_EXTRACTION:
            primary = UiaAnswerExtractor(
                min_length=MIN_ANSWER_LENGTH,
                wait_timeout=UIA_ANSWER_WAIT_TIMEOUT,
                poll_interval=UIA_ANSWER_POLL_INTERVAL,
            )
        lx, rx, ty, by = get_bounds()
        fallback = OcrAnswerExtractor(
            lx, rx, ty, by,
            min_length=MIN_ANSWER_LENGTH,
            max_retries=MAX_ANSWER_RETRIES,
        )
        extractor = FallbackAnswerExtractor(
            primary, fallback, require_likes=ENABLE_MATERIAL_LIKES_GATE
        )
        return extractor.extract()

    def extract_content(self, fast_mode=False):
        """DOM 提取知乎问题标题和首答。

        主通道：check_answerable（DOM 检测「撤销删除」）+
        get_primary_answer（DOM 提取正文与互动数据）。

        首答过短或问题不可回答时重新选题再试（MAX_TOPIC_RETRY 次），
        而不是直接降级 OCR——本机 OCR 未校准，降级只在 DOM 多次尝试
        全部失败后作为最后手段。
        """
        from applications.zhihu_story.config import (
            MIN_ANSWER_LENGTH,
            ENABLE_DOM_ANSWER_EXTRACTION,
        )
        from applications.zhihu_story.browser_adapter import (
            normalize_question_url)

        log.info("=" * 50)
        log.info("步骤 2：自动提取标题和回答（DOM 通道）")
        log.info("=" * 50)

        browser = self._browser()
        MAX_TOPIC_RETRY = 3
        gate_reject_count = 0   # 点赞门槛拒绝计数（重试耗尽时提示）

        for attempt in range(MAX_TOPIC_RETRY + 1):
            if attempt > 0:
                log.warning(f"  首答过短或不可回答，重新选题"
                            f"（第 {attempt}/{MAX_TOPIC_RETRY} 次）")
                self.select_topic()
            # 确保当前停在问题页（幂等重进，防止页面被外部跳转）
            url = normalize_question_url(browser.page.url)
            if url:
                browser.open_question(url)

            # ★ 先快速检测本问题是否可回答（DOM 替代 OCR「撤销删除」）
            try:
                can_answer, reason = browser.check_answerable()
            except Exception as exc:
                # 页面异常（风控/空壳/超时）视为不可判定，重新选题
                log.warning(f"  可回答性检测异常：{exc}")
                continue
            if not can_answer:
                log.warning(f"  问题不可回答：{reason}")
                continue

            if ENABLE_DOM_ANSWER_EXTRACTION:
                try:
                    data = browser.get_primary_answer(min_length=1)
                except Exception as exc:
                    log.warning(f"  DOM 提取异常：{exc}，重新选题")
                    continue
                answer = (data or {}).get("answer") or ""
                if len(answer) >= MIN_ANSWER_LENGTH:
                    title = (data or {}).get("title") or ""
                    footer = (data or {}).get("footer") or {}
                    # ★ 点赞门槛（与 batch 收集同一判定）：未达最低
                    # 赞同数的题目重新选题，避免 low-quality 素材入库
                    from applications.zhihu_story.config import (
                        MATERIAL_MIN_LIKES)
                    pass_likes, like_reason = self._material_likes_pass(
                        (footer or {}).get("likes"), MATERIAL_MIN_LIKES)
                    if not pass_likes:
                        gate_reject_count += 1
                        log.warning(f"  点赞门槛未过：{like_reason}"
                                    f"，重新选题")
                        continue
                    # ★ 返回最终实际提取的问题 URL：不可回答重选题后
                    # 不能沿用首次选题的 URL（否则发布导航到被跳过的题）
                    final_url = normalize_question_url(browser.page.url) or url
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
                    return title, answer, footer, final_url
                log.warning(f"  首答过短（{len(answer)} 字 < "
                            f"{MIN_ANSWER_LENGTH}），尝试下一题")
            else:
                break  # DOM 提取被显式关闭，走 OCR 降级

        # DOM 多次尝试全部失败，最后才降级 UIA/OCR 屏幕通道
        if gate_reject_count:
            log.warning(f"  DOM 通道多次尝试未获合格首答"
                        f"（其中 {gate_reject_count} 次被点赞门槛拒绝），"
                        f"降级 UIA/OCR 屏幕通道")
        title, answer, footer = self._extract_answer_with_fallback()

        if not title or not answer or len(answer) < MIN_ANSWER_LENGTH:
            raise RuntimeError(
                f"提取失败：标题={len(title or '')}字 "
                f"回答={len(answer or '')}字")

        log.info(f"提取成功！标题：{title[:50]}... | "
                 f"回答：{len(answer)}字符")
        from applications.zhihu_story.config import (
            ENABLE_MATERIAL_LIKES_GATE, MATERIAL_MIN_LIKES)
        if ENABLE_MATERIAL_LIKES_GATE:
            likes = (footer or {}).get("likes")
            log.warning(
                f"  ⚠ 降级路径不强制点赞门槛（gate 已开："
                f"要求 ≥{MATERIAL_MIN_LIKES}，实测赞={likes}）——"
                f"如需严格执行请重新运行")
        final_url = normalize_question_url(browser.page.url) or url
        return title, answer, footer, final_url

    # ============================================================
    # 步骤4：发布到知乎（DOM）
    # ============================================================

    def publish(self, story, title, url, md_path=None):
        """DOM 发布：写回答 → 编辑器直接写入故事全文。

        ★ 不采用导入文档上传：上传 API 全 200 但服务端草稿不更新
        （知乎程序化导入落盘不可靠，仅空草稿时偶发成功）。编辑器
        直接写入会触发自动保存，成功判定轮询服务端草稿 API（可验证）。
        """
        log.info("=" * 50)
        log.info("步骤 4：发布故事到知乎（DOM 通道）")
        log.info("=" * 50)

        # 保存 .md 留档（供人工核对/重发）
        if md_path and os.path.exists(md_path):
            md_abs_path = os.path.abspath(md_path)
            log.info(f"使用已有文件：{md_abs_path}")
        else:
            md_abs_path = self.save_story_file(story)
            log.info(f"故事已保存：{md_abs_path}")

        browser = self._browser()
        self._require_login(browser)
        # 发布前强制导航一次：生成耗时数分钟，页面可能已滞留/漂移；
        # 一次性定位到目标 URL（幂等跳过只用于提取环节的重进）
        browser.open_question(url, force=True)

        saved = browser.publish_story(story, question_url=url)
        if not saved:
            raise RuntimeError(
                "编辑器写入后服务端草稿未在等待窗口内确认"
                "（请打开浏览器人工确认草稿后手动发布）")
        log.info("  服务端草稿已确认（编辑器写入通道）")

        # 不再收尾 reload：草稿已落盘，刷新只会制造一次多余的页面
        # 加载（对用户观感就是「又跳了一下」），且破坏验收时的编辑器态
        log.info(f"草稿已保存，完成：「{title[:30]}...」")
        return md_abs_path

    # ============================================================
    # 批量素材收集（DOM 通道）
    # ============================================================

    def collect_materials_batch(self, target):
        """
        批量素材收集：滚动推荐页 + 新开 tab 提取（DOM 通道）。

        流程（与原 OCR 版结构一致，仅替换操作方式）：
        1. 打开推荐页
        2. DOM 解析 → 规则筛选 → 取前 N 个新开 tab 提取
        3. 滚动下一屏，重复步骤 2
        4. 滚满 SCROLLS_PER_REFRESH 轮后重新打开推荐页刷新内容
        5. 循环直到采够 target 篇
        """
        from applications.zhihu_story.config import (
            MIN_ANSWER_LENGTH,
            BATCH_QUESTIONS_PER_PAGE,
            SCROLLS_PER_REFRESH,
            MATERIAL_MIN_LIKES,
            MAX_TOTAL_ATTEMPTS,
            ENABLE_DOM_ANSWER_EXTRACTION,
        )

        browser = self._browser()
        self._require_login(browser)
        main_page = browser.page

        materials = []
        visited_titles = set()
        refresh_count = 0
        total_scrolls = 0
        total_attempts = 0

        def _advance_to_next_screen(page_idx, reason):
            """当前屏无可采内容时滚动下翻，避免重复解析同一屏。"""
            if len(materials) >= target:
                return
            if page_idx < SCROLLS_PER_REFRESH - 1:
                log.info(f"  {reason}，翻到下一屏")
                browser.scroll_feed()
            else:
                log.info(f"  {reason}，本轮推荐页扫描结束")

        # ── 外层：刷新循环 ──
        while len(materials) < target and total_attempts < MAX_TOTAL_ATTEMPTS:
            refresh_count += 1
            log.info(f"\n{'='*40}")
            log.info(f"  🔄 推荐页第 {refresh_count} 次加载"
                     f"（已采集 {len(materials)}/{target}）")
            log.info(f"{'='*40}")

            browser.open_recommend_page()

            # ── 内层：逐屏滚动 ──
            for page_idx in range(SCROLLS_PER_REFRESH):
                if len(materials) >= target:
                    break
                if total_attempts >= MAX_TOTAL_ATTEMPTS:
                    break

                total_scrolls += 1
                log.info(f"\n  ── 第 {total_scrolls} 屏"
                         f"（已采集 {len(materials)}/{target}）──")

                # DOM 解析当前屏
                all_questions = browser.get_recommend_questions(max_cards=60)
                if not all_questions:
                    log.warning("  未识别到问题")
                    _advance_to_next_screen(page_idx, "未识别到问题")
                    continue

                # 去重
                new_qs = [q for q in all_questions
                          if q["title"] not in visited_titles]
                if not new_qs:
                    msg = f"当前屏 {len(all_questions)} 个问题全部已访问"
                    log.info(f"  {msg}")
                    _advance_to_next_screen(page_idx, msg)
                    continue

                log.info(f"  可见 {len(all_questions)} 个，"
                         f"新问题 {len(new_qs)} 个")

                # 规则筛选 + 评分排序
                candidates = self._apply_story_filter(new_qs)
                if not candidates:
                    log.info("  筛选后无可用问题")
                    _advance_to_next_screen(page_idx, "筛选后无可用问题")
                    continue
                for q in candidates:
                    q["score"] = self._dom_score(q)
                candidates.sort(
                    key=lambda q: (q.get("is_hot", False), q["score"]),
                    reverse=True,
                )

                # 取前 N 个进入提取
                pick = min(BATCH_QUESTIONS_PER_PAGE, len(candidates))
                to_enter = candidates[:pick]

                log.info(f"  本轮进入 {pick} 个问题：")
                for i, q in enumerate(to_enter):
                    hot = " [飙升]" if q.get("is_hot") else ""
                    log.info(f"    {i+1}. {q['title'][:40]}...{hot}")

                for i, q in enumerate(to_enter):
                    if len(materials) >= target:
                        break
                    if total_attempts >= MAX_TOTAL_ATTEMPTS:
                        log.warning("  已达到最大采集尝试数 "
                                    f"{MAX_TOTAL_ATTEMPTS}，停止采集")
                        break

                    visited_titles.add(q["title"])
                    total_attempts += 1
                    log.info(f"\n  进入 {i+1}/{pick}："
                             f"{q['title'][:40]}...")

                    page_tab = None
                    try:
                        # 新开 tab 提取（替代 中键+ctrl+Tab）
                        page_tab = browser.open_new_page(q["href"])
                        browser.switch_page(page_tab)
                        page_tab.wait_for_timeout(2500)

                        can_answer, reason = browser.check_answerable()
                        if not can_answer:
                            log.info(f"  ⏭ {reason}")
                            continue

                        title, answer, footer = None, None, None
                        if ENABLE_DOM_ANSWER_EXTRACTION:
                            data = browser.get_primary_answer(min_length=1)
                            if data and data.get("answer"):
                                title = data.get("title")
                                answer = data.get("answer")
                                footer = data.get("footer") or {}

                        if not (title and answer
                                and len(answer) >= MIN_ANSWER_LENGTH):
                            log.warning(f"    ✗ DOM 提取失败或过短"
                                        f"（{len(answer or '')}字）")
                            continue

                        likes = (footer or {}).get("likes")
                        pass_likes, like_reason = self._material_likes_pass(
                            likes, MATERIAL_MIN_LIKES)
                        if not pass_likes:
                            log.info(f"    ✗ 点赞门槛未过：{like_reason}"
                                     f"，跳过素材")
                            continue

                        materials.append({
                            "title": title,
                            "answer": answer,
                            "url": q["href"],
                            "index": len(materials) + 1,
                            "footer": footer or {},
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

                    except Exception as e:
                        log.error(f"    ✗ 出错：{e}")

                    finally:
                        if page_tab is not None:
                            browser.close_page(page_tab)
                            browser.switch_page(main_page)

                # 本屏提取完毕，翻到下一屏
                if len(materials) < target and total_attempts < MAX_TOTAL_ATTEMPTS:
                    browser.scroll_feed()

        log.info(f"\n  素材收集完成：{len(materials)}/{target}"
                 f"（共 {total_scrolls} 屏，"
                 f"刷新推荐页 {refresh_count} 次，"
                 f"访问 {len(visited_titles)} 个问题，"
                 f"尝试提取 {total_attempts} 次）")
        return materials
