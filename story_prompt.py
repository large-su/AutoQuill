# ============================================================
# story_prompt.py — 故事 prompt 构建（由 llm_api.py 拆分，2026-08）
#
# 职责：把 素材（参考回答/配方/元知识/作者签名）渲染成
#       故事生成的完整 user prompt。不发起任何 API 调用。
#
# 架构位置：Layer 0 (Tools) — 被 story_generation / workflows 共享。
#
# 提示词本体在 applications/zhihu_story/prompts.py（知乎域内容，
# 平台抽象轮再收敛）。
# ============================================================

import logging

log = logging.getLogger(__name__)


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
        from config.story import META_RETRIEVAL_ENABLE, META_RETRIEVAL_TOP_K
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
        from config.story import STORY_MATERIAL_MODE
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
