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
        extract_content(url)   → 返回 (title, content, footer)
                                 footer: 读者互动数据 + 发表时间的 dict，
                                 采集失败时为 None（不影响 title/content）
        publish(story, title, url, md_path)
        collect_materials_batch(target)  → 返回 [{title, answer, url, index, footer}, ...]

    基类提供：
        generate_story()       → API/Web 模式分发
        extract_recipe()       → 配方提炼
        run_single()           → 单次生成即发布
        run_batch(target)      → 批量：收集→生成→评分→发布
    """

    name = "base"

    # ============================================================
    # 异步配方提炼基础设施（采集阶段边采边炼，时间重叠）
    # ============================================================

    def _init_async_recipe_extraction(self):
        """初始化异步配方提炼线程池（采集循环开始前调用）。"""
        from concurrent.futures import ThreadPoolExecutor
        self._recipe_executor = ThreadPoolExecutor(max_workers=3)
        self._recipe_futures = []  # list of (material_index, future)

    def _fire_recipe_extraction(self, title, answer, material_index):
        """异步发起单篇配方提炼（非阻塞，采集循环内调用）。"""
        if not hasattr(self, '_recipe_executor') or self._recipe_executor is None:
            return
        from kb_manager import extract_single_recipe
        future = self._recipe_executor.submit(
            extract_single_recipe, title, answer  # 使用默认 timeout=90s
        )
        self._recipe_futures.append((material_index, future))

    def _collect_async_recipes(self, materials):
        """等待所有异步提炼完成，绑定配方到 materials（采集结束后调用）。"""
        if not hasattr(self, '_recipe_futures') or not self._recipe_futures:
            return 0

        from kb_manager import save_kb, load_kb

        pending = len(self._recipe_futures)
        log.info(f"\n  等待 {pending} 个异步配方提炼完成...")

        bound = 0
        new_recipes_for_kb = []

        for mat_idx, future in self._recipe_futures:
            try:
                recipe = future.result(timeout=60)
            except Exception:
                recipe = None

            if recipe and 0 <= mat_idx < len(materials):
                m = materials[mat_idx]
                recipe["source_title"] = m["title"][:80]
                recipe["_source_idx"] = mat_idx
                recipe["added_at"] = datetime.now().strftime("%Y-%m-%d")
                recipe["times_used"] = 0
                recipe["avg_score"] = None
                recipe["score_history"] = []
                recipe["score_count"] = 0

                m["recipe"] = recipe
                m["genre"] = recipe.get("genre", "其他")
                new_recipes_for_kb.append(recipe)
                bound += 1

        # 统一写入 KB（避免并发竞态）
        if new_recipes_for_kb:
            try:
                kb = load_kb()
                max_id = 0
                for r in kb.get("recipes", []):
                    rid = r.get("id", "recipe_000")
                    try:
                        num = int(rid.split("_")[1])
                        max_id = max(max_id, num)
                    except (IndexError, ValueError):
                        pass
                for recipe in new_recipes_for_kb:
                    max_id += 1
                    recipe["id"] = f"recipe_{max_id:03d}"
                kb["recipes"].extend(new_recipes_for_kb)
                kb["stats"]["sources_analyzed"] = (
                    kb["stats"].get("sources_analyzed", 0) + len(new_recipes_for_kb)
                )
                save_kb(kb)
                log.info(f"  ✓ 新增 {len(new_recipes_for_kb)} 个配方（总计 {len(kb['recipes'])} 个）")

                from config import KB_MERGE_TRIGGER
                if len(kb["recipes"]) >= KB_MERGE_TRIGGER:
                    log.info(f"  知识库条目（{len(kb['recipes'])}）已达压缩阈值（{KB_MERGE_TRIGGER}），"
                             f"建议运行：python kb_manager.py --compress")
            except Exception as e:
                log.warning(f"  KB 写入异常（配方已绑定到素材，不影响生成）：{e}")

        # 清理
        self._recipe_executor.shutdown(wait=False)
        self._recipe_futures = []
        self._recipe_executor = None

        log.info(f"  异步配方绑定成功：{bound}/{len(materials)}")
        return bound

    # ============================================================
    # 子类必须实现
    # ============================================================

    def select_topic(self):
        """步骤1：选题，返回问题页 URL"""
        raise NotImplementedError

    def extract_content(self, fast_mode=False):
        """步骤2：提取内容，返回 (title, content, footer)

        footer 为读者互动数据 + 发表时间的 dict（参与元学习入池）；
        采集失败时为 None，主流程照常运行。
        """
        raise NotImplementedError

    def publish(self, story, title, url, md_path=None):
        """步骤4：发布到平台"""
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
        if getattr(self, "_meta_knowledge", None):
            log.info(f"  已注入元知识（心法手册，{len(self._meta_knowledge)} 字符）")
        log.info("=" * 50)

        if LLM_MODE == "api":
            return self._generate_api(question_title, top_answer, recipe)
        else:
            return self._generate_web(question_title, top_answer, recipe)

    def _generate_api(self, question_title, top_answer, recipe=None):
        """API 模式：流式 HTTP 请求"""
        from llm_api import generate_story

        meta = getattr(self, "_meta_knowledge", None)
        story = generate_story(question_title, top_answer, recipe=recipe,
                               meta_knowledge=meta)
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
        """Web 模式：通过 Web Driver 操控 LLM 网站
        
        支持 LONG_FORM_MODE：
          - False → 单轮生成（短文模式，直接出正文）
          - True  → 两轮生成（长文模式：大纲 → 正文）
        """
        try:
            from config import LONG_FORM_MODE
        except ImportError:
            LONG_FORM_MODE = False

        # Web 模式不支持弧循环长文流水线（~33 次调用），
        # 当 LONG_FORM_MODE=True 时也走单轮生成，由
        # build_story_prompt 中的 STORY_RECIPE_PROMPT 承载创作意图
        return self._generate_web_short_form(
            question_title, top_answer, recipe
        )

    def _generate_web_short_form(self, question_title, top_answer, recipe=None):
        """Web 短文模式：单轮 prompt 直接出正文"""
        from web_drivers import get_driver
        from llm_api import build_story_prompt

        meta = getattr(self, "_meta_knowledge", None)
        full_prompt, mode_str = build_story_prompt(
            question_title, top_answer, recipe,
            meta_knowledge=meta,
        )
        log.info(f"  Prompt 模式：{mode_str}")

        driver = get_driver()
        return driver.generate(full_prompt)

    def extract_recipe(self, title, answer):
        """从参考文章提炼配方，返回 recipe dict 或 None"""
        from config import LLM_API_KEY
        try:
            from config import KB_ENABLE
        except ImportError:
            return None

        if not KB_ENABLE or not LLM_API_KEY:
            return None

        try:
            from kb_manager import extract_and_store
            new_recipes = extract_and_store(
                [{"title": title, "answer": answer}]
            )
            if isinstance(new_recipes, list) and new_recipes:
                recipe = new_recipes[0]
                log.info(f"  使用配方：[{recipe.get('genre', '?')}] "
                         f"{recipe.get('perspective', '')} "
                         f"{recipe.get('hook', '?')[:25]}")
                return recipe
            else:
                log.info("  未提炼出配方，使用参考文章模式")
        except ImportError:
            log.info("  知识库未安装，使用参考文章模式")
        except Exception as e:
            log.warning(f"  配方提炼出错（{e}），回退到参考文章模式")

        return None

    # ============================================================
    # 保存故事文件（通用）
    # ============================================================

    def save_story_file(self, story, index=None):
        """保存故事为 .md 文件，返回绝对路径"""
        output_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "output"
        )
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

    def run_single(self):
        """传统模式：选题→提取→配方→生成→校验→发布"""
        from config import MIN_ANSWER_LENGTH, LLM_MODE
        from llm_api import (
            validate_story_format,
            clean_story_output,
            fix_story_format,
        )

        url = self.select_topic()
        # footer（读者互动数据）在单条流程中用不上（此流程不走元学习入池），
        # 但签名必须对齐 extract_content 的三元组返回值
        title, answer, _footer = self.extract_content()

        recipe = self.extract_recipe(title, answer)
        story = self.generate_story(title, answer, recipe=recipe)

        if story and LLM_MODE == "web":
            story = fix_story_format(clean_story_output(story))
        
        if not story or len(story) < 500:
            log.error(f"故事过短或生成失败"
                      f"（{len(story or '')}字符），跳过")
            return False

        # 格式合规检测
        fmt_score, is_valid, fmt_details = validate_story_format(story)

        if not is_valid:
            # ★ 检查是否启用格式重试
            try:
                from config import ENABLE_FORMAT_RETRY
            except ImportError:
                ENABLE_FORMAT_RETRY = True

            if not ENABLE_FORMAT_RETRY:
                log.warning(f"格式不合规（{fmt_score}/10），"
                            f"ENABLE_FORMAT_RETRY=False，跳过重试，标记废稿")
                return False

            log.warning(f"格式不合规（{fmt_score}/10），重试一次...")
            retry_story = self.generate_story(title, answer, recipe=recipe)
            
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

        self.publish(story, title, url)
        log.info("本轮完成！")
        return True

    # ============================================================
    # 批量运行（流水线模式）
    # ============================================================

    def run_batch(self, target, publish_count=None, use_meta=None):
        """
        流水线批量模式：

        阶段1：收集素材
        阶段1.5：配方提炼
        阶段2：生成故事（API并行 / Web串行或并行）
        阶段2.5：格式检测 + 重试
        阶段3：评分 → 择优发布
        阶段3.5：元学习（评分回写 + 入池 + 检测蒸馏，自动）

        参数：
            target:        生成故事数量
            publish_count: 发布数量，None 则使用 config 默认值
                - publish_count < target  → 评分择优发布前 N 篇
                - publish_count >= target → 全部发布，跳过评分
            use_meta:      是否在阶段2注入元知识到生成 prompt。
                           None → 使用 config.META_INJECT_DEFAULT
                           True/False → 显式覆盖
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from config import (
            LLM_MODE, LLM_API_KEY, DEFAULT_BATCH_PUBLISH_COUNT,
            WAIT_BETWEEN_CYCLES
        )
        from config import random_delay
        from llm_api import (
            generate_story_parallel, score_stories,
            validate_story_format, clean_story_output, fix_story_format
        )
        from desktop_utils import (
            take_screenshot, print_progress, reset_progress
        )

        if publish_count is None:
            publish_count = DEFAULT_BATCH_PUBLISH_COUNT

        need_scoring = publish_count < target

        # ===== 解析 use_meta + 加载元知识 =====
        try:
            from config import META_INJECT_DEFAULT, META_LEARN_ENABLE
        except ImportError:
            META_INJECT_DEFAULT = False
            META_LEARN_ENABLE = False

        if use_meta is None:
            use_meta = META_INJECT_DEFAULT

        self._meta_knowledge = None  # 每次 run_batch 重置
        if use_meta:
            try:
                from meta_learner import load_meta_knowledge, log_pool_stats
                meta_body = load_meta_knowledge()
                if meta_body:
                    self._meta_knowledge = meta_body
                    log.info(f"✓ 已加载元知识（心法手册，{len(meta_body)} 字符），"
                             f"将注入到本批次所有生成 prompt")
                else:
                    log.info("⚠ 开启了 --use-meta，但 meta_knowledge.md 尚不存在"
                             "或为空。本批次正常生成（等达到阈值后自动建立初稿）")
                log_pool_stats()
            except Exception as e:
                log.warning(f"加载元知识异常（忽略，按无元知识运行）：{e}")

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
        if self._meta_knowledge:
            log.info(f"  ✨ 元知识已注入（use_meta=True）")
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

        # ===== 阶段1.5：配方提炼 =====
        # ★ 优先走异步流水线（采集过程中已边采边炼，此处只收集结果）
        #    若异步未启用，回退到旧的串行批处理模式
        try:
            from config import KB_ENABLE
        except ImportError:
            KB_ENABLE = False

        if KB_ENABLE and LLM_API_KEY:
            time_phase15_start = time.time()
            log.info(f"\n{'─'*50}")
            log.info("阶段1.5：配方提炼（从参考文章现提现用）")
            log.info(f"{'─'*50}")

            # ★ 优先收集异步提炼结果
            async_bound = self._collect_async_recipes(materials)

            if async_bound > 0:
                # 异步模式成功：未绑定的素材标记为无配方
                for m in materials:
                    if not m.get("recipe"):
                        m["recipe"] = None
                        log.info(f"  {m['index']}. → 未提炼出配方，"
                                 f"使用参考文章模式")
            else:
                # 异步未启用或全部失败 → 回退到串行批处理
                try:
                    from kb_manager import extract_and_store

                    articles = [{"title": m["title"], "answer": m["answer"]}
                                for m in materials]
                    new_recipes = extract_and_store(articles)

                    if isinstance(new_recipes, int):
                        log.warning("  kb_manager 返回了数量而非配方列表")
                        for m in materials:
                            m["recipe"] = None
                    else:
                        log.info(f"  提炼出 {len(new_recipes)} 个配方")
                        bound = 0
                        for recipe in new_recipes:
                            src_idx = recipe.get("_source_idx")
                            if src_idx is not None and 0 <= src_idx < len(materials):
                                m = materials[src_idx]
                                if not m.get("recipe"):
                                    m["recipe"] = recipe
                                    m["genre"] = recipe.get("genre", "其他")
                                    bound += 1
                                    log.info(
                                        f"  {m['index']}. "
                                        f"[{recipe.get('genre', '?')}] "
                                        f"{recipe.get('perspective', '')} → "
                                        f"{recipe.get('hook', '?')[:25]}"
                                    )
                            recipe.pop("_source_idx", None)

                        for m in materials:
                            if not m.get("recipe"):
                                m["recipe"] = None
                                log.info(f"  {m['index']}. → 未提炼出配方，"
                                         f"使用参考文章模式")

                        log.info(f"  绑定成功：{bound}/{len(materials)}")

                except ImportError as e:
                    log.info(f"  kb_manager 导入失败（{e}），跳过配方提炼")
                    for m in materials:
                        m["recipe"] = None
                except Exception as e:
                    log.warning(f"  配方提炼出错（{e}），跳过")
                    for m in materials:
                        m["recipe"] = None

            time_phase15 = time.time() - time_phase15_start
            log.info(f"  阶段1.5耗时：{time_phase15:.1f}s")
        else:
            for m in materials:
                m["recipe"] = None

        # ===== 阶段2：生成故事 =====
        log.info(f"\n{'─'*50}")
        if LLM_MODE == "api":
            log.info(f"阶段2：并行生成 {len(materials)} 篇故事")
        else:
            log.info(f"阶段2：串行生成 {len(materials)} 篇故事（Web 模式）")
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
        try:
            from config import ENABLE_PARAGRAPH_ANALYSIS
        except ImportError:
            ENABLE_PARAGRAPH_ANALYSIS = False
        if ENABLE_PARAGRAPH_ANALYSIS:
            try:
                from llm_api import plot_paragraph_distribution
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
            try:
                from config import ENABLE_FORMAT_RETRY
            except ImportError:
                ENABLE_FORMAT_RETRY = True

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
        _did_score = False  # ★ 追踪本轮是否实际执行了评分
        if actual_publish >= len(generated):
            # 全部发布，跳过评分
            log.info(f"阶段3：全部发布（{len(generated)} 篇 ≤ "
                     f"目标 {publish_count} 篇，跳过评分）")
            log.info(f"{'─'*50}\n")
            to_publish = list(generated)
            to_skip = []
        else:
            # 评分择优
            _did_score = True
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

        # ===== 阶段 3.5：元学习（评分回写 + 入池 + 检测蒸馏）=====
        # 仅当本轮实际执行了评分时才能学习（_did_score 标志位）
        if META_LEARN_ENABLE and _did_score:
            time_meta_start = time.time()
            log.info(f"\n{'─'*50}")
            log.info("阶段 3.5：元学习（评分回写 + 入池 + 检测蒸馏）")
            log.info(f"{'─'*50}")
            try:
                from meta_learner import (
                    enqueue_full_batch, check_and_distill, log_pool_stats
                )
                added = enqueue_full_batch(scored)
                log.info(f"  本轮入池 {added} 条配方")
                log_pool_stats()
                distilled = check_and_distill()
                if distilled:
                    log.info("  ✓ 本轮已完成心法蒸馏，新版元知识已就绪")
            except Exception as e:
                log.warning(f"  元学习过程出错（忽略，不影响主流程）：{e}")
            time_meta = time.time() - time_meta_start
            log.info(f"  阶段 3.5 耗时 {time_meta:.1f}s")

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
        try:
            from applications.zhihu_story.config import STORY_GENERATE_CONCURRENCY
            return STORY_GENERATE_CONCURRENCY
        except ImportError:
            return 5

    def _batch_generate_api(self, materials, print_progress_fn,
                            reset_progress_fn):
        """API 并行生成"""
        from concurrent.futures import (
            ThreadPoolExecutor, wait, FIRST_COMPLETED
        )
        from llm_api import generate_story_parallel

        base_workers = min(len(materials), self._get_gen_concurrency())
        try:
            from config import (
                STORY_GENERATE_CONCURRENCY_AUTO,
                STORY_GENERATE_CONCURRENCY_MIN,
                STORY_GENERATE_CONCURRENCY_MAX,
            )
        except ImportError:
            STORY_GENERATE_CONCURRENCY_AUTO = False
            STORY_GENERATE_CONCURRENCY_MIN = base_workers
            STORY_GENERATE_CONCURRENCY_MAX = base_workers

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
            meta = getattr(self, "_meta_knowledge", None)

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
                        meta_knowledge=meta,
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
        """
        Web 生成入口：根据 config 里 parallel_tabs 自动选择串行/并行。

        parallel_tabs <= 1 → 走原有串行逻辑（单 tab，单会话）
        parallel_tabs  > 1 → 走 ParallelWebRunner（独立 Edge 窗口 + 多 tab）

        ★ 长文模式（LONG_FORM_MODE=True）强制走串行：
        长文需要两轮生成（大纲→正文），ParallelWebRunner 目前只支持单步任务，
        无法在单个 slot 内串联两轮 prompt。长文走串行可保证每个故事的大纲+正文
        在同一会话中完成，上下文连贯。
        """
        from config import WEB_DRIVER_NAME, WEB_DRIVERS
        
        try:
            from config import LONG_FORM_MODE
        except ImportError:
            LONG_FORM_MODE = False

        drv_cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        parallel_tabs = drv_cfg.get("parallel_tabs", 1)

        if parallel_tabs > 1 and not LONG_FORM_MODE:
            self._batch_generate_web_parallel(materials, drv_cfg)
        else:
            if LONG_FORM_MODE and parallel_tabs > 1:
                log.info("  ⚠ 长文模式不支持并行，自动切换为串行生成")
            self._batch_generate_web_serial(materials)

    def _batch_generate_web_serial(self, materials):
        """Web 串行生成（原有逻辑，单 tab 复用同一会话）"""
        from llm_api import clean_story_output, fix_story_format

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

    def _batch_generate_web_parallel(self, materials, drv_cfg):
        """Web 并行生成（独立 Edge 窗口 + N 个 tab）"""
        from llm_api import (
            build_story_prompt, clean_story_output, fix_story_format
        )
        from web_drivers.parallel_runner import ParallelWebRunner

        # 并行 tab 数不超过任务数
        num_slots = min(drv_cfg.get("parallel_tabs", 3), len(materials))
        threshold = drv_cfg.get("consecutive_fail_threshold", 2)
        scan_interval = drv_cfg.get("scan_interval", 2)

        log.info(f"  启用并行模式：{num_slots} 个 tab "
                 f"（总任务 {len(materials)} 个）")

        # 构造 (prompt, meta) 任务列表
        meta = getattr(self, "_meta_knowledge", None)
        tasks = []
        for mat in materials:
            full_prompt, _mode = build_story_prompt(
                mat['title'], mat['answer'], recipe=mat.get('recipe'),
                meta_knowledge=meta,
            )
            tasks.append((full_prompt, mat))

        runner = ParallelWebRunner(
            num_slots=num_slots,
            threshold=threshold,
            scan_interval=scan_interval,
        )

        results = [None] * len(tasks)
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

        # 映射结果回 materials
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

    def _batch_retry_api(self, non_compliant, compliant,
                         print_progress_fn, reset_progress_fn):
        """API 并行重试不合规文章"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from llm_api import generate_story_parallel, validate_story_format

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
            meta = getattr(self, "_meta_knowledge", None)
            for mat in non_compliant:
                future = pool.submit(
                    generate_story_parallel,
                    mat['title'], mat['answer'], mat['index'],
                    retry_progress, recipe=mat.get('recipe'),
                    meta_knowledge=meta,
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
        """
        Web 重试入口：根据 config 里 parallel_tabs 自动选择串行/并行。

        逻辑与 _batch_generate_web 类似，但多一步 format_score 比较：
        只有重试版本的 fmt_score 严格大于原版本才采用。

        ★ 长文模式强制走串行：并行 runner 只支持单步 prompt，
        无法串联大纲→正文两轮流程。
        """
        from config import WEB_DRIVER_NAME, WEB_DRIVERS

        try:
            from config import LONG_FORM_MODE
        except ImportError:
            LONG_FORM_MODE = False

        drv_cfg = WEB_DRIVERS[WEB_DRIVER_NAME]
        parallel_tabs = drv_cfg.get("parallel_tabs", 1)

        if parallel_tabs > 1 and not LONG_FORM_MODE:
            return self._batch_retry_web_parallel(
                non_compliant, compliant, drv_cfg
            )
        else:
            if LONG_FORM_MODE and parallel_tabs > 1:
                log.info("  ⚠ 长文模式不支持并行重试，自动切换为串行")
            return self._batch_retry_web_serial(non_compliant, compliant)

    def _batch_retry_web_serial(self, non_compliant, compliant):
        """Web 串行重试不合规文章（原有逻辑）"""
        from llm_api import (
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

    def _batch_retry_web_parallel(self, non_compliant, compliant, drv_cfg):
        """Web 并行重试不合规文章"""
        from llm_api import (
            build_story_prompt, clean_story_output,
            fix_story_format, validate_story_format
        )
        from web_drivers.parallel_runner import ParallelWebRunner

        if not non_compliant:
            return 0

        num_slots = min(
            drv_cfg.get("parallel_tabs", 3), len(non_compliant)
        )
        threshold = drv_cfg.get("consecutive_fail_threshold", 2)
        scan_interval = drv_cfg.get("scan_interval", 2)

        log.info(f"  启用并行重试：{num_slots} 个 tab "
                 f"（重试任务 {len(non_compliant)} 个）")

        meta = getattr(self, "_meta_knowledge", None)
        tasks = []
        for mat in non_compliant:
            full_prompt, _mode = build_story_prompt(
                mat['title'], mat['answer'], recipe=mat.get('recipe'),
                meta_knowledge=meta,
            )
            tasks.append((full_prompt, mat))

        runner = ParallelWebRunner(
            num_slots=num_slots,
            threshold=threshold,
            scan_interval=scan_interval,
        )

        results = [None] * len(tasks)
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
                    log.info(f"  ✓ 任务 {mat['index']} "
                             f"并行重试合规（{retry_fmt}/10）")
                else:
                    log.info(f"  ✗ 任务 {mat['index']} "
                             f"并行重试仍不合规")
            else:
                log.info(f"  ✗ 任务 {mat['index']} 并行重试失败")

        return retried_ok
