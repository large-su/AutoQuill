# ============================================================
# llm_api.py - LLM API 调用模块
#
# 职责：LLM 请求/响应（SSE 流式、评分、筛选）、prompt 构建、
#       长文流水线编排。
#
# 纯文本处理（清洗、断句、格式修复、校验、章节拆分）已迁移至
# core/story_text.py，本模块不再包含文本管线实现。
#
# 依赖：pip install requests
# ============================================================

import requests
import json
import re
import time
import math
import sys
import logging

from core.story_text import (
    clean_story_output,
    enforce_short_sentences,
    replace_em_dashes,
    fix_story_format,
    validate_story_format,
    plot_paragraph_distribution,
    ensure_chapter_complete,
    normalize_chapter_headers,
    split_batch_chapters,
    parse_score_json,
)

log = logging.getLogger(__name__)


# Prompt 构建（三种素材模式统一入口）
# ============================================================

def _resolve_meta_content(meta_knowledge, recipe):
    """
    解析要注入的元知识内容。

    有 recipe 且检索开关开启时，调用分层检索取最相关小节；
    无 recipe 或检索关闭/失败时，返回全量 meta。

    返回：
        (meta_text, was_retrieved): 元知识文本 和 是否实际做了检索
    """
    if not meta_knowledge or not str(meta_knowledge).strip():
        return "", False
    if not recipe:
        return str(meta_knowledge).strip(), False

    try:
        from applications.zhihu_story.config import META_RETRIEVAL_ENABLE, META_RETRIEVAL_TOP_K
    except ImportError:
        return str(meta_knowledge).strip(), False

    if not META_RETRIEVAL_ENABLE:
        return str(meta_knowledge).strip(), False

    try:
        from meta_learner import retrieve_meta_sections
        retrieved = retrieve_meta_sections(
            meta_knowledge, recipe, top_k=META_RETRIEVAL_TOP_K
        )
        if retrieved and len(retrieved.strip()) > 50:
            log.info(
                f"  [元知识检索] 从 {len(str(meta_knowledge))} 字符中"
                f" 检索出 top-{META_RETRIEVAL_TOP_K} 相关小节"
                f"（{len(retrieved)} 字符）"
            )
            return retrieved, True
    except Exception as e:
        log.warning(f"  [元知识检索] 检索失败，回退全量注入：{e}")

    return str(meta_knowledge).strip(), False


def build_story_prompt(question_title, reference_answer=None, recipe=None,
                       meta_knowledge=None, author_profile=None):
    """
    根据 STORY_MATERIAL_MODE 构建故事生成 prompt。

    四种模式：
      - "sample"               参考文章开头截取（默认）：前 3000 字直接注入，零 LLM 提炼
      - "recipe"               配方驱动（从当前文章提炼配方，不附参考原文）
      - "reference"            参考文章模式（整篇注入，旧逻辑）
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    问题标题
        reference_answer:  参考回答文本
        recipe:            配方 dict（包含 hook/conflict/... 字段）
        meta_knowledge:    跨任务积累的元知识文本（可选）。
                          若 STORY_RECIPE_PROMPT 内含 {meta_knowledge} 占位符，
                          会直接填入；否则作为一个独立的"心法节"追加到 prompt 末尾。
                          建议只传经过 meta_learner.load_meta_knowledge()
                          处理后的正文（已剥除元数据块）。
        author_profile:    作者技能签名 dict（author_profiler.load_author_profile
                          的返回）。非 None 时把风格签名渲染为独立节追加到 prompt
                          末尾（generate_story 的 author= 参数会自动加载）。

    返回：(user_message, mode_str)
    """
    from applications.zhihu_story.prompts import STORY_SYSTEM_PROMPT

    try:
        from applications.zhihu_story.config import STORY_MATERIAL_MODE
    except ImportError:
        STORY_MATERIAL_MODE = "reference"

    # 预先格式化 meta 节（占位符注入 + 追加节 两种路径共用）
    _meta_text_for_placeholder = ""  # 填进 {meta_knowledge} 占位符的完整文本
    _meta_section_for_append = ""    # 用作追加节的完整文本

    # 分层检索（有 recipe 时取最相关小节，否则全量）
    _meta_content, _meta_retrieved = _resolve_meta_content(
        meta_knowledge, recipe
    )
    _has_meta = bool(_meta_content)

    if _has_meta:
        try:
            from applications.zhihu_story.prompts import META_STORY_INJECT_SECTION
            # 渲染好的完整心法节，含前导标题
            rendered_section = META_STORY_INJECT_SECTION.format(
                meta_knowledge=_meta_content
            )
            _meta_text_for_placeholder = rendered_section
            _meta_section_for_append = rendered_section
        except ImportError:
            # 兜底：META_STORY_INJECT_SECTION 未配置时，退回到裸文本
            _meta_text_for_placeholder = (
                "\n\n## 创作心法（来自跨篇作品的积累）\n\n"
                + str(_meta_content).strip() + "\n"
            )
            _meta_section_for_append = _meta_text_for_placeholder

    def _format_recipe(template, recipe, reference_section=""):
        """
        格式化 recipe 到 prompt。

        reference_section：
          - "recipe" 模式 → ""
          - "recipe_and_reference" 模式 → 参考文章指引块
        """
        return template.format(
            hook=recipe.get("hook", "自由发挥"),
            conflict=recipe.get("conflict", "自由发挥"),
            pacing=recipe.get("pacing", "自由发挥"),
            style=recipe.get("style", "自由发挥"),
            character=recipe.get("character", "自由发挥"),
            perspective=recipe.get("perspective", "不限"),
            tone=recipe.get("tone", "不限"),
            meta_knowledge=_meta_text_for_placeholder,
            reference_section=reference_section,
        )

    def _maybe_append_meta(prompt_body, template_source):
        """
        若 meta 存在且 template 中没有 {meta_knowledge} 占位符
        （说明 meta 没有被 _format_recipe 注入），则追加心法节到 prompt 末尾。

        返回：(最终 prompt, 是否实际注入了 meta)
        """
        if not _has_meta:
            return prompt_body, False
        # 检查原 template 是否含占位符
        if "{meta_knowledge}" in template_source:
            # 已在 _format_recipe 中填入，不再追加
            return prompt_body, True
        # 没占位符 → 追加
        return prompt_body + _meta_section_for_append, True

    # === 模式1：纯配方 ===
    if STORY_MATERIAL_MODE == "recipe" and recipe:
        from applications.zhihu_story.prompts import STORY_RECIPE_PROMPT
        recipe_prompt = _format_recipe(STORY_RECIPE_PROMPT, recipe,
                                       reference_section="")
        recipe_prompt, injected = _maybe_append_meta(
            recipe_prompt, STORY_RECIPE_PROMPT
        )
        user_message = f"{recipe_prompt}\n\n请为以下知乎问题创作一个全新的故事：\n\n{question_title}"
        meta_tag = " +心法" if injected else ""
        mode_str = f"配方模式{meta_tag} [{recipe.get('genre', '?')}] {recipe.get('perspective', '?')} hook={recipe.get('hook', '?')[:15]}"

    # === 模式2：配方 + 参考文章 ===
    elif STORY_MATERIAL_MODE == "recipe_and_reference" and recipe:
        from applications.zhihu_story.prompts import STORY_RECIPE_PROMPT
        ref_section = (
            "\n## 参考文章\n\n"
            "以下\"高赞文章\"仅供风格借鉴，感受其语感、节奏和氛围即可。\n"
            "注意：必须是全新构思的故事，情节设定必须完全避开参考文章！"
            "绝不允许搬运任何情节或角色！\n"
        )
        recipe_prompt = _format_recipe(STORY_RECIPE_PROMPT, recipe,
                                       reference_section=ref_section)
        recipe_prompt, injected = _maybe_append_meta(
            recipe_prompt, STORY_RECIPE_PROMPT
        )
        user_message = f"""{recipe_prompt}

以下是"全新文章主题"（知乎问题）：
{question_title}

以下是"高赞文章"（仅供风格借鉴）：
{reference_answer or '（无参考文章）'}

请根据以上创作指引和风格参考，创作一个全新的故事。"""
        meta_tag = " +心法" if injected else ""
        mode_str = (f"配方+参考{meta_tag} [{recipe.get('genre', '?')}] {recipe.get('perspective', '?')} "
                    f"hook={recipe.get('hook', '?')[:15]}（参考{len(reference_answer or '')}字）")

    # === 模式3：参考文章采样（默认：开头 3000 字截取注入，零 LLM 提炼） ===
    elif STORY_MATERIAL_MODE == "sample":
        from core.story_text import sample_reference_sections
        sample = sample_reference_sections(reference_answer) \
            if reference_answer else ""
        system_body = STORY_SYSTEM_PROMPT
        injected = False
        if _has_meta:
            system_body = system_body + _meta_section_for_append
            injected = True
        meta_tag = " +心法" if injected else ""
        if sample:
            user_message = f"""{system_body}

## 全新文章主题（知乎问题）

{question_title}

## 参考文章（高赞回答开头——仅供感受语感与节奏，严禁借鉴情节）

{sample}

请根据以上要求，创作一个全新的故事。"""
            mode_str = f"采样模式{meta_tag}（参考{len(sample)}字）"
        else:
            # 无参考文章：仅基础要求 + 主题
            user_message = f"""{system_body}

## 全新文章主题（知乎问题）

{question_title}

请根据以上要求，创作一个全新的故事。"""
            mode_str = f"采样模式{meta_tag}（无参考文章）"

    # === 模式4：纯参考文章（旧逻辑 / 兜底） ===
    else:
        # 参考文章模式：STORY_SYSTEM_PROMPT 里没有 recipe 占位符，
        # 直接追加心法节即可
        system_body = STORY_SYSTEM_PROMPT
        injected = False
        if _has_meta:
            system_body = system_body + _meta_section_for_append
            injected = True
        user_message = f"""{system_body}

以下是"全新文章主题"（知乎问题）：
{question_title}

以下是"高赞文章"（参考风格）：
{reference_answer}

请根据以上内容，按照要求，开始创作全新的故事。"""
        meta_tag = " +心法" if injected else ""
        mode_str = f"参考文章模式{meta_tag}（{len(reference_answer or '')} 字符）"

    # === 风格签名注入（通用在前，作者专用在后） ===
    author_tag = ""
    if author_profile:
        try:
            from applications.zhihu_story.author_profiler import (
                render_style_section, render_general_section,
                load_general_profile)
            general = load_general_profile()
            general_section = render_general_section(general)
            if general_section:
                user_message += general_section
                author_tag = " +通用风格"
            user_message += render_style_section(author_profile)
            author_tag += f" +作者:{author_profile.get('author', '?')}"
        except Exception as e:
            log.warning(f"  [作者风格注入] 渲染失败，跳过：{e}")
    mode_str = mode_str + author_tag

    return user_message, mode_str


    if not splits:
        # 尝试 ## N. 格式
        pattern = re.compile(
            r'(?:^|\n)(##\s*(\d+)[\.、\s][^\n]*)',
            re.MULTILINE
        )
        splits = list(pattern.finditer(text))

    if not splits:
        # 尝试 ## 第N章 格式
        pattern = re.compile(
            r'(?:^|\n)(##\s*第\s*(\d+)\s*章[^\n]*)',
            re.MULTILINE
        )
        splits = list(pattern.finditer(text))

    if not splits:
        log.warning("[SplitChapters] 无法从批量输出中解析章节分隔符")
        return []

    # 第一个章节标题之前的文字视为引言，并入第一章
    intro_text = text[:splits[0].start()].strip()

    chapters = []
    for i, m in enumerate(splits):
        num = int(m.group(2))
        start = m.start()
        if i + 1 < len(splits):
            next_start = splits[i + 1].start()
            body = text[start:next_start].strip()
        else:
            body = text[start:].strip()

        if body.startswith('\n'):
            body = body[1:]

        # 引言并入第一章开头
        if i == 0 and intro_text:
            body = intro_text + "\n\n" + body

        chapters.append((num, body))

    return chapters


# ============================================================
# 长文模式主函数（大纲→批量写作交替流水线）
# ============================================================

def generate_long_form_story(question_title, recipe=None,
                              meta_knowledge=None, workspace=None):
    """
    长文模式（大纲→批量写作交替）：Foundation → 大纲⇄批量写作 循环。

    流水线：
      阶段 -1：生成故事基石（foundation）
      阶段 1-N：批量大纲 → 批量章节 → 批量大纲 → 批量章节 → ...
      阶段 N+1：拼接全文 + 全局格式修复 + 校验

    参数：
        workspace: 可选 StoryWorkspace 实例。用于 --resume 恢复，跳过 foundation
                   直接从批量循环继续。

    返回：完整故事文本，失败返回 None
    """
    from applications.zhihu_story.config import LONG_FORM_CHAPTER_COUNT, BATCH_CHAPTER_COUNT
    from core.story_workspace import StoryWorkspace

    # --resume: 使用已有 workspace 恢复
    if workspace is not None:
        ws = workspace
        resume = True
    else:
        ws = StoryWorkspace()
        resume = False

    total = LONG_FORM_CHAPTER_COUNT
    total_batches = (total + BATCH_CHAPTER_COUNT - 1) // BATCH_CHAPTER_COUNT

    if not resume:
        total_steps = 1 + total_batches * 2  # foundation + (大纲+正文)×批数
    else:
        p = ws.progress
        if not p:
            log.error("[长文模式-恢复] _progress.json 缺失或损坏，无法继续")
            return None
        remaining_chapters = total - p['last_chapter_written']
        remaining_batches = (remaining_chapters + BATCH_CHAPTER_COUNT - 1) // BATCH_CHAPTER_COUNT
        total_steps = remaining_batches * 2
        print(f"\n  ⏮ 恢复: {p['last_chapter_written']}/{total} 章已完成，"
              f"剩余 {remaining_chapters} 章（{remaining_batches} 批）\n")

    print(f"  ══ 长文模式 · {total} 章 · 每批 {BATCH_CHAPTER_COUNT} 章 · {total_batches} 批 ══")
    print(f"  Story ID: {ws.story_id}")
    print()

    step = 1

    # ================================================================
    # 阶段 -1：故事基石
    # ================================================================
    if not resume:
        if ws.progress is None:
            ws.progress = {
                'title': question_title[:50],
                'total_chapters': total,
                'last_chapter_written': 0,
                'status': 'in_progress',
            }

        print(f"  [{step}/{total_steps}] 生成故事基石...")
        foundation = _generate_foundation(question_title, recipe,
                                          stream_to_terminal=True)
        if not foundation:
            print(f"  ✗ 故事基石生成失败")
            log.error("[长文模式] 故事基石生成失败")
            return None
        ws.foundation = foundation
        chars = len(foundation)
        print(f"  [{step}/{total_steps}] 故事基石 ✓ ({chars} 字)")
        step += 1
    else:
        foundation = ws.foundation
        if not foundation:
            log.error("[长文模式-恢复] foundation.md 不存在或为空，无法继续")
            return None
        log.info(f"[长文模式-恢复] 跳过 Foundation，从第 {ws.progress['last_chapter_written'] + 1} 章继续")

    # ================================================================
    # 批量交替循环
    # ================================================================
    batch_num = (ws.progress['last_chapter_written'] // BATCH_CHAPTER_COUNT) + 1 if resume else 1

    while ws.progress['last_chapter_written'] < total:
        last_written = ws.progress['last_chapter_written']
        remaining = total - last_written
        batch_count = min(BATCH_CHAPTER_COUNT, remaining)
        start_ch = last_written + 1
        end_ch = start_ch + batch_count - 1

        # —— 大纲 ——
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章大纲...")
        batch_outline = _generate_batch_outline(ws, recipe, stream_to_terminal=True)
        if not batch_outline:
            print(f"  ✗ 大纲生成失败")
            log.error(f"[长文模式] 第{batch_num}批大纲生成失败")
            return None
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章大纲 ✓ "
              f"({len(batch_outline)} 字)")
        step += 1

        # —— 正文 ——
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章正文...")
        batch_text = _generate_batch_chapters(
            ws, recipe, meta_knowledge=meta_knowledge, stream_to_terminal=True
        )
        if not batch_text:
            print(f"  ✗ 章节生成失败")
            log.error(f"[长文模式] 第{batch_num}批章节生成失败")
            return None
        print(f"  [{step}/{total_steps}] 第 {start_ch}-{end_ch} 章正文 ✓ "
              f"({len(batch_text)} 字)")
        step += 1

        # 拆分章节
        chapters = split_batch_chapters(batch_text)
        if not chapters:
            log.error(f"[长文模式] 无法从第{batch_num}批输出中拆分章节")
            return None

        # 逐章清洗、格式修复、归一化、保存
        ch_stats = []
        for ch_num, ch_text in chapters:
            ch_text = clean_story_output(ch_text)
            ch_text = fix_story_format(ch_text)
            ch_text, is_complete = ensure_chapter_complete(ch_text)
            if not is_complete:
                log.warning(f"[长文-第{ch_num}章] 末尾不完整，已回截")
            ch_text = normalize_chapter_headers(ch_text, ch_num)
            if len(ch_text) < 80:
                log.warning(f"[长文-第{ch_num}章] 内容过短（{len(ch_text)} 字）")
            elif not re.search(r'[。！？」』]', ch_text):
                log.warning(f"[长文-第{ch_num}章] 缺少句末标点，可能格式异常")
            ws.save_chapter(ch_num, ch_text)
            ch_stats.append(f"第{ch_num}章 {len(ch_text)}字")

        print(f"         {' · '.join(ch_stats)}")

        # 保存大纲快照
        ws.snapshot_outline(batch_num)

        # 更新进度
        last_ch = chapters[-1][0]
        ws.progress['last_chapter_written'] = last_ch
        batch_num += 1

    # ================================================================
    # 拼接 + 校验 + 导出
    # ================================================================
    print(f"\n  拼接全文 + 格式校验...")
    full_story = ws.assemble()
    full_story = fix_story_format(full_story)

    score, is_valid, details = validate_story_format(full_story)
    if not is_valid:
        log.warning(f"[长文模式] 全文格式校验不通过（{score}/10），"
                    f"详情：{details}")

    total_chars = len(full_story)
    ws.export_final()

    status = "✓" if is_valid else "⚠"
    print(f"\n  ══ {status} 完成！{total} 章 · {total_chars} 字 · 格式 {score}/10 ══")
    print(f"  输出目录: {ws._dir}")
    print()

    return full_story


# ============================================================
# 长文模式并行版本
# ============================================================

def generate_long_form_story_parallel(question_title, task_id,
                                       progress, recipe=None, meta_knowledge=None):
    """
    长文模式并行版本：用于批量并行生成场景。
    与 generate_long_form_story 的区别：不流式打印，通过 progress dict 报告状态。
    """
    from applications.zhihu_story.config import LONG_FORM_CHAPTER_COUNT, BATCH_CHAPTER_COUNT
    from core.story_workspace import StoryWorkspace

    short_title = question_title[:20] + "..." if len(question_title) > 20 else question_title

    progress[task_id] = {
        'status': '生成中·基石',
        'chars': 0,
        'elapsed': 0,
        'title': short_title,
    }

    start_total = time.time()
    ws = StoryWorkspace(task_id=task_id)

    # === 阶段 -1：生成故事基石（静默） ===
    foundation = _generate_foundation(question_title, recipe, stream_to_terminal=False)
    if not foundation:
        progress[task_id]['status'] = '❌ 基石失败'
        return None
    ws.foundation = foundation

    ws.progress = {
        'title': question_title[:50],
        'total_chapters': LONG_FORM_CHAPTER_COUNT,
        'last_chapter_written': 0,
        'status': 'in_progress',
    }

    # === 批量交替循环 ===
    total = LONG_FORM_CHAPTER_COUNT
    batch_num = 1
    total_chars = 0

    while ws.progress['last_chapter_written'] < total:
        last_written = ws.progress['last_chapter_written']
        remaining = total - last_written
        batch_count = min(BATCH_CHAPTER_COUNT, remaining)
        start_ch = last_written + 1
        end_ch = start_ch + batch_count - 1

        # 批量大纲
        progress[task_id]['status'] = f'生成中·大纲({start_ch}-{end_ch})'
        batch_outline = _generate_batch_outline(ws, recipe, stream_to_terminal=False)
        if not batch_outline:
            progress[task_id]['status'] = f'❌ 大纲失败'
            return None

        # 批量章节
        progress[task_id]['status'] = f'生成中·{start_ch}-{end_ch}章'
        batch_text = _generate_batch_chapters(
            ws, recipe, meta_knowledge=meta_knowledge, stream_to_terminal=False
        )
        if not batch_text:
            progress[task_id]['status'] = f'❌ 第{start_ch}-{end_ch}章失败'
            return None

        # 拆分 + 清洗 + 保存
        chapters = split_batch_chapters(batch_text)
        if not chapters:
            progress[task_id]['status'] = f'❌ 拆分失败'
            return None

        for ch_num, ch_text in chapters:
            ch_text = clean_story_output(ch_text)
            ch_text = fix_story_format(ch_text)
            ch_text, is_complete = ensure_chapter_complete(ch_text)
            if not is_complete:
                log.warning(f"[长文-并行-第{ch_num}章] 末尾不完整，已回截")
            ch_text = normalize_chapter_headers(ch_text, ch_num)
            if len(ch_text) < 80:
                log.warning(f"[长文-并行-第{ch_num}章] 内容过短（{len(ch_text)} 字）")
            ws.save_chapter(ch_num, ch_text)
            total_chars += len(ch_text)

        ws.snapshot_outline(batch_num)
        last_ch = chapters[-1][0]
        ws.progress['last_chapter_written'] = last_ch
        batch_num += 1

        progress[task_id].update({
            'status': f'生成中·{last_ch}/{total}章',
            'chars': total_chars,
            'elapsed': time.time() - start_total,
        })

    # === 拼接 + 校验 ===
    full_story = ws.assemble()
    full_story = fix_story_format(full_story)

    score, is_valid, details = validate_story_format(full_story)
    if not is_valid:
        log.warning(f"[长文-并行] 格式校验不通过（{score}/10），详情：{details}")

    ws.export_final()

    elapsed = time.time() - start_total
    progress[task_id].update({
        'status': f'✓ 完成({total}章)',
        'chars': len(full_story),
        'elapsed': elapsed,
    })

    return full_story


# ============================================================
# 通用流式 LLM 调用（长文模式 + 短文模式共用）
# ============================================================

def _call_llm_streaming(user_message, max_tokens, temperature=None,
                         on_chunk=None, label="LLM"):
    """
    通用的流式 chat.completions 调用。

    参数：
        user_message: 完整的用户消息文本
        max_tokens:   max_tokens 参数
        temperature:  温度（None 则用 config.LLM_API_TEMPERATURE）
        on_chunk:     可选回调 fn(content_chunk: str)。
                      若为 None：不打印不回调（静默累积）；
                      若为 sys.stdout.write：实时打印到终端。
        label:        日志标签

    返回：(full_content: str, elapsed: float, error: str or None)
    """
    from config import (
        LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL,
        LLM_API_TEMPERATURE, LLM_API_TIMEOUT,
        LLM_API_FREQUENCY_PENALTY, LLM_API_PRESENCE_PENALTY,
        LLM_API_EXTRA_BODY,
    )
    try:
        from config import (
            LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT,
            LLM_API_STREAM_FIRST_TOKEN_TIMEOUT,
            LLM_API_STREAM_IDLE_TIMEOUT,
        )
    except ImportError:
        LLM_API_CONNECT_TIMEOUT = 20
        LLM_API_STREAM_READ_TIMEOUT = LLM_API_TIMEOUT
        LLM_API_STREAM_FIRST_TOKEN_TIMEOUT = 45
        LLM_API_STREAM_IDLE_TIMEOUT = 60

    if not LLM_API_KEY:
        return "", 0.0, "API Key 未配置"

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": user_message}],
        "max_tokens": max_tokens,
        "temperature": temperature if temperature is not None else LLM_API_TEMPERATURE,
        "frequency_penalty": LLM_API_FREQUENCY_PENALTY,
        "presence_penalty": LLM_API_PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    start = time.time()
    full_content = ""
    last_usage = None
    response = None
    stream_stop = None
    stream_watchdog = None
    stream_state = {
        'first_content_at': None,
        'last_content_at': None,
        'timeout_reason': None,
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload,
            timeout=(LLM_API_CONNECT_TIMEOUT, LLM_API_STREAM_READ_TIMEOUT),
            stream=True,
        )
        response.encoding = "utf-8"

        if response.status_code != 200:
            return full_content, time.time() - start, \
                f"HTTP {response.status_code}: {response.text[:300]}"

        # Socket read timeout 无法区分 SSE 心跳与真实文本，
        # 用独立计时器观察实际内容 token 的到达。
        import threading
        stream_started = time.time()
        stream_stop = threading.Event()

        def _watch_stream_content():
            while not stream_stop.wait(0.5):
                now = time.time()
                first_content_at = stream_state['first_content_at']
                if first_content_at is None:
                    if now - stream_started < LLM_API_STREAM_FIRST_TOKEN_TIMEOUT:
                        continue
                    stream_state['timeout_reason'] = (
                        f"等待首个内容 token 超过 {LLM_API_STREAM_FIRST_TOKEN_TIMEOUT}s "
                        "（连接后无内容）"
                    )
                elif now - stream_state['last_content_at'] >= LLM_API_STREAM_IDLE_TIMEOUT:
                    stream_state['timeout_reason'] = (
                        f"内容停滞超过 {LLM_API_STREAM_IDLE_TIMEOUT}s（无新 token）"
                    )
                else:
                    continue

                try:
                    response.close()
                except Exception:
                    pass
                return

        stream_watchdog = threading.Thread(
            target=_watch_stream_content,
            name=f"llm-stream-watchdog-{label}",
            daemon=True,
        )
        stream_watchdog.start()

        for line in response.iter_lines(decode_unicode=True):
            if stream_state['timeout_reason']:
                break
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                if "usage" in chunk and chunk.get("usage"):
                    last_usage = chunk["usage"]
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                # 推理模型（deepseek-v4 等）先流 reasoning_content 再流 content：
                # 思维链期间的增量同样视为"流有活动"，避免看门狗误杀
                if delta.get("reasoning_content"):
                    now = time.time()
                    if stream_state['first_content_at'] is None:
                        stream_state['first_content_at'] = now
                    stream_state['last_content_at'] = now
                    continue
                content = delta.get("content", "")
                if content:
                    now = time.time()
                    if stream_state['first_content_at'] is None:
                        stream_state['first_content_at'] = now
                    stream_state['last_content_at'] = now
                    full_content += content
                    if on_chunk:
                        try:
                            on_chunk(content)
                        except Exception:
                            pass
            except json.JSONDecodeError:
                continue

        if stream_state['timeout_reason']:
            return (
                full_content,
                time.time() - start,
                stream_state['timeout_reason'],
            )

        if last_usage:
            try:
                from llm_token_tracker import tracker
                tracker.report(LLM_API_MODEL, last_usage)
            except Exception:
                pass

        return full_content, time.time() - start, None

    except requests.exceptions.Timeout:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return (
            full_content,
            time.time() - start,
            f"超时：连接/读取超过 {LLM_API_STREAM_READ_TIMEOUT}s",
        )
    except requests.exceptions.ConnectionError as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"ConnectionError: {e}"
    except Exception as e:
        if stream_state['timeout_reason']:
            return full_content, time.time() - start, stream_state['timeout_reason']
        return full_content, time.time() - start, f"Exception: {e}"
    finally:
        if stream_stop is not None:
            stream_stop.set()
        if stream_watchdog is not None:
            stream_watchdog.join(timeout=1)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def generate_story(question_title, reference_answer=None, recipe=None,
                   meta_knowledge=None, author=None):
    """
    通过 API 生成故事，支持流式输出到终端。

    根据 config.LONG_FORM_MODE 分流：
      - True  → 长文模式（大纲→批量写作交替流水线）
      - False → 短文模式（默认）：单轮按 STORY_MATERIAL_MODE 生成

    短文模式根据 config.STORY_MATERIAL_MODE 自动选择 prompt 构建方式：
      - "recipe"               配方驱动
      - "reference"            参考文章模式
      - "recipe_and_reference" 配方 + 参考文章结合

    参数：
        question_title:    知乎问题标题
        reference_answer:  高赞回答文本
        recipe:            知识库配方 dict
        meta_knowledge:    跨任务积累的元知识文本（可选，用于 --use-meta 模式）
        author:            作者名（可选）。从 data/authors/{name}.json 加载
                           技能签名并注入 prompt（仅短文模式）

    返回：
        生成的故事文本（已清洗），失败返回 None
    """
    from config import LLM_API_KEY, LLM_API_MAX_TOKENS, LLM_API_MODEL

    if not LLM_API_KEY:
        log.error("API Key 未配置！请在 config.py 中设置 LLM_API_KEY")
        return None

    # ===== 长文模式分发（盐选投稿） =====
    try:
        from applications.zhihu_story.config import LONG_FORM_MODE
    except ImportError:
        LONG_FORM_MODE = False
    if LONG_FORM_MODE:
        if author:
            log.warning("  [作者风格] 长文模式暂不支持作者注入，跳过")
        return generate_long_form_story(
            question_title, recipe=recipe, meta_knowledge=meta_knowledge,
        )

    author_profile = _load_author_profile_or_none(author)

    # ===== 统一 prompt 构建 =====
    user_message, mode_str = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge, author_profile=author_profile,
    )

    log.info(f"API 流式调用开始")
    log.info(f"  模型：{LLM_API_MODEL} | 模式：{mode_str}")
    log.info(f"  问题：{question_title[:40]}...")
    print()
    print("  ── 生成内容开始 ──")

    # 心跳：长生成可能持续数分钟，期间无日志会让外层看门狗
    # （日志 mtime 静默超时）误判卡死杀进程——定期写进度
    _heartbeat = {"n": 0}

    def _on_chunk(c):
        sys.stdout.write(c)
        sys.stdout.flush()
        _heartbeat["n"] += len(c)
        if _heartbeat["n"] >= 400:
            log.info(f"    生成中… 累计输出 {_heartbeat['n']} 字符")
            _heartbeat["n"] = 0

    full_content, elapsed, error = _call_llm_streaming(
        user_message,
        max_tokens=LLM_API_MAX_TOKENS,
        on_chunk=_on_chunk,
        label=f"短文 [{mode_str}]",
    )

    print()
    print("  ── 生成内容结束 ──")
    print()

    if error and not full_content:
        log.error(f"API 调用失败：{error}")
        return None

    if error and full_content:
        log.warning(f"API 部分失败（{error}），使用已接收内容（{len(full_content)} 字符）")

    log.info(f"  ✓ 流式生成完成！耗时 {elapsed:.1f}s | {len(full_content)} 字符")

    # 清洗 + 格式后处理
    cleaned = clean_story_output(full_content)
    cleaned = fix_story_format(cleaned)
    if len(cleaned) != len(full_content):
        log.info(f"  清洗+后处理后：{len(cleaned)} 字符（原始 {len(full_content)} 字符）")

    return cleaned


# ============================================================
# 并行生成（批量模式用）
# ============================================================

def _load_author_profile_or_none(author):
    """按作者名加载技能签名；无签名文件或失败时返回 None。"""
    if not author:
        return None
    try:
        from applications.zhihu_story.author_profiler import load_author_profile
        profile = load_author_profile(author)
        if profile:
            log.info(f"  [作者风格] 已加载「{author}」技能签名")
        else:
            log.warning(f"  [作者风格] 未找到「{author}」技能签名"
                        f"（先运行 author_profiler）")
        return profile
    except Exception as e:
        log.warning(f"  [作者风格] 加载失败：{e}")
        return None


def generate_story_parallel(question_title, reference_answer, task_id, progress,
                            recipe=None, meta_knowledge=None, author=None):
    """
    非流式生成故事，用于多线程并行调用。

    与 generate_story() 的区别：
    - 不使用流式输出（避免多线程 stdout 交叉）
    - 通过 progress dict 实时报告状态
    - 线程安全

    参数：
        question_title:    知乎问题标题
        reference_answer:  高赞回答文本（配方模式下可为 None）
        task_id:           任务编号（从1开始）
        progress:          共享进度字典
        recipe:            知识库配方（可选，提供则使用配方模式）
        meta_knowledge:    跨任务积累的元知识文本（可选）
        author:            作者名（可选），注入其技能签名

    返回：
        生成的故事文本（已清洗），失败返回 None
    """
    from config import LLM_API_KEY, LLM_API_MAX_TOKENS

    short_title = question_title[:20] + "..." if len(question_title) > 20 else question_title

    # 初始化进度
    progress[task_id] = {
        'status': '等待中',
        'chars': 0,
        'elapsed': 0,
        'started_at': None,
        'title': short_title,
    }

    if not LLM_API_KEY:
        progress[task_id]['status'] = '❌ 无Key'
        return None

    # ===== 长文模式分发（盐选投稿） =====
    try:
        from applications.zhihu_story.config import LONG_FORM_MODE
    except ImportError:
        LONG_FORM_MODE = False
    if LONG_FORM_MODE:
        if author:
            log.warning("  [作者风格] 长文模式暂不支持作者注入，跳过")
        return generate_long_form_story_parallel(
            question_title, task_id, progress,
            recipe=recipe, meta_knowledge=meta_knowledge,
        )

    author_profile = _load_author_profile_or_none(author)

    # ===== 统一 prompt 构建 =====
    user_message, _ = build_story_prompt(
        question_title, reference_answer, recipe,
        meta_knowledge=meta_knowledge, author_profile=author_profile,
    )

    local_start = time.time()
    accumulated = {"text": ""}
    first_token_logged = False
    progress[task_id].update({
        'status': '生成中',
        'started_at': local_start,
    })
    log.info(f"  任务 {task_id} 开始 API 生成")

    def _on_chunk(c):
        nonlocal first_token_logged
        if not first_token_logged:
            first_token_logged = True
            log.info(f"  任务 {task_id} 收到首个正文 token")
        accumulated["text"] += c
        elapsed = time.time() - local_start
        progress[task_id].update({
            'chars': len(accumulated["text"]),
            'elapsed': elapsed,
        })

    full_content, elapsed, error = _call_llm_streaming(
        user_message,
        max_tokens=LLM_API_MAX_TOKENS,
        on_chunk=_on_chunk,
    )

    if error:
        progress[task_id].update({
            'status': f'❌ {error[:30]}',
            'elapsed': elapsed,
        })
        log.warning(f"  任务 {task_id} 生成失败：{error}")
        return None

    cleaned = clean_story_output(full_content)
    cleaned = fix_story_format(cleaned)

    progress[task_id].update({
        'status': f'✓ 完成',
        'chars': len(cleaned),
        'elapsed': elapsed,
    })

    return cleaned


# ============================================================
# KB 配置解析（DRY：filter_story_questions 和 score_stories 共用）
# ============================================================

def _resolve_kb_config():
    """
    解析知识库任务用的 API 配置（KB 优先，故事生成回退）。

    返回: (api_key: str, base_url: str, model: str, extra_body: dict)
    """
    from config import LLM_API_KEY, LLM_API_BASE_URL, LLM_API_MODEL
    try:
        from config import KB_LLM_API_KEY as _kb_key
    except ImportError:
        _kb_key = LLM_API_KEY
    try:
        from config import KB_LLM_BASE_URL as _kb_url
    except ImportError:
        _kb_url = LLM_API_BASE_URL
    try:
        from config import KB_LLM_MODEL as _model
    except ImportError:
        _model = LLM_API_MODEL
    try:
        from config import KB_LLM_EXTRA_BODY as _extra_body
    except ImportError:
        _extra_body = {}
    return (
        (_kb_key or LLM_API_KEY),
        (_kb_url or LLM_API_BASE_URL),
        _model,
        dict(_extra_body or {}),
    )


# ============================================================
# 故事领域筛选
# ============================================================

def filter_story_questions(questions):
    """
    用 LLM 判断候选问题中哪些属于故事/小说/文学创作领域。

    参数：
        questions: [{title, ...}, ...]

    返回：
        过滤后的问题列表（只保留故事领域的）
    """
    api_key, base_url, _MODEL, extra_body = _resolve_kb_config()

    if not api_key or not questions:
        return questions

    titles = [q['title'] for q in questions]

    from applications.zhihu_story.prompts import FILTER_PROMPT
    prompt = FILTER_PROMPT
    for i, t in enumerate(titles):
        prompt += f"{i+1}. {t}\n"

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.1,
        "stream": False
    }
    if extra_body:
        payload.update(extra_body)

    try:
        log.info("LLM 故事领域筛选...")
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.encoding = "utf-8"  # 强制 UTF-8，避免响应头无 charset 时中文乱码
        if resp.status_code != 200:
            log.warning(f"筛选 API 失败：{resp.status_code}")
            return questions

        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

        # ★ Token 用量上报
        try:
            from llm_token_tracker import tracker
            tracker.report(_MODEL, data.get("usage", {}))
        except Exception:
            pass

        log.info(f"  LLM 回复：{reply}")

        # "无"或类似回复 → 没有适合的问题
        if reply.strip() == "无" or ("没有" in reply and len(reply) < 15):
            log.warning("  LLM 认为没有适合写故事的问题")
            for q in questions:
                q['is_story'] = False
            return []  # 返回空列表，外部会兜底

        # 提取保留的编号（正向逻辑）
        # 兼容多种格式：逗号分隔 "1,3,5" / 中文标点 "1、3、5" / 空格分隔 "1 3 5"
        #            / "保留1,2,3" / "编号1、3" 等
        numbers = re.findall(r'\d+', reply)
        keep_indices = set()
        for n in numbers:
            idx = int(n) - 1
            if 0 <= idx < len(questions):
                keep_indices.add(idx)

        # 兜底：如果按数字没解析到，尝试按中文大写数字或"全部保留"之类的文本
        if not keep_indices:
            if any(kw in reply for kw in ('全部保留', '全部适合', '都适合', '都保留',
                                           '都适合写故事', '均适合', '均保留')):
                log.info("  LLM 认为全部适合写故事")
                return questions
            log.warning("  未解析到有效编号，返回全部兜底")
            return questions

        # 正向保留
        filtered = [questions[i] for i in sorted(keep_indices)]
        excluded = [questions[i] for i in range(len(questions)) if i not in keep_indices]

        for q in filtered:
            q['is_story'] = True
        for q in excluded:
            q['is_story'] = False

        kept_titles = [q['title'][:25] for q in filtered]
        excluded_titles = [q['title'][:25] for q in excluded]
        log.info(f"  保留 {len(filtered)}/{len(questions)} 个故事问题：{kept_titles}")
        if excluded_titles:
            log.info(f"  排除 {len(excluded)} 个非故事问题：{excluded_titles}")

        return filtered if filtered else questions  # 兜底

    except Exception as e:
        log.warning(f"筛选出错：{e}，返回全部")
        return questions


# ============================================================
# API 连接测试
# ============================================================

def test_api_connection():
    """测试 API 连接"""
    from config import (
        LLM_API_KEY,
        LLM_API_BASE_URL,
        LLM_API_MODEL,
        LLM_API_EXTRA_BODY,
    )

    if not LLM_API_KEY:
        print("  ❌ API Key 未配置！")
        return False

    print(f"  测试 API 连接...")
    print(f"  地址：{LLM_API_BASE_URL}")
    print(f"  模型：{LLM_API_MODEL}")
    print(f"  Key：{LLM_API_KEY[:8]}...{LLM_API_KEY[-4:]}")

    url = f"{LLM_API_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}"
    }
    payload = {
        "model": LLM_API_MODEL,
        "messages": [{"role": "user", "content": "请回复：连接成功"}],
        "max_tokens": 1000,
        "stream": False
    }
    if isinstance(LLM_API_EXTRA_BODY, dict):
        payload.update(LLM_API_EXTRA_BODY)

    try:
        start = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.encoding = "utf-8"  # 强制 UTF-8
        elapsed = time.time() - start

        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            print(f"  ✓ 连接成功！（{elapsed:.1f}s）回复：{reply}")
            return True
        else:
            print(f"  ❌ HTTP {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ {e}")
        return False



# ============================================================
# 文章质量评分
# ============================================================

def score_stories(stories_data):
    """
    用 LLM 对多篇故事进行质量评分（知乎读者视角）。
    
    评分维度（6项，每项1-10分，满分60）：
    1. 开头冲击力（3秒生死线）
    2. 情节节奏（心跳图vs生产线）
    3. 情绪与人物（活人vs提线木偶）
    4. 语言人味（说人话vs播音腔）
    5. 结尾余味（留钩vs句号）
    6. 细节质感（毛坯房vs样板间）
    
    参数：
        stories_data: [{
            'index': 序号,
            'title': 问题标题,
            'story': 故事全文,
            'url': 问题链接,
            'md_path': .md 文件路径,
        }, ...]
    
    返回：
        按总分降序排列的列表，每个元素增加 'score' 和 'score_detail' 字段
    """
    api_key, base_url, _MODEL, extra_body = _resolve_kb_config()

    if not api_key or not stories_data:
        log.warning("评分跳过（无 API Key 或无故事）")
        return stories_data

    log.info(f"=" * 50)
    log.info(f"文章质量评分（共 {len(stories_data)} 篇）")
    log.info(f"=" * 50)

    # 构建评分 prompt
    from applications.zhihu_story.prompts import SCORE_PROMPT
    prompt = SCORE_PROMPT
    try:
        from applications.zhihu_story.config import SCORE_STORY_HEAD_CHARS, SCORE_STORY_TAIL_CHARS
    except ImportError:
        SCORE_STORY_HEAD_CHARS = 1000
        SCORE_STORY_TAIL_CHARS = 500

    def _build_score_preview(story):
        """评分只看开头+结尾，降低 prompt 体积。"""
        story = story or ""
        head_chars = max(0, SCORE_STORY_HEAD_CHARS)
        tail_chars = max(0, SCORE_STORY_TAIL_CHARS)
        if len(story) <= head_chars + tail_chars:
            return story
        head = story[:head_chars]
        tail = story[-tail_chars:] if tail_chars else ""
        omitted = len(story) - head_chars - tail_chars
        return (
            f"{head}\n\n...(中间省略 {omitted} 字)...\n\n"
            f"【结尾片段】\n{tail}"
        )

    for i, item in enumerate(stories_data):
        story_preview = _build_score_preview(item['story'])
        
        prompt += f"\n--- 故事 {i+1}（问题：{item['title'][:50]}）---\n"
        prompt += story_preview
        prompt += "\n"

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max(4000, len(stories_data) * 350 + 500),
        "temperature": 0.3,  # 低温度保证评分稳定
        "stream": False
    }
    if extra_body:
        payload.update(extra_body)

    try:
        log.info("发送评分请求...")
        import time as _time
        start = _time.time()
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.encoding = "utf-8"  # 强制 UTF-8
        elapsed = _time.time() - start

        if resp.status_code != 200:
            log.error(f"评分 API 失败：{resp.status_code}")
            return stories_data

        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

        # ★ Token 用量上报
        try:
            from llm_token_tracker import tracker
            tracker.report(_MODEL, data.get("usage", {}))
        except Exception:
            pass

        log.info(f"评分完成（{elapsed:.1f}s）")

        # 解析 JSON
        # 清理可能的 markdown 代码块
        clean_reply = reply.strip()
        if clean_reply.startswith("```"):
            clean_reply = clean_reply.split("\n", 1)[1] if "\n" in clean_reply else clean_reply[3:]
        if clean_reply.endswith("```"):
            clean_reply = clean_reply[:-3]
        clean_reply = clean_reply.strip()

        scores = parse_score_json(clean_reply, len(stories_data))

        # 将评分合并到 stories_data
        score_map = {s['index']: s for s in scores}

        for i, item in enumerate(stories_data):
            idx = i + 1
            if idx in score_map:
                s = score_map[idx]
                item['score'] = s.get('total', 0)
                item['score_detail'] = {
                    '开头冲击力': s.get('hook', 0),
                    '情节节奏': s.get('plot', s.get('pacing', 0)),
                    '情感共鸣': s.get('emotion', s.get('character', 0)),
                    '真实感': s.get('authenticity', s.get('language', 0)),
                    '结尾余味': s.get('ending', 0),
                    '格式体验': s.get('format', s.get('texture', 0)),
                }
                item['score_comment'] = s.get('comment', '')

                detail = ' | '.join(f"{k}={v}" for k, v in item['score_detail'].items())
                log.info(f"  故事 {idx}「{item['title'][:30]}...」")
                log.info(f"    总分={item['score']} | {detail}")
                log.info(f"    点评：{item['score_comment']}")
            else:
                item['score'] = 0
                item['score_detail'] = {}
                item['score_comment'] = '评分缺失'

        # 按总分降序排列
        stories_data.sort(key=lambda x: x.get('score', 0), reverse=True)

        log.info(f"\n  排名：")
        for rank, item in enumerate(stories_data):
            log.info(f"  第{rank+1}名: [{item['score']}分] {item['title'][:40]}...")

        return stories_data

    except json.JSONDecodeError as e:
        log.error(f"评分结果 JSON 解析失败：{e}")
        log.error(f"  原始回复（前 500 字）：{reply[:500]}")
        log.error(f"  原始回复（后 300 字）：{reply[-300:]}")
        return stories_data

    except Exception as e:
        log.error(f"评分出错：{e}")
        return stories_data
