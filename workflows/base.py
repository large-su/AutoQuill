# ============================================================
# workflows/base.py — 工作流基类
#
# 定义内容创作工作流的标准生命周期：
#   选题 → 提取内容 → 生成故事 → 发布
#
# 子类只需实现平台专属的方法（选题、提取、发布），
# 故事生成（API/Web 分发）和批量编排在基类中通用实现。
# ============================================================

import time
import os
import logging
from datetime import datetime

log = logging.getLogger(__name__)



# —— P0 拆分：单篇生成/批量调度两块行为移入 Mixin；本文件保留
# 抽象步骤接口、素材筛选、单条流程 run_single 与批量总编排 run_batch。
from .workflow_batch import BatchGenerationMixin
from .workflow_generation import GenerationMixin

class WorkflowBase(GenerationMixin, BatchGenerationMixin):
    """
    工作流基类。

    子类需实现：
        select_topic()         → 返回 URL
        extract_content()      → 返回 (title, content, footer, url)
                                 footer: 读者互动数据 + 发表时间的 dict，
                                 采集失败时为 None（不影响 title/content）
        publish(story, title, url, md_path)
        collect_materials_batch(target)  → 返回 [{title, answer, url, index, footer}, ...]

    基类提供：
        generate_story()       → API/Web 模式分发（采样模式：片段直接注入）
        run_single()           → 单次生成即发布
        run_batch(target)      → 批量：收集→生成→评分→发布
    """

    name = "base"

    # ============================================================
    # 子类必须实现
    # ============================================================

    def select_topic(self):
        """步骤1：选题，返回问题页 URL"""
        raise NotImplementedError

    def extract_content(self, fast_mode=False):
        """步骤2：提取内容，返回 (title, content, footer, url)

        footer 为读者互动数据 + 发表时间的 dict（参与元学习入池）；
        采集失败时为 None，主流程照常运行。url 为最终实际提取的问题
        页 URL（内部重选题时必须返回新 URL）。
        """
        raise NotImplementedError

    def publish(self, story, title, url, md_path=None):
        """步骤4：发布到平台。返回故事 md 文件绝对路径。"""
        raise NotImplementedError

    def collect_materials_batch(self, target):
        """批量收集素材，返回 [{title, answer, url, index}, ...]"""
        raise NotImplementedError

    def _ai_screen_questions(self, materials, target):
        """大模型问题池筛选：排除不适合写故事/小说的候选，取最适合的。

        在硬性规则收集之后、生成之前调用（API 模式走服务商 API，
        Web 模式走网页版大模型）。LLM 失败/禁用时原样返回不阻断流程；
        LLM 判定全部不适合写故事时返回空列表（由调用方中止本批）。
        """
        from config.story import QUESTION_AI_SCREEN
        if not QUESTION_AI_SCREEN or len(materials) <= 1:
            return materials
        try:
            from story_scoring import screen_question_pool
        except Exception:
            return materials
        log.info(f"\n{'─'*50}\n大模型问题池筛选（排除不适合写故事/小说的候选）\n{'─'*50}")
        screened = screen_question_pool(materials)
        if not screened:
            # 空列表 = 大模型成功判定全部 keep=false（都不适合写故事）。
            # 失败时 screen_question_pool 原样返回非空 materials，不会走到这。
            log.warning("大模型筛选判定全部候选均不适合写故事，排除全部")
            return []
        if len(screened) > target:
            screened = screened[:target]
        for i, m in enumerate(screened):
            m["index"] = i + 1
        for m in screened:
            log.info(f"  保留 #{m['index']}: {m['title'][:50]}...")
        return screened

    # ============================================================
    # 步骤3：生成故事（通用，API/Web 分发）
    # ============================================================

    def save_story_file(self, story, index=None):
        """保存故事为 .md 文件，返回绝对路径"""
        from core import paths
        output_dir = paths.data("output")
        os.makedirs(output_dir, exist_ok=True)

        if index:
            md_filename = (f"story_{index}_"
                           f"{datetime.now():%Y%m%d_%H%M%S}.md")
        else:
            md_filename = f"story_{datetime.now():%Y%m%d_%H%M%S}.md"

        md_path = os.path.join(output_dir, md_filename)
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(story)
        return os.path.abspath(md_path)

    # ============================================================
    # 单次运行（传统模式：生成即发布）
    # ============================================================

    def run_single(self, on_extracted=None, on_story=None):
        """传统模式：选题→提取→采样生成→校验→发布

        on_extracted: 可选回调 fn(title, answer, footer, url)，
        提取完成后立即调用（Web 控制台展示提取结果用）。
        on_story: 可选回调 fn(story_text, md_path)，故事生成并存盘后
        立即调用（Web 控制台展示生成故事卡用；不保证已发布——
        格式不合规被跳过时同样回调，供人工核对废稿）。
        """
        url = self.select_topic()
        # footer（读者互动数据）在单条流程中用不上（此流程不走元学习入池），
        # 但签名必须对齐 extract_content 的返回值。★ url 取 extract_content
        # 返回的最终 URL：不可回答重选题后，实际提取的题可能与首次选题
        # 不同（线上发布曾导航到被跳过的旧题而失败）
        title, answer, _footer, url = self.extract_content()
        if on_extracted:
            try:
                on_extracted(title, answer, _footer, url)
            except Exception:
                log.warning("on_extracted 回调失败", exc_info=True)

        # ★ 生成带反馈重试：无输出/过短/格式不合规都自动重试，最多
        # STORY_GENERATE_MAX_ATTEMPTS 次；重试时把上一版的失败原因
        #（字数/章节/长段/引号等）注入 prompt 指导模型修正。返回
        # (story, ok)：ok=True 表示合格；ok=False 时 story 为最高分版本
        #（可能非空，供人工核对）。
        story, _pass = self.generate_story_with_retry(title, answer)

        if not story:
            log.error("故事生成失败（模型无输出），本轮未完成")
            return False

        # 生成即存盘：即使多次尝试仍不合规，最高分版本也要落盘供人工核对
        # （用户需要在 UI 里看到生成结果，而非静默丢弃）
        md_path = self.save_story_file(story)
        if on_story:
            try:
                on_story(story, md_path)
            except Exception:
                log.warning("on_story 回调失败", exc_info=True)

        if not _pass:
            log.warning("多次尝试均未通过格式校验（最高分版已存盘），"
                        "标记废稿，本轮未发布")
            return False

        self.publish(story, title, url, md_path)
        try:
            from core import feedback_loop
            feedback_loop.record_story_published(
                url, title, {"story_file": md_path})
        except Exception:
            log.debug("反馈闭环落账失败(不影响结果)", exc_info=True)
        log.info("本轮完成！")
        return True

    # ============================================================
    # 纯净模式（工作台 · 完整链路）
    # ============================================================

    def run_clean(self, on_extracted=None, on_story=None):
        """纯净模式：选题（有飙升选飙升、无则按关注量）→ 提取（仅赞门槛）
        → 极简生成（风格学习 + 原创禁令）→ 洗稿/抄袭审核 → 发布草稿。

        与 run_single 的差异（刻意去限制）：
        - 选题：不做故事关键词/大模型体裁筛选，只按流量信号选
        - 提取：不卡首答长度/体裁，只卡点赞门槛
        - 生成：不套格式/字数/章节/去AI味守则，只剩风格学习与原创禁令
        - 审核：对比参考高赞回答判定涉嫌抄袭/洗稿，通过才发布

        on_extracted: 可选回调 fn(title, answer, footer, url)
        on_story: 可选回调 fn(story_text, md_path, audit)
        """
        url = self.select_topic_clean()
        title, answer, _footer, url = self.extract_content_clean()
        if on_extracted:
            try:
                on_extracted(title, answer, _footer, url)
            except Exception:
                log.warning("on_extracted 回调失败", exc_info=True)

        story, audit = self.generate_clean_with_retry(title, answer)

        if not story:
            log.error("纯净模式故事生成失败（模型无输出），本轮未完成")
            return False

        # 生成即存盘：即使审核未过，最终版本也要落盘供人工核对
        md_path = self.save_story_file(story)
        if on_story:
            try:
                on_story(story, md_path, audit)
            except Exception:
                log.warning("on_story 回调失败", exc_info=True)

        if not audit or not audit.get("passed"):
            log.warning("纯净模式原创审核未通过（%s），标记待人工核对，本轮未发布",
                        (audit or {}).get("verdict", "未知"))
            return False

        self.publish(story, title, url, md_path)
        try:
            from core import feedback_loop
            feedback_loop.record_story_published(
                url, title, {"story_file": md_path})
        except Exception:
            log.debug("反馈闭环落账失败(不影响结果)", exc_info=True)
        log.info("纯净模式本轮完成！")
        return True

    def _collect_materials_single_style(self, target):
        """单轮式素材精选：循环执行单轮链路的选题+提取，收集 target 份。

        每轮走 extract_content()（与 run_single 完全同源）：
          - 热度选题 → 并行开 5 个候选 → 点赞门槛 + 过短/不可回答过滤
            → 取点赞最优 → LLM 问题池筛选挑最适合的 1 个
          - 失败自动重选题（MAX_TOPIC_RETRY 级）
        提取环节保留浏览器内 5 页并行的效率点；素材质量与单轮一致。
        连续 BATCH_COLLECT_MAX_EMPTY_ROUNDS 轮无新素材即停止。
        """
        from config.story import BATCH_COLLECT_MAX_EMPTY_ROUNDS
        materials = []
        seen_urls = set()
        seen_titles = set()
        empty_rounds = 0
        rounds = 0
        while (len(materials) < target
               and empty_rounds < BATCH_COLLECT_MAX_EMPTY_ROUNDS):
            rounds += 1
            try:
                title, answer, footer, url = self.extract_content()
            except Exception as exc:
                empty_rounds += 1
                log.warning(f"  第 {rounds} 轮精选未获素材：{exc}")
                continue
            if not (title and answer and url):
                empty_rounds += 1
                continue
            if url in seen_urls or title in seen_titles:
                log.warning("  第 %d 轮命中已收集题目，跳过：%s...",
                            rounds, title[:30])
                empty_rounds += 1
                continue
            seen_urls.add(url)
            seen_titles.add(title)
            materials.append({"title": title, "answer": answer,
                              "footer": footer or {}, "url": url})
            empty_rounds = 0
            log.info("  已精选 %d/%d 份：%s...",
                     len(materials), target, title[:40])
        if len(materials) < target:
            log.warning("  单轮式精选结束：%d/%d（连续 %d 轮无新素材）",
                        len(materials), target, empty_rounds)
        return materials

    # ============================================================
    # 批量运行（流水线模式）
    # ============================================================

    def run_batch(self, target, publish_count=None):
        """
        流水线批量模式：

        阶段1：收集素材
        阶段2：生成故事（API并行 / Web串行或并行）
        阶段2.5：格式检测 + 重试
        阶段3：评分 → 择优发布

        参数：
            target:        生成故事数量
            publish_count: 发布数量，None 则使用 config 默认值
                - publish_count < target  → 评分择优发布前 N 篇
                - publish_count >= target → 全部发布，跳过评分
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from config import (
            LLM_MODE,
            WAIT_BETWEEN_CYCLES,
        )
        from config.story import DEFAULT_BATCH_PUBLISH_COUNT
        from config import random_delay
        from story_generation import generate_story_parallel
        from story_scoring import score_stories
        from core.story_text import (
            validate_story_format, clean_story_output, fix_story_format
        )
        from desktop_utils import (
            take_screenshot, print_progress, reset_progress
        )

        if publish_count is None:
            publish_count = DEFAULT_BATCH_PUBLISH_COUNT

        need_scoring = publish_count < target

        # 生成通道的实际并行标签（Web 模式 parallel_tabs>1 且任务>1 时并行）
        from config import WEB_DRIVER_NAME, WEB_DRIVERS
        _drv_cfg = WEB_DRIVERS.get(WEB_DRIVER_NAME, {})
        gen_parallel = (LLM_MODE == "api"
                        or (_drv_cfg.get("parallel_tabs", 1) > 1 and target > 1))
        gen_mode_label = "并行" if gen_parallel else "串行"

        log.info(f"\n{'='*60}")
        log.info("流水线批量模式")
        if need_scoring:
            log.info(f"  目标：收集 {target} 份素材 → "
                     f"{gen_mode_label}生成 → "
                     f"评分 → 发布前 {publish_count} 篇")
        else:
            log.info(f"  目标：收集 {target} 份素材 → "
                     f"{gen_mode_label}生成 → "
                     f"全部发布（{target} 篇，不评分）")
        log.info(f"{'='*60}")

        time_total_start = time.time()

        # ===== 阶段1：批量收集素材 =====
        time_phase1_start = time.time()
        log.info(f"\n{'─'*50}")
        log.info(f"阶段1：批量素材收集（目标 {target} 份）")
        log.info(f"{'─'*50}")

        from config.story import BATCH_QUALITY_FIRST
        if BATCH_QUALITY_FIRST:
            # 质量优先：素材 = 逐轮「选题→并行 5 候选取最优→LLM 筛选→提取」，
            # 与单轮完整链路完全同源（不再整页滚动取前 N 凑数）
            log.info(f"  单轮式素材精选（与单轮链路同质，目标 {target} 份）")
            materials = self._collect_materials_single_style(target)
        else:
            materials = self.collect_materials_batch(target)

        if not materials:
            log.error("没有收集到任何素材！")
            return 0

        # 大模型问题池筛选：先排除不适合写故事/小说的，再挑最适合的
        materials = self._ai_screen_questions(materials, target)

        for i, m in enumerate(materials):
            m['index'] = i + 1

        if len(materials) < target:
            pct = len(materials) / target * 100
            log.warning(f"\n素材收集未达目标：{len(materials)}/{target}（{pct:.0f}%）")
            if pct < 30:
                log.warning("  ⚠ 收集量严重不足！可能原因：")
                log.warning("    1. 知乎推荐页已滚到底（无新问题可采）")
                log.warning("    2. 规则筛选过于严格（检查 STORY_INCLUDE/EXCLUDE_KEYWORDS）")
                log.warning("    3. BATCH_QUESTIONS_PER_PAGE 太小，采集太慢")
        else:
            log.info(f"\n素材收集完成：{len(materials)}/{target}")
        for m in materials:
            log.info(f"  {m['index']}. {m['title'][:50]}...")

        time_phase1 = time.time() - time_phase1_start
        log.info(f"  阶段1耗时：{time_phase1:.1f}s")

        # 采样模式：参考文章片段直接注入（配方层已退役，
        # generate_story(recipe=...) 参数保留为未来反馈闭环接口）
        for m in materials:
            m.setdefault("recipe", None)

        # ===== 阶段2：生成故事 =====
        log.info(f"\n{'─'*50}")
        if LLM_MODE == "api":
            log.info(f"阶段2：并行生成 {len(materials)} 篇故事")
        else:
            log.info(f"阶段2：批量生成 {len(materials)} 篇故事（Web 模式）")
        log.info(f"{'─'*50}\n")

        start_all = time.time()

        from config.story import BATCH_QUALITY_FIRST
        if LLM_MODE == "api":
            if BATCH_QUALITY_FIRST:
                self._batch_generate_api_quality(materials, print_progress,
                                                 reset_progress)
            else:
                self._batch_generate_api(materials, print_progress,
                                         reset_progress)
        else:
            if BATCH_QUALITY_FIRST:
                self._batch_generate_web_quality(materials)
            else:
                self._batch_generate_web(materials)

        total_gen_time = time.time() - start_all
        generated = [m for m in materials if m.get('story')]

        log.info(f"\n生成完成！")
        log.info(f"  耗时 {total_gen_time:.1f}s | "
                 f"成功 {len(generated)}/{len(materials)} 篇")

        if not generated:
            log.error("没有成功生成任何故事！")
            return 0

        # 段落分布分析
        from config.story import ENABLE_PARAGRAPH_ANALYSIS
        if ENABLE_PARAGRAPH_ANALYSIS:
            try:
                from tools.story_plots import plot_paragraph_distribution
                plot_paragraph_distribution(generated)
            except Exception as e:
                log.warning(f"  段落分布分析出错（{e}），跳过")
        else:
            log.info("  段落分布分析已关闭（ENABLE_PARAGRAPH_ANALYSIS=False）")

        # ===== 阶段2.5：格式合规检测 + 重试 =====
        time_phase25_start = time.time()
        log.info(f"\n{'─'*50}")
        log.info(f"阶段2.5：格式合规检测（{len(generated)} 篇）")
        log.info(f"{'─'*50}")

        compliant = []
        non_compliant = []
        for m in generated:
            fmt_score, is_valid, _ = validate_story_format(m['story'])
            m['format_score'] = fmt_score
            if is_valid:
                compliant.append(m)
            else:
                non_compliant.append(m)

        log.info(f"\n  合规 {len(compliant)} 篇，"
                 f"不合规 {len(non_compliant)} 篇")

        retried_ok = 0
        if non_compliant:
            from config.story import ENABLE_FORMAT_RETRY, BATCH_QUALITY_FIRST
            if BATCH_QUALITY_FIRST:
                log.warning(f"  质量优先：{len(non_compliant)} 篇经带反馈重试仍不合格，标记废稿（不发布）")
                for m in non_compliant:
                    m["story"] = None
            elif not ENABLE_FORMAT_RETRY:
                log.info(f"  ENABLE_FORMAT_RETRY=False，"
                         f"跳过 {len(non_compliant)} 篇不合规文章的重试")
            else:
                log.info(f"\n  重试 {len(non_compliant)} 篇不合规文章...")
                if LLM_MODE == "api":
                    retried_ok = self._batch_retry_api(
                        non_compliant, compliant,
                        print_progress, reset_progress
                    )
                else:
                    retried_ok = self._batch_retry_web(
                        non_compliant, compliant
                    )

        time_phase25 = time.time() - time_phase25_start
        total_gen_before_filter = len(generated)
        generated = compliant

        log.info(f"\n  阶段2.5 完成："
                 f"{len(generated)}/{total_gen_before_filter} 篇合规"
                 f"（重试挽回 {retried_ok} 篇）  "
                 f"耗时 {time_phase25:.1f}s")

        if not generated:
            log.error("所有故事均不合规，无法继续！")
            return 0

        # ===== 阶段3：评分 + 发布 =====
        time_phase3_start = time.time()
        log.info(f"\n{'─'*50}")

        # 判断是否需要评分：publish_count < 合规故事数 才需要择优
        actual_publish = min(publish_count, len(generated))
        if actual_publish >= len(generated):
            # 全部发布，跳过评分
            log.info(f"阶段3：全部发布（{len(generated)} 篇 ≤ "
                     f"目标 {publish_count} 篇，跳过评分）")
            log.info(f"{'─'*50}\n")
            to_publish = list(generated)
            to_skip = []
        else:
            # 评分择优
            log.info(f"阶段3：质量评分 + 择优发布前 {actual_publish} 篇")
            log.info(f"{'─'*50}\n")
            scored = score_stories(generated) or []
            scored = self._apply_prior_to_scores(scored)
            if scored and not any("score" in it for it in scored):
                if LLM_MODE == "web":
                    log.warning("⚠ 评分未生效（DeepSeek 网页版不可用），"
                                "将按生成顺序择优发布；请确认网页版大模型已登录")
                else:
                    log.warning("⚠ 评分未生效（评分服务商 API Key 无效或请求失败），"
                                "将按生成顺序择优发布；请在设置→生成通道检查 API Key")
            to_publish = scored[:actual_publish]
            to_skip = scored[actual_publish:]

        log.info(f"\n  将发布（{len(to_publish)} 篇）：")
        for rank, item in enumerate(to_publish):
            detail = item.get('score_detail', {})
            detail_str = (' | '.join(f"{k}={v}" for k, v in detail.items())
                          if detail else '')
            flavor = None
            try:
                from tools.ai_flavor_check import check_text
                got = check_text(item.get("story") or "")
                flavor = got[1] if got else None
            except Exception:
                flavor = None
            if flavor is not None:
                detail_str = (detail_str + f" | AI味={flavor}" if detail_str
                              else f"AI味={flavor}")
            score_str = (f"[{item.get('score', '?')}分] "
                         if 'score' in item else '')
            log.info(f"    第{rank+1}名 {score_str}"
                     f"{item['title'][:35]}...")
            if detail_str:
                log.info(f"      {detail_str}")

        if to_skip:
            log.info("  候补（优先级较低，发布失败时补位）：")
            for item in to_skip:
                log.info(f"    [{item.get('score', '?')}分] "
                         f"{item['title'][:35]}...")

        # 串行发布；如主队列失败，则用候补文章补位，尽量发布满目标数量。
        published = 0
        attempted = 0
        target_publish = len(to_publish)
        publish_queue = list(to_publish)
        backup_queue = list(to_skip)
        max_attempts = len(publish_queue) + len(backup_queue)

        while publish_queue and published < target_publish:
            item = publish_queue.pop(0)
            attempted += 1
            log.info(f"\n发布尝试 {attempted}/{max_attempts}"
                     f"（成功 {published}/{target_publish}）...")
            try:
                self.publish(item['story'], item['title'], item['url'],
                             md_path=item.get('md_path'))
                published += 1
                try:
                    from core import feedback_loop
                    feedback_loop.record_story_published(
                        item.get('url'), item.get('title'),
                        {"story_file": item.get('md_path')})
                except Exception:
                    pass
                log.info("  ✓ 发布成功")
            except KeyboardInterrupt:
                log.info("\n用户中断发布。")
                break
            except Exception as e:
                log.error(f"  发布失败：{e}")
                take_screenshot("error")
                if backup_queue:
                    replacement = backup_queue.pop(0)
                    publish_queue.append(replacement)
                    log.info("  → 启用候补补位："
                             f"{replacement['title'][:35]}...")

            if publish_queue and published < target_publish:
                random_delay(WAIT_BETWEEN_CYCLES)

        if published < target_publish:
            log.warning(f"  本轮未发布满：{published}/{target_publish}，"
                        "候补文章已用尽或连续失败")

        time_phase3 = time.time() - time_phase3_start

        time_total = time.time() - time_total_start

        log.info(f"\n{'='*60}")
        log.info("流水线批量模式完成！")
        log.info(f"{'─'*60}")
        log.info(f"  阶段1 素材收集：{len(materials)} 份    "
                 f"耗时 {time_phase1:.1f}s")
        log.info(f"  阶段2 {gen_mode_label}生成："
                 f"{total_gen_before_filter} 篇    "
                 f"耗时 {total_gen_time:.1f}s")
        log.info(f"  阶段2.5 格式检测：{len(generated)} 篇合规"
                 f"（重试挽回 {retried_ok} 篇）  "
                 f"耗时 {time_phase25:.1f}s")
        log.info(f"  阶段3 评分发布：{published} 篇      "
                 f"耗时 {time_phase3:.1f}s")
        log.info(f"{'─'*60}")
        log.info(f"  总耗时：{time_total:.1f}s（{time_total/60:.1f}分钟）")
        log.info(f"{'='*60}")
        return published
