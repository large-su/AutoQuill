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


class WorkflowBase:
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

    # ============================================================
    # 步骤3：生成故事（通用，API/Web 分发）
    # ============================================================

    def generate_story(self, question_title, top_answer, recipe=None):
        """根据 LLM_MODE 分发到 API 或 Web 模式生成故事"""
        from config import LLM_MODE

        log.info("=" * 50)
        log.info(f"步骤 3：生成故事（模式：{LLM_MODE}）")
        if recipe:
            log.info(f"  配方模式：[{recipe.get('genre', '?')}] "
                     f"{recipe.get('hook', '?')[:25]}")
        # 如果已加载了元知识，提示一下
        log.info("=" * 50)

        if LLM_MODE == "api":
            return self._generate_api(question_title, top_answer, recipe)
        else:
            return self._generate_web(question_title, top_answer, recipe)

    def _generate_api(self, question_title, top_answer, recipe=None):
        """API 模式：流式 HTTP 请求"""
        from llm_api import generate_story

        author = getattr(self, "author", None)
        story = generate_story(question_title, top_answer, recipe=recipe,
                               author=author)
        if not story:
            log.error("API 生成失败")
            from desktop_utils import focus_edge
            fallback = input("切换到网页模式重试？(y/n) >> ").strip().lower()
            if fallback == 'y':
                focus_edge()
                return self._generate_web(
                    question_title, top_answer, recipe
                )
            return None
        return story

    def _generate_web(self, question_title, top_answer, recipe=None):
        """Web 模式：通过 Web Driver 操控 LLM 网站（单轮生成）"""
        return self._generate_web_short_form(
            question_title, top_answer, recipe
        )

    def _generate_web_short_form(self, question_title, top_answer, recipe=None):
        """Web 短文模式：单轮 prompt 直接出正文"""
        from web_drivers import get_driver
        from llm_api import build_story_prompt, _load_author_profile_or_none

        author = getattr(self, "author", None)
        full_prompt, mode_str = build_story_prompt(
            question_title, top_answer, recipe,
            author_profile=_load_author_profile_or_none(author),
        )
        log.info(f"  Prompt 模式：{mode_str}")

        driver = get_driver()
        return driver.generate(full_prompt)

    # ============================================================
    # 保存故事文件（通用）
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
        from config.story import MIN_ANSWER_LENGTH
        from config import LLM_MODE
        from core.story_text import (
            validate_story_format,
            clean_story_output,
            fix_story_format,
        )

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

        # 采样模式：参考文章片段直接注入（零 LLM 提炼），不再走配方
        story = self.generate_story(title, answer)

        if story and LLM_MODE == "web":
            story = fix_story_format(clean_story_output(story))

        if not story or len(story) < 500:
            log.error(f"故事过短或生成失败"
                      f"（{len(story or '')}字符），跳过")
            return False

        # 生成即存盘：即使格式不合规被跳过，废稿也要落盘供人工核对
        # （用户需要在 UI 里看到生成结果，而非静默丢弃）
        md_path = self.save_story_file(story)
        if on_story:
            try:
                on_story(story, md_path)
            except Exception:
                log.warning("on_story 回调失败", exc_info=True)

        # 格式合规检测
        fmt_score, is_valid, fmt_details = validate_story_format(story)

        if not is_valid:
            # ★ 检查是否启用格式重试
            from config.story import ENABLE_FORMAT_RETRY

            if not ENABLE_FORMAT_RETRY:
                log.warning(f"格式不合规（{fmt_score}/10），"
                            f"ENABLE_FORMAT_RETRY=False，跳过重试，标记废稿")
                return False

            log.warning(f"格式不合规（{fmt_score}/10），重试一次...")
            retry_story = self.generate_story(title, answer)
            
            if retry_story and LLM_MODE == "web":
                retry_story = fix_story_format(clean_story_output(retry_story))

            if retry_story and len(retry_story) >= 500:
                retry_fmt, retry_valid, _ = validate_story_format(retry_story)
                if retry_fmt > fmt_score:
                    story = retry_story
                    fmt_score = retry_fmt
                    is_valid = retry_valid
                    log.info(f"重试版本更优（{fmt_score}/10）"
                             f"{'✓合规' if is_valid else '✗仍不合规'}")
                else:
                    log.info(f"重试版本未改善，使用原版（{fmt_score}/10）")

            if not is_valid:
                log.warning(f"两次生成均不合规（{fmt_score}/10），"
                            f"标记废稿，跳过")
                return False

        self.publish(story, title, url, md_path)
        log.info("本轮完成！")
        return True

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
        from config.story import (
            DEFAULT_BATCH_PUBLISH_COUNT,
            KB_ENABLE,
        )
        from config import random_delay
        from llm_api import generate_story_parallel, score_stories
        from core.story_text import (
            validate_story_format, clean_story_output, fix_story_format
        )
        from desktop_utils import (
            take_screenshot, print_progress, reset_progress
        )

        if publish_count is None:
            publish_count = DEFAULT_BATCH_PUBLISH_COUNT

        need_scoring = publish_count < target

        log.info(f"\n{'='*60}")
        log.info("流水线批量模式")
        if need_scoring:
            log.info(f"  目标：收集 {target} 份素材 → "
                     f"{'并行' if LLM_MODE == 'api' else '串行'}生成 → "
                     f"评分 → 发布前 {publish_count} 篇")
        else:
            log.info(f"  目标：收集 {target} 份素材 → "
                     f"{'并行' if LLM_MODE == 'api' else '串行'}生成 → "
                     f"全部发布（{target} 篇，不评分）")
        log.info(f"{'='*60}")

        time_total_start = time.time()

        # ===== 阶段1：批量收集素材 =====
        time_phase1_start = time.time()
        log.info(f"\n{'─'*50}")
        log.info(f"阶段1：批量素材收集（目标 {target} 份）")
        log.info(f"{'─'*50}")

        materials = self.collect_materials_batch(target)

        if not materials:
            log.error("没有收集到任何素材！")
            return 0

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

        # 采样模式：参考文章片段直接注入（零 LLM 提炼），不再绑定配方
        for m in materials:
            m["recipe"] = None

        # ===== 阶段2：生成故事 =====
        log.info(f"\n{'─'*50}")
        if LLM_MODE == "api":
            log.info(f"阶段2：并行生成 {len(materials)} 篇故事")
        else:
            log.info(f"阶段2：批量生成 {len(materials)} 篇故事（Web 模式）")
        log.info(f"{'─'*50}\n")

        start_all = time.time()

        if LLM_MODE == "api":
            self._batch_generate_api(materials, print_progress,
                                     reset_progress)
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
                from core.story_text import plot_paragraph_distribution
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
            # ★ 检查是否启用格式重试
            from config.story import ENABLE_FORMAT_RETRY

            if not ENABLE_FORMAT_RETRY:
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
            scored = score_stories(generated)
            to_publish = scored[:actual_publish]
            to_skip = scored[actual_publish:]

        log.info(f"\n  将发布（{len(to_publish)} 篇）：")
        for rank, item in enumerate(to_publish):
            detail = item.get('score_detail', {})
            detail_str = (' | '.join(f"{k}={v}" for k, v in detail.items())
                          if detail else '')
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
        if KB_ENABLE and any(m.get('recipe') for m in materials):
            log.info("  阶段1.5 配方提炼：现提现用")
        log.info(f"  阶段2 "
                 f"{'并行' if LLM_MODE == 'api' else '串行'}生成："
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

    # ============================================================
    # 批量生成内部方法
    # ============================================================

    @staticmethod
    def _get_gen_concurrency():
        """读取故事并行生成的并发数配置。"""
        from config.story import STORY_GENERATE_CONCURRENCY
        return STORY_GENERATE_CONCURRENCY

    def _batch_generate_api(self, materials, print_progress_fn,
                            reset_progress_fn):
        """API 并行生成"""
        from concurrent.futures import (
            ThreadPoolExecutor, wait, FIRST_COMPLETED
        )
        from llm_api import generate_story_parallel

        base_workers = min(len(materials), self._get_gen_concurrency())
        from config.story import (
            STORY_GENERATE_CONCURRENCY_AUTO,
            STORY_GENERATE_CONCURRENCY_MIN,
            STORY_GENERATE_CONCURRENCY_MAX,
        )

        if STORY_GENERATE_CONCURRENCY_AUTO:
            max_workers = min(
                len(materials),
                max(1, STORY_GENERATE_CONCURRENCY_MAX)
            )
            min_workers = min(
                max_workers,
                max(1, STORY_GENERATE_CONCURRENCY_MIN)
            )
            current_limit = min(max_workers, max(min_workers, base_workers))
            log.info(f"  并发数：自适应 {current_limit}"
                     f"（范围 {min_workers}-{max_workers}）")
        else:
            current_limit = base_workers
            max_workers = base_workers
            min_workers = base_workers
            log.info(f"  并发数：{max_workers}")

        progress = {}
        reset_progress_fn()

        # ★ 尝试启用 Rich 美化进度面板
        try:
            from rich_progress import create_rich_progress
            _rich_render, _rich_teardown, _rich_panel = create_rich_progress(
                len(materials)
            )
            if _rich_panel is not None:
                print_progress_fn = _rich_render
                reset_progress_fn = lambda: None  # rich 不需要 reset
            else:
                _rich_teardown = lambda: None
        except Exception:
            _rich_panel = None
            _rich_teardown = lambda: None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_mat = {}

            next_index = 0
            success_streak = 0
            fail_streak = 0

            def _submit_until_capacity():
                nonlocal next_index
                while (next_index < len(materials)
                       and len(future_to_mat) < current_limit):
                    mat = materials[next_index]
                    next_index += 1
                    future = pool.submit(
                        generate_story_parallel,
                        mat['title'], mat['answer'], mat['index'],
                        progress, recipe=mat.get('recipe'),
                        author=getattr(self, "author", None),
                    )
                    future_to_mat[future] = mat

            _submit_until_capacity()

            while future_to_mat:
                done, _ = wait(
                    future_to_mat,
                    timeout=2,
                    return_when=FIRST_COMPLETED,
                )
                print_progress_fn(progress, len(materials))
                if not done:
                    continue

                for future in done:
                    mat = future_to_mat.pop(future)
                    ok = False
                    try:
                        story = future.result()
                        if story and len(story) >= 500:
                            mat['story'] = story
                            mat['md_path'] = self.save_story_file(
                                story, mat['index']
                            )
                            ok = True
                        else:
                            mat['story'] = None
                            log.warning(f"  任务 {mat['index']} 生成结果不合格")
                    except Exception as e:
                        mat['story'] = None
                        log.error(f"  任务 {mat['index']} 异常：{e}")

                    if ok:
                        success_streak += 1
                        fail_streak = 0
                    else:
                        fail_streak += 1
                        success_streak = 0

                    if STORY_GENERATE_CONCURRENCY_AUTO:
                        if fail_streak >= 2 and current_limit > min_workers:
                            current_limit -= 1
                            fail_streak = 0
                            log.warning(f"  API 并发自动降至 {current_limit}")
                        elif (success_streak >= max(4, current_limit * 2)
                              and current_limit < max_workers):
                            current_limit += 1
                            success_streak = 0
                            log.info(f"  API 并发自动升至 {current_limit}")

                _submit_until_capacity()

        print_progress_fn(progress, len(materials))
        reset_progress_fn()
        _rich_teardown()  # ★ 关闭 Rich 面板
        print()

    def _batch_generate_web(self, materials):
        """Web 生成入口：parallel_tabs > 1 且任务数 > 1 → 并行，否则串行。"""
        from config import WEB_DRIVER_NAME, WEB_DRIVERS
        drv_cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        if drv_cfg.get("parallel_tabs", 1) > 1 and len(materials) > 1:
            self._batch_generate_web_parallel(materials, drv_cfg)
        else:
            self._batch_generate_web_serial(materials)

    def _batch_generate_web_parallel(self, materials, drv_cfg):
        """Web 并行生成（DOM 版：共享 context 的 N 个独立页面）。

        每 slot 一个独立 driver 实例（create_driver），单线程主循环
        轮询派发/收集（web_drivers/parallel.py），与 API 并行
        ThreadPoolExecutor 不同：页面操作必须单线程。
        """
        from llm_api import build_story_prompt, _load_author_profile_or_none
        from core.story_text import clean_story_output, fix_story_format
        from web_drivers.parallel import ParallelWebRunner

        num_slots = min(drv_cfg.get("parallel_tabs", 2), len(materials))
        threshold = drv_cfg.get("consecutive_fail_threshold", 2)
        scan_interval = drv_cfg.get("scan_interval", 2)

        log.info(f"  启用并行模式：{num_slots} 个页面 "
                 f"（总任务 {len(materials)} 个）")

        # 构造 prompt 任务列表（与串行 _generate_web_short_form
        # 一致地注入作者文风——旧版并行漏传，此处对齐）
        author = getattr(self, "author", None)
        tasks = []
        for mat in materials:
            full_prompt, _mode = build_story_prompt(
                mat['title'], mat['answer'], recipe=mat.get('recipe'),
                author_profile=_load_author_profile_or_none(author),
            )
            tasks.append((full_prompt, mat))

        runner = ParallelWebRunner(
            num_slots=num_slots,
            threshold=threshold,
            scan_interval=scan_interval,
        )
        results = []
        try:
            runner.setup()
            results = runner.run(tasks)
        except Exception as e:
            log.error(f"并行运行器异常：{e}")
        finally:
            try:
                runner.teardown()
            except Exception as e:
                log.warning(f"teardown 异常：{e}")

        # 映射结果回 materials（与串行同款清洗与保存）
        for i, mat in enumerate(materials):
            story = results[i] if i < len(results) else None
            if story and len(story) >= 500:
                story = fix_story_format(clean_story_output(story))
                mat['story'] = story
                mat['md_path'] = self.save_story_file(story, mat['index'])
                log.info(f"  ✓ 任务 {mat['index']} 并行生成成功"
                         f"（{len(story)} 字符）")
            else:
                mat['story'] = None
                log.warning(f"  ✗ 任务 {mat['index']} 并行生成失败")

        from web_drivers import reset_driver
        reset_driver()  # 兜底关单例页（并行本身不用单例）

    def _batch_generate_web_serial(self, materials):
        """Web 串行生成（单 tab 复用同一会话）"""
        from core.story_text import clean_story_output, fix_story_format

        for i, mat in enumerate(materials):
            log.info(f"\n  Web 串行生成 {i+1}/{len(materials)}："
                     f"{mat['title'][:40]}...")
            try:
                story = self._generate_web(
                    mat['title'], mat['answer'],
                    recipe=mat.get('recipe')
                )
                if story and len(story) >= 500:
                    story = fix_story_format(clean_story_output(story))
                    mat['story'] = story
                    mat['md_path'] = self.save_story_file(
                        story, mat['index']
                    )
                    log.info(f"    ✓ 生成成功（{len(story)} 字符）")
                else:
                    mat['story'] = None
                    log.warning("    ✗ 生成失败或过短")
            except Exception as e:
                mat['story'] = None
                log.error(f"    ✗ 异常：{e}")

        from web_drivers import reset_driver
        reset_driver()

    def _batch_retry_api(self, non_compliant, compliant,
                         print_progress_fn, reset_progress_fn):
        """API 并行重试不合规文章"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from llm_api import generate_story_parallel
        from core.story_text import validate_story_format

        retried_ok = 0
        retry_progress = {}
        reset_progress_fn()

        # ★ 尝试启用 Rich 美化进度面板
        try:
            from rich_progress import create_rich_progress
            _rich_render, _rich_teardown, _rich_panel = create_rich_progress(
                len(non_compliant)
            )
            if _rich_panel is not None:
                print_progress_fn = _rich_render
                reset_progress_fn = lambda: None
            else:
                _rich_teardown = lambda: None
        except Exception:
            _rich_panel = None
            _rich_teardown = lambda: None

        with ThreadPoolExecutor(
            max_workers=min(len(non_compliant), 5)
        ) as pool:
            future_to_mat = {}
            for mat in non_compliant:
                future = pool.submit(
                    generate_story_parallel,
                    mat['title'], mat['answer'], mat['index'],
                    retry_progress, recipe=mat.get('recipe'),
                    author=getattr(self, "author", None),
                )
                future_to_mat[future] = mat

            all_done = False
            while not all_done:
                time.sleep(2)
                print_progress_fn(retry_progress, len(non_compliant))
                all_done = all(
                    future.done()
                    or '完成' in retry_progress.get(
                        mat['index'], {}
                    ).get('status', '')
                    or '❌' in retry_progress.get(
                        mat['index'], {}
                    ).get('status', '')
                    or '超时' in retry_progress.get(
                        mat['index'], {}
                    ).get('status', '')
                    for future, mat in future_to_mat.items()
                )

            for future in as_completed(future_to_mat):
                mat = future_to_mat[future]
                try:
                    retry_story = future.result()
                    if retry_story and len(retry_story) >= 500:
                        retry_fmt, retry_valid, _ = validate_story_format(
                            retry_story
                        )
                        if retry_valid and retry_fmt > mat['format_score']:
                            mat['story'] = retry_story
                            mat['format_score'] = retry_fmt
                            mat['md_path'] = self.save_story_file(
                                retry_story, f"{mat['index']}_retry"
                            )
                            compliant.append(mat)
                            retried_ok += 1
                            log.info(f"  ✓ 任务 {mat['index']} "
                                     f"重试合规（{retry_fmt}/10）")
                        else:
                            log.info(f"  ✗ 任务 {mat['index']} "
                                     f"重试仍不合规，标记废稿")
                except Exception as e:
                    log.error(f"  任务 {mat['index']} 重试异常：{e}")

        reset_progress_fn()
        _rich_teardown()  # ★ 关闭 Rich 面板
        print()
        return retried_ok

    def _batch_retry_web(self, non_compliant, compliant):
        """Web 重试入口：parallel_tabs > 1 且任务数 > 1 → 并行，否则串行。

        逻辑与 _batch_generate_web 类似，但多一步 format_score 比较：
        只有重试版本的 fmt_score 严格大于原版本才采用。
        """
        from config import WEB_DRIVER_NAME, WEB_DRIVERS
        drv_cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        if drv_cfg.get("parallel_tabs", 1) > 1 and len(non_compliant) > 1:
            return self._batch_retry_web_parallel(
                non_compliant, compliant, drv_cfg)
        return self._batch_retry_web_serial(non_compliant, compliant)

    def _batch_retry_web_parallel(self, non_compliant, compliant, drv_cfg):
        """Web 并行重试不合规文章（复用并行调度器，保留 fmt_score 严格大于语义）。"""
        from llm_api import build_story_prompt, _load_author_profile_or_none
        from core.story_text import (
            clean_story_output, fix_story_format, validate_story_format
        )
        from web_drivers.parallel import ParallelWebRunner

        num_slots = min(drv_cfg.get("parallel_tabs", 2), len(non_compliant))
        threshold = drv_cfg.get("consecutive_fail_threshold", 2)
        scan_interval = drv_cfg.get("scan_interval", 2)

        log.info(f"  并行重试：{num_slots} 个页面 "
                 f"（共 {len(non_compliant)} 篇不合规）")

        author = getattr(self, "author", None)
        tasks = []
        for mat in non_compliant:
            full_prompt, _mode = build_story_prompt(
                mat['title'], mat['answer'], recipe=mat.get('recipe'),
                author_profile=_load_author_profile_or_none(author),
            )
            tasks.append((full_prompt, mat))

        runner = ParallelWebRunner(
            num_slots=num_slots,
            threshold=threshold,
            scan_interval=scan_interval,
        )
        results = []
        try:
            runner.setup()
            results = runner.run(tasks)
        except Exception as e:
            log.error(f"并行重试运行器异常：{e}")
        finally:
            try:
                runner.teardown()
            except Exception as e:
                log.warning(f"teardown 异常：{e}")

        retried_ok = 0
        for i, mat in enumerate(non_compliant):
            retry_story = results[i] if i < len(results) else None
            if retry_story and len(retry_story) >= 500:
                retry_story = fix_story_format(
                    clean_story_output(retry_story)
                )
                retry_fmt, retry_valid, _ = validate_story_format(
                    retry_story
                )
                if retry_valid and retry_fmt > mat['format_score']:
                    mat['story'] = retry_story
                    mat['format_score'] = retry_fmt
                    mat['md_path'] = self.save_story_file(
                        retry_story, f"{mat['index']}_retry"
                    )
                    compliant.append(mat)
                    retried_ok += 1
                    log.info(f"  ✓ 任务 {mat['index']} 重试合规（{retry_fmt}/10）")
                else:
                    log.info(f"  ✗ 任务 {mat['index']} 重试仍不合规，标记废稿")
            else:
                log.error(f"  任务 {mat['index']} 重试失败")

        from web_drivers import reset_driver
        reset_driver()  # 兜底关单例页
        return retried_ok

    def _batch_retry_web_serial(self, non_compliant, compliant):
        """Web 串行重试不合规文章（原有逻辑）"""
        from core.story_text import (
            clean_story_output, fix_story_format, validate_story_format
        )

        retried_ok = 0
        for mat in non_compliant:
            log.info(f"  Web 重试 {mat['index']}："
                     f"{mat['title'][:40]}...")
            try:
                retry_story = self._generate_web(
                    mat['title'], mat['answer'],
                    recipe=mat.get('recipe')
                )
                if retry_story and len(retry_story) >= 500:
                    retry_story = fix_story_format(
                        clean_story_output(retry_story)
                    )
                    retry_fmt, retry_valid, _ = validate_story_format(
                        retry_story
                    )
                    if retry_valid and retry_fmt > mat['format_score']:
                        mat['story'] = retry_story
                        mat['format_score'] = retry_fmt
                        mat['md_path'] = self.save_story_file(
                            retry_story, f"{mat['index']}_retry"
                        )
                        compliant.append(mat)
                        retried_ok += 1
                        log.info(f"  ✓ 重试合规（{retry_fmt}/10）")
                    else:
                        log.info("  ✗ 重试仍不合规，标记废稿")
            except Exception as e:
                log.error(f"  重试异常：{e}")

        from web_drivers import reset_driver
        reset_driver()
        return retried_ok
