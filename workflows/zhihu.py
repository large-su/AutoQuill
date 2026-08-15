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
# 通道约束（V4.0.2 起纯 DOM，无屏幕降级）：
#   - 提取：无降级 —— UIA/OCR 旧屏幕通道已移除；重试耗尽直接报错
#     （错误信息含点赞门槛拒绝次数统计）
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
        from config.story import AUTHOR_PROFILE
        # 作者技能注入：生成时把该作者的蒸馏技能 profile 注入 prompt
        self.author = AUTHOR_PROFILE or None

    # ============================================================
    # 浏览器通道（全局共享单例）
    # ============================================================

    def _browser(self):
        from web_drivers.browser_pool import get_browser
        return get_browser()

    def _require_login(self, browser):
        if not browser.is_logged_in():
            raise RuntimeError(
                "知乎登录态失效或尚未登录。\n"
                "请点击控制台右上角「设置」→「登录知乎」完成登录后再运行。")

    # ============================================================
    # 步骤1：选题（DOM）
    # ============================================================

    def select_topic(self, avoid=None):
        """选题；avoid 为已尝试过的问题 href 集合（重选时避开，
        否则候选池不变时会反复选到同一题直到重试耗尽）。"""
        from config.story import QUESTION_SELECT_MODE, QUESTION_SOURCE

        browser = self._browser()
        self._require_login(browser)

        # 自选问题：跳过选题环节，直接进入给定问题的提取
        if QUESTION_SOURCE == "custom":
            return self._select_custom(browser)

        log.info("=" * 50)
        log.info(f"步骤 1：选题（{QUESTION_SELECT_MODE}，DOM 通道）")
        log.info("=" * 50)

        if QUESTION_SELECT_MODE == "auto":
            return self._select_auto(browser, avoid=avoid)
        return self._select_manual(browser, avoid=avoid)

    def _source_url(self):
        """选题候选池 URL：跟随设置里的选题来源（默认推荐话题）。"""
        from config.story import (QUESTION_SOURCE, ZHIHU_INVITED_URL,
                                  ZHIHU_RECOMMEND_URL)
        if QUESTION_SOURCE == "invited":
            return ZHIHU_INVITED_URL
        return ZHIHU_RECOMMEND_URL

    def _scan_recommend(self, browser, url=None, retries=2):
        """DOM 扫描候选页：卡片解析（含热度标签）→ 评分。

        url 为选题候选池（推荐话题/邀请回答页，None 走默认推荐页）；
        返回 (all_qs, hot_qs, normal_qs)；重试仍失败返回 (None, None, None)。
        候选页偶尔风控/加载抖动导致卡片为空，重试可自愈。
        """
        for attempt in range(retries + 1):
            browser.open_recommend_page(url)
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

    def _select_auto(self, browser, avoid=None):
        """全自动选题：DOM 解析 → 热度检测 → 规则筛选 → 评分选最优。

        筛选为空（整页无故事类问题）时滚动推荐页扩池重扫，最多
        MAX_SELECT_SCREENS 屏；仍无命中则明确报错——绝不静默选非
        故事热门话题（线上曾因此选到「美伊战争」）。
        avoid：已尝试问题 href 集合，选优与扩池时一并排除。"""
        from config.story import (
            ENABLE_STORY_FILTER, MAX_SELECT_SCREENS)
        avoid = avoid or set()

        all_qs, hot_qs, normal_qs = self._scan_recommend(
            browser, self._source_url())
        if not all_qs:
            raise RuntimeError(
                "候选页 DOM 解析为空（登录态/网络/页面结构，可重试）")

        log.info(f"  DOM 解析到 {len(all_qs)} 个问题"
                 f"（飙升 {len(hot_qs)} / 普通 {len(normal_qs)}）")
        for i, q in enumerate(all_qs[:15]):
            hot_flag = " [飙升]" if q.get("is_hot") else ""
            sig = (f"赞={q.get('likes')} 评={q.get('comments')}"
                   if q.get("likes") is not None else
                   f"关注={q.get('followers')} 答={q.get('answers')}")
            log.info(f"    {i+1}. {q['title'][:35]}{hot_flag} "
                     f"score={q['score']:.0f} {sig}")

        best = self._pick_best(all_qs, hot_qs, normal_qs, avoid)

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
                     if q.get("href") not in seen
                     and q.get("href") not in avoid]
            if not fresh:
                continue
            seen.update(q["href"] for q in fresh)
            for q in fresh:
                q["score"] = self._dom_score(q)
            all_qs = all_qs + fresh
            hot_qs = hot_qs + [q for q in fresh if q.get("is_hot")]
            normal_qs = normal_qs + [q for q in fresh
                                     if not q.get("is_hot")]
            best = self._pick_best(all_qs, hot_qs, normal_qs, avoid)

        if best is None:
            tried = (f"，已尝试 {len(avoid)} 个候选均不满足要求"
                     if avoid else "")
            raise RuntimeError(
                "推荐页扫描多屏均未发现故事类问题（关键词白名单无"
                f"命中{tried}）。可重试（推荐页内容会刷新），或把 "
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

    def _select_manual(self, browser, avoid=None):
        """手动选题：DOM 解析供参考，输入编号进入（无需鼠标）。
        avoid：已尝试问题 href 集合（重试时排除，避免再次进入）。"""
        avoid = avoid or set()
        all_qs, hot_qs, normal_qs = self._scan_recommend(
            browser, self._source_url())
        if not all_qs:
            raise RuntimeError(
                "候选页 DOM 解析为空（登录态/网络/页面结构，可重试）")

        # 规则筛选（替代 LLM 筛选）
        normal_filtered = self._apply_story_filter(normal_qs) or normal_qs
        candidates = hot_qs + normal_filtered
        if avoid:
            candidates = [q for q in candidates if q["href"] not in avoid]
            if not candidates:
                raise RuntimeError(
                    f"候选 {len(avoid)} 个均已尝试过且不满足要求，"
                    "请更换选题来源或稍后重试")
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

    def _select_batch(self, browser, avoid=None, count=5):
        """批量选题：扫描候选池 → 规则筛选 → 评分排序 → 取前 count 个。

        与 _select_auto 同源（DOM 扫描 + 规则筛选 + 评分），但不打开
        任何页面——供并行提取一次取多个候选。候选不足时滚动扩池重扫
        （MAX_SELECT_SCREENS 屏上限）；仍无命中明确报错——绝不静默
        选非故事话题。avoid：已尝试问题 href 集合，选优与扩池排除。
        """
        from config.story import ENABLE_STORY_FILTER, MAX_SELECT_SCREENS
        avoid = avoid or set()

        all_qs, hot_qs, normal_qs = self._scan_recommend(
            browser, self._source_url())
        if not all_qs:
            raise RuntimeError(
                "候选页 DOM 解析为空（登录态/网络/页面结构，可重试）")

        def _rank(qs):
            kept = self._apply_story_filter(qs) if ENABLE_STORY_FILTER else qs
            kept.sort(key=lambda q: q["score"], reverse=True)
            return kept

        # 飙升组在前（与 _pick_best 的「飙升优先」语义一致）
        candidates = [q for q in _rank(hot_qs) + _rank(normal_qs)
                      if q["href"] not in avoid]
        seen = {q["href"] for q in all_qs}

        screen = 0
        while (len(candidates) < count and ENABLE_STORY_FILTER
               and screen < MAX_SELECT_SCREENS):
            screen += 1
            log.warning(f"  候选不足 {count} 个，滚动第 {screen}/"
                        f"{MAX_SELECT_SCREENS} 屏找故事类问题")
            browser.scroll_feed()
            fresh = [q for q in browser.get_recommend_questions(max_cards=60)
                     if q.get("href") not in seen
                     and q.get("href") not in avoid]
            if not fresh:
                continue
            seen.update(q["href"] for q in fresh)
            for q in fresh:
                q["score"] = self._dom_score(q)
            fresh_hot = [q for q in fresh if q.get("is_hot")]
            fresh_normal = [q for q in fresh if not q.get("is_hot")]
            have = {q["href"] for q in candidates}
            for q in _rank(fresh_hot) + _rank(fresh_normal):
                if q["href"] not in have:
                    candidates.append(q)

        if not candidates:
            tried = (f"，已尝试 {len(avoid)} 个候选均不满足要求"
                     if avoid else "")
            raise RuntimeError(
                "推荐页扫描多屏均未发现故事类问题（关键词白名单无"
                f"命中{tried}）。可重试（推荐页内容会刷新），或把 "
                "QUESTION_SELECT_MODE 改为 manual 手动选题。")

        log.info(f"  批量选题：{len(candidates)} 个候选，"
                 f"取前 {min(count, len(candidates))} 个并行提取")
        return candidates[:count]

    def _select_custom(self, browser):
        """自选问题模式：跳过选题环节，直接进入给定问题的提取。

        校验设置里的问题链接（normalize_question_url 可识别
        /question/{id}）；无效则明确报错提醒，绝不静默回退选题。"""
        from config.story import CUSTOM_QUESTION_URL
        from applications.zhihu_story.browser_adapter import (
            normalize_question_url)

        log.info("=" * 50)
        log.info("步骤 1：选题（自选问题，跳过选题）")
        log.info("=" * 50)

        url = normalize_question_url(CUSTOM_QUESTION_URL)
        if not url:
            raise RuntimeError(
                "自选问题网址无效：请在设置「选题来源」→「自选问题」"
                "中填入正确的知乎问题链接"
                "（https://www.zhihu.com/question/…）")
        log.info(f"  自选问题：{url}")
        browser.open_question(url)
        return url

    def _material_likes_pass(self, likes, min_likes):
        """点赞门槛判定（batch 与 single 共用）。返回 (通过?, 原因)。

        - gate 关闭 → 恒通过
        - likes 未识别 → 按 MATERIAL_UNKNOWN_LIKES_POLICY
          （keep=保留 / drop=跳过，drop 也记「未识别」原因便于排查）
        - likes < min_likes → 不通过，附数字原因
        """
        from config.story import (
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
        from config.story import (
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

    def _pick_best(self, all_questions, hot_questions, normal_questions,
                   avoid=None):
        """从候选中选出最优问题；无合格候选返回 None（绝不回退）。

        ★ 规则筛选强制生效：筛选为空时不能回退到未筛选列表——否则
        会选到「美伊战争」这类与故事写作无关的热门话题（线上翻车
        正是这个回退：日志「规则筛选后无可用问题」后按分数选了
        美伊战争）。返回 None 由调用方滚动扩池或明确报错。
        avoid：已尝试问题 href 集合，排除后再选优。"""
        from config.story import ENABLE_STORY_FILTER

        if avoid:
            all_questions = [q for q in all_questions
                             if q["href"] not in avoid]
            hot_questions = [q for q in hot_questions
                             if q["href"] not in avoid]
            normal_questions = [q for q in normal_questions
                                if q["href"] not in avoid]
            if not all_questions:
                log.info(f"  候选已全部尝试过（{len(avoid)} 个），"
                         "排除后无可用候选")

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
    # 步骤2：提取回答（DOM 通道，无屏幕降级）
    # ============================================================

    def _extract_batch_parallel(self, browser, questions):
        """并行打开并提取多个问题的首答（共享 context 多 page）。

        所有 goto 先发出再等待——页面加载在浏览器进程并行，Playwright
        sync API 单线程即可完成（规避线程亲和问题，与 web_drivers/
        parallel.py 同原则）。每页独立检测与提取，失败原因单独记录、
        不阻塞其他页。结束后把操作页切回主 page，不改变调用方状态。

        返回 [{q, can_answer, reason, title, answer, footer}]。
        """
        from applications.zhihu_story.browser_adapter import _NAV_TIMEOUT
        main_page = browser.page
        pages = []
        total = len(questions)
        log.info(f"  并行打开 {total} 个候选问题页（浏览器内并发加载）…")
        try:
            for i, q in enumerate(questions, 1):
                try:
                    p = browser.context.new_page()
                    p.goto(q["href"], wait_until="domcontentloaded",
                           timeout=_NAV_TIMEOUT)
                    pages.append((q, p))
                    log.info(f"    ✓ 已加载 {i}/{total}："
                             f"{q['title'][:28]}...")
                except Exception as exc:
                    log.warning(f"    ✗ 打开失败 {i}/{total}："
                                f"{q['title'][:28]}...（{exc}）")

            if not pages:
                log.warning("  本批候选页面全部打开失败，整批跳过")
                return []
            log.info(f"  ⚡ {len(pages)} 页已就绪，开始并行提取首答…")

            results = []
            for q, p in pages:
                browser.switch_page(p)
                try:
                    can_answer, reason = browser.check_answerable()
                    if not can_answer:
                        log.info(f"  ⏭ {q['title'][:30]}...：{reason}")
                        results.append({"q": q, "can_answer": False,
                                        "reason": reason})
                        continue
                    data = browser.get_primary_answer(min_length=1)
                    answer = (data or {}).get("answer") or ""
                    if not answer.strip():
                        log.warning(f"  ✗ {q['title'][:30]}...：提取失败")
                        results.append({"q": q, "can_answer": True,
                                        "reason": "提取失败（容器/超时）"})
                        continue
                    log.info(f"  ✓ {q['title'][:30]}...：{len(answer)}字")
                    results.append({"q": q, "can_answer": True, "reason": "",
                                    "title": (data.get("title") or "").strip(),
                                    "answer": answer,
                                    "footer": data.get("footer") or {}})
                except Exception as exc:
                    log.warning(f"  ✗ {q['title'][:30]}...：{exc}")
                    results.append({"q": q, "can_answer": False,
                                    "reason": f"提取异常：{exc}"})
                finally:
                    try:
                        p.close()
                    except Exception:
                        pass
            return results
        finally:
            browser.switch_page(main_page)

    def _extract_auto_parallel(self, browser, attempted):
        """全自动选题并行提取一批候选，取点赞最高的合格者。

        一批 PARALLEL_EXTRACT_LIMIT 个候选并行提取；过短/不可回答/
        低赞各自记录，不阻塞其他候选。返回 (title, answer, footer,
        final_url, gate_reject_count)；整批全败返回 None（调用方整批
        重选，attempted 已累计排除）。
        """
        from config.story import (
            MIN_ANSWER_LENGTH, MATERIAL_MIN_LIKES, PARALLEL_EXTRACT_LIMIT)
        from applications.zhihu_story.browser_adapter import (
            normalize_question_url)

        candidates = self._select_batch(browser, avoid=attempted,
                                        count=PARALLEL_EXTRACT_LIMIT)
        attempted.update(q["href"] for q in candidates)
        results = self._extract_batch_parallel(browser, candidates)

        gate_reject_count = 0
        good = []
        for r in results:
            answer = r.get("answer") or ""
            if not (r["can_answer"] and len(answer) >= MIN_ANSWER_LENGTH):
                continue
            footer = r.get("footer") or {}
            pass_likes, like_reason = self._material_likes_pass(
                footer.get("likes"), MATERIAL_MIN_LIKES)
            if not pass_likes:
                gate_reject_count += 1
                log.warning(f"  ✗ {r['q']['title'][:30]}...："
                            f"点赞门槛未过（{like_reason}）")
                continue
            good.append(r)
        if not good:
            log.warning("  本批候选均不合格（过短/不可回答/低赞），"
                        "整批重新选题")
            return None, gate_reject_count

        good.sort(key=lambda r: (r.get("footer") or {}).get("likes") or 0,
                  reverse=True)
        best = good[0]
        footer = best.get("footer") or {}
        final_url = (normalize_question_url(best["q"]["href"])
                     or best["q"]["href"])
        log.info(f"提取成功（并行批 {len(good)} 个合格取最优）！"
                 f"标题：{best['title'][:50]}... | "
                 f"回答：{len(best['answer'])}字符 | "
                 f"赞={footer.get('likes')}")
        return (best["title"], best["answer"], footer, final_url,
                gate_reject_count)

    def extract_content(self, fast_mode=False):
        """DOM 提取知乎问题标题和首答。

        主通道：check_answerable（DOM 检测「撤销删除」）+
        get_primary_answer（DOM 提取正文与互动数据）。

        首答过短或问题不可回答时重新选题再试（MAX_TOPIC_RETRY 次）；
        全部失败报错并给出失败原因统计（UIA/OCR 屏幕降级已移除——
        纯 DOM 通道，可无头运行）。
        """
        from config.story import (
            MIN_ANSWER_LENGTH,
            ENABLE_DOM_ANSWER_EXTRACTION,
            MAX_TOPIC_RETRY,
            QUESTION_SELECT_MODE, QUESTION_SOURCE,
        )
        from applications.zhihu_story.browser_adapter import (
            normalize_question_url)

        log.info("=" * 50)
        log.info("步骤 2：自动提取标题和回答（DOM 通道）")
        log.info("=" * 50)

        browser = self._browser()
        gate_reject_count = 0   # 点赞门槛拒绝计数（重试耗尽时提示）
        attempted = set()       # 已尝试问题 href（重选时避开，
                                # 防候选池不变时反复选同一题）

        # 全自动选题：一批并行提取候选，取点赞最高的合格者——串行试错
        # 改为整批并行，失败原因不阻塞其他候选（manual/custom/fast_mode
        # 走下方原串行路径，行为不变）
        parallel_enabled = (not fast_mode
                            and QUESTION_SELECT_MODE == "auto"
                            and QUESTION_SOURCE != "custom")

        for attempt in range(MAX_TOPIC_RETRY + 1):
            if parallel_enabled:
                if attempt > 0:
                    log.warning(f"  本批候选均不合格，重新批量选题"
                                f"（第 {attempt}/{MAX_TOPIC_RETRY} 批）")
                result = self._extract_auto_parallel(browser, attempted)
                if result is not None:
                    title, answer, footer, final_url, rejects = result
                    gate_reject_count += rejects
                    return title, answer, footer, final_url
                continue

            if attempt > 0:
                log.warning(f"  首答过短或不可回答，重新选题"
                            f"（第 {attempt}/{MAX_TOPIC_RETRY} 次）")
                self.select_topic(avoid=attempted)
            # 确保当前停在问题页（幂等重进，防止页面被外部跳转）
            url = normalize_question_url(browser.page.url)
            if url:
                attempted.add(url)
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
                    from config.story import (
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
                # DOM 提取被显式关闭：纯 DOM 通道无降级，直接失败
                raise RuntimeError(
                    "ENABLE_DOM_ANSWER_EXTRACTION 已关闭，但 UIA/OCR "
                    "屏幕降级通道已移除——请重新开启 DOM 提取后重试")

        # 全部重试耗尽：报错并给出门槛/质量失败统计（纯 DOM，无降级）
        reasons = []
        if gate_reject_count:
            reasons.append(f"{gate_reject_count} 次被点赞门槛拒绝")
        log.error("  DOM 通道尝试 %d 次未获合格首答%s",
                  MAX_TOPIC_RETRY + 1,
                  f"（其中 {', '.join(reasons)}）" if reasons else "")
        raise RuntimeError(
            f"提取失败：重试 {MAX_TOPIC_RETRY + 1} 次仍无合格首答"
            f"（首答过短或不可回答）"
            + (f"，其中 {gate_reject_count} 次被点赞门槛拒绝" if gate_reject_count
               else "")
            + "。可调低 config 中的 MIN_ANSWER_LENGTH / MATERIAL_MIN_LIKES 后重试")

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
        from config.story import (
            MIN_ANSWER_LENGTH,
            BATCH_QUESTIONS_PER_PAGE,
            SCROLLS_PER_REFRESH,
            MATERIAL_MIN_LIKES,
            MAX_TOTAL_ATTEMPTS,
            ENABLE_DOM_ANSWER_EXTRACTION,
        )

        browser = self._browser()
        self._require_login(browser)

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
            log.info(f"  🔄 候选页第 {refresh_count} 次加载"
                     f"（已采集 {len(materials)}/{target}）")
            log.info(f"{'='*40}")

            # 批量采集跟随选题来源（邀请回答/推荐话题）；
            # 自选问题模式仅支持单篇，批量时回退推荐页
            from config.story import QUESTION_SOURCE, ZHIHU_RECOMMEND_URL
            source_url = (ZHIHU_RECOMMEND_URL
                          if QUESTION_SOURCE == "custom"
                          else self._source_url())
            if QUESTION_SOURCE == "custom":
                log.warning("  自选问题模式仅用于单篇，批量采集回退「推荐话题」")
            browser.open_recommend_page(source_url)

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

                for q in to_enter:
                    visited_titles.add(q["title"])
                total_attempts += len(to_enter)

                if ENABLE_DOM_ANSWER_EXTRACTION:
                    # 并行提取本屏候选：所有页面加载在浏览器进程并行，
                    # 逐个等待不再串行累加（失败原因不阻塞其他候选）
                    results = self._extract_batch_parallel(browser, to_enter)
                else:
                    results = []
                for r in results:
                    if len(materials) >= target:
                        break
                    if total_attempts >= MAX_TOTAL_ATTEMPTS:
                        log.warning("  已达到最大采集尝试数 "
                                    f"{MAX_TOTAL_ATTEMPTS}，停止采集")
                        break

                    q = r["q"]
                    answer = r.get("answer") or ""
                    title = r.get("title") or ""
                    if not (r["can_answer"] and title
                            and len(answer) >= MIN_ANSWER_LENGTH):
                        log.warning(f"    ✗ DOM 提取失败或过短"
                                    f"（{len(answer)}字）")
                        continue

                    footer = r.get("footer") or {}
                    likes = footer.get("likes")
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

                # 本屏提取完毕，翻到下一屏
                if len(materials) < target and total_attempts < MAX_TOTAL_ATTEMPTS:
                    browser.scroll_feed()

        log.info(f"\n  素材收集完成：{len(materials)}/{target}"
                 f"（共 {total_scrolls} 屏，"
                 f"刷新推荐页 {refresh_count} 次，"
                 f"访问 {len(visited_titles)} 个问题，"
                 f"尝试提取 {total_attempts} 次）")
        return materials
