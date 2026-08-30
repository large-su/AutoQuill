# ============================================================
# workflows/BatchGenerationMixin — 批量生成与批量重试调度（并行/串行双通道）
# P0 拆分自 WorkflowBase；方法体逐字搬运未改动。
# 行为守护：tests/test_zhihu_workflow 的源码锚点断言 + 全量回归。
# ============================================================

import os
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)


class BatchGenerationMixin:
    # ============================================================
    # 质量优先批量生成（v4.7.2 回顾：旧批量曾为效率牺牲质量；
    # 本组方法与单轮链路同质：每篇走带失败原因反馈的重试循环）
    # ============================================================

    def _run_one_story_quality(self, mat):
        """单篇生成：与单轮完整链路完全一致的口径。

        内部走 generate_story_with_retry（最多 STORY_GENERATE_MAX_ATTEMPTS 次，
        每轮注入上一版失败原因；无输出/过短/格式不合规都重试；
        返回 (最高分版本, ok)）。Web 模式同样带前端改版自动降级 API。
        """
        from core.story_text import validate_story_format
        story, ok = self.generate_story_with_retry(
            mat['title'], mat['answer'])
        if not story:
            mat['story'] = None
            mat['ok'] = False
            return False
        mat['story'] = story
        mat['ok'] = ok
        mat['md_path'] = self.save_story_file(story, mat['index'])
        fmt_score, is_valid, _ = validate_story_format(story)
        mat['format_score'] = fmt_score
        return True

    def _batch_generate_api_quality(self, materials, print_progress_fn,
                                    reset_progress_fn):
        """API 并行生成（质量优先版）：并发外壳 + 每篇单轮口径的反馈重试。

        并发提交 generate_story_with_retry，保留不伤质量的效率点
        （API 多线程并行；每篇重试 2-3 次的成本互不阻塞）。
        """
        from concurrent.futures import (ThreadPoolExecutor, wait,
                                 FIRST_COMPLETED)
        from config.story import (
            STORY_GENERATE_CONCURRENCY, STORY_GENERATE_CONCURRENCY_MAX,
            STORY_GENERATE_CONCURRENCY_MIN,
        )

        max_workers = min(len(materials), max(1, STORY_GENERATE_CONCURRENCY_MAX))
        min_workers = min(max_workers, max(1, STORY_GENERATE_CONCURRENCY_MIN))
        current_limit = min(max_workers,
                            max(min_workers, min(len(materials),
                                                STORY_GENERATE_CONCURRENCY)))
        log.info(f"  质量优先·API 并行生成：并发 {current_limit}（每篇带反馈重试）")

        progress = {}
        reset_progress_fn()
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_mat = {}
            next_index = 0

            def _submit_until_capacity():
                nonlocal next_index
                while (next_index < len(materials)
                       and len(future_to_mat) < current_limit):
                    mat = materials[next_index]
                    next_index += 1
                    future_to_mat[pool.submit(
                        self._run_one_story_quality, mat)] = mat

            _submit_until_capacity()
            while future_to_mat:
                done, _ = wait(future_to_mat, timeout=2,
                               return_when=FIRST_COMPLETED)
                print_progress_fn(progress, len(materials))
                if not done:
                    continue
                for future in done:
                    mat = future_to_mat.pop(future)
                    try:
                        ok = future.result()
                    except Exception as e:
                        ok = False
                        mat['story'] = None
                        mat['ok'] = False
                        log.error(f"  #{mat['index']} 生成异常：{e}")
                    if ok:
                        log.info(f"  ✓ #{mat['index']} 完成："
                                 f"{mat['title'][:24]}...")
                    else:
                        log.warning(f"  ✗ #{mat['index']} 多次重试仍不合格"
                                    f"（{mat['title'][:24]}...）")
                _submit_until_capacity()
        reset_progress_fn()
        print()

    def _batch_generate_web_quality(self, materials):
        """Web 串行生成（质量优先版）：与单轮链路一字不差。

        每篇走 generate_story_with_retry（含 Web 故障自动降级 API）。
        刻意不用旧的多标签并行：反馈重试需要多次往返同一会话窗口，
        串行优先保证质量（用户口径：质量第一，效率第二）。
        """
        for mat in materials:
            log.info(f"  质量优先·Web 串行生成 #{mat['index']}："
                     f"{mat['title'][:40]}...")
            self._run_one_story_quality(mat)

    @staticmethod
    def _apply_prior_to_scores(scored):
        """批量择优排序叠加账号题材先验（发布 top N 向口碑题材倾斜）。

        乘数来自 feedback_loop.topic_genre_multiplier（题材先验/全局中位），
        无数据/未知题材恒为 1.0；失败不阻断评分流程。
        返回新排序后的列表。
        """
        from config.story import TOPIC_PRIOR_IN_SCORE
        if not scored or not TOPIC_PRIOR_IN_SCORE:
            return scored
        try:
            from core.feedback_loop import topic_genre_multiplier
        except Exception:
            return scored
        for it in scored:
            try:
                boost = topic_genre_multiplier(it.get('title') or '')
            except Exception:
                boost = 1.0
            it['prior_boost'] = round(boost, 3)
            it['score_weighted'] = round(
                (it.get('score') or 0) * boost, 2)
        scored.sort(key=lambda it: it.get('score_weighted',
                                       it.get('score') or 0),
                    reverse=True)
        return scored

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
        from story_generation import generate_story_parallel

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
        from story_generation import generate_story_parallel
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
