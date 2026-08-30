# -*- coding: utf-8 -*-
import pathlib
p = pathlib.Path('workflows/base.py')
src = p.read_text(encoding='utf-8')

# 1) 阶段1：质量优先用单轮式精选
old = '''        materials = self.collect_materials_batch(target)'''
new = '''        from config.story import BATCH_QUALITY_FIRST
        if BATCH_QUALITY_FIRST:
            # 质量优先：素材 = 逐轮「选题→并行 5 候选取最优→LLM 筛选→提取」，
            # 与单轮完整链路完全同源（不再整页滚动取前 N 凑数）
            log.info(f"  单轮式素材精选（与单轮链路同质，目标 {target} 份）")
            materials = self._collect_materials_single_style(target)
        else:
            materials = self.collect_materials_batch(target)'''
assert old in src, 'stage1 anchor'
src = src.replace(old, new, 1)

# 2) 阶段2：生成分支
old = '''        if LLM_MODE == "api":
            self._batch_generate_api(materials, print_progress,
                                     reset_progress)
        else:
            self._batch_generate_web(materials)'''
new = '''        from config.story import BATCH_QUALITY_FIRST
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
                self._batch_generate_web(materials)'''
assert old in src, 'stage2 anchor'
src = src.replace(old, new, 1)

# 3) 阶段2.5：质量优先下生成已带反馈重试，只复核不再盲重试
old = '''        retried_ok = 0
        if non_compliant:
            # ★ 检查是否启用格式重试
            from config.story import ENABLE_FORMAT_RETRY

            if not ENABLE_FORMAT_RETRY:'''
new = '''        retried_ok = 0
        if non_compliant:
            from config.story import ENABLE_FORMAT_RETRY, BATCH_QUALITY_FIRST
            if BATCH_QUALITY_FIRST:
                log.warning(f"  质量优先：{len(non_compliant)} 篇经带反馈重试仍不合格，标记废稿（不发布）")
                for m in non_compliant:
                    m["story"] = None
            elif not ENABLE_FORMAT_RETRY:'''
assert old in src, 'stage25 anchor'
src = src.replace(old, new, 1)

# 4) 阶段3：评分排序乘题材先验
old = '''            scored = score_stories(generated)
            if scored and not any("score" in it for it in scored):'''
new = '''            scored = score_stories(generated) or []
            scored = self._apply_prior_to_scores(scored)
            if scored and not any("score" in it for it in scored):'''
assert old in src, 'stage3 anchor'
src = src.replace(old, new, 1)

# 5) 新增单轮式素材精选方法（放在 run_batch 方法之后、类尾部之前）
tail_anchor = '''    # ============================================================
    # 步骤4：发布到知乎（DOM）
    # ============================================================'''
assert tail_anchor in src, 'tail anchor'
method = '''
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
                log.warning(f"  第 {rounds} 轮命中已收集题目，跳过："
                            f"{title[:30]}...")
                empty_rounds += 1
                continue
            seen_urls.add(url)
            seen_titles.add(title)
            materials.append({"title": title, "answer": answer,
                              "footer": footer or {}, "url": url})
            empty_rounds = 0
            log.info(f"  已精选 {len(materials)}/{target} 份："
                     f"{title[:40]}...")
        if len(materials) < target:
            log.warning(f"  单轮式精选结束：{len(materials)}/{target}"
                        f"（连续 {empty_rounds} 轮无新素材）")
        return materials

    # ============================================================
    # 步骤4：发布到知乎（DOM）
    # ============================================================'''
src = src.replace(tail_anchor, method, 1)

p.write_text(src, encoding='utf-8')
print('base.py updated')
