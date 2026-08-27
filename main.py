# ============================================================
# AutoQuill v3.0 — 统一入口
#
# 用法：
#   python main.py                 批量模式（默认）：收集素材 → 生成 → 发布
#   python main.py --single        传统模式：逐轮生成即发布
#   python main.py --test-api      测试 API 连接
#
# 架构分层：
#   applications/zhihu_story/ → 应用层（采集 browser_adapter、
#                               文风蒸馏 author_profiler）
#   workflows/                → 工作流编排（知乎批量）
#   core/                     → 核心领域（story_text 正文渲染、paths 路径）
#   tools/                    → 开发期工具（不在运行时路径上）
#   web_drivers/              → LLM 网站驱动（DeepSeek DOM 驱动）
#
# 基础模块：
#   main.py              → 入口（DPI、日志、CLI 分发）
#   desktop_utils.py     → 窗口焦点、截图、终端进度面板
#   llm_api.py           → LLM API 调用（流式/非流式 + 作者风格双层注入）
#   llm_token_tracker.py → API 模式 Token 用量追踪
#   config/              → 配置包（__init__ 框架配置 + story 业务参数 + JSON 运行时数据）
#
# 知识系统：
#   kb_manager.py    → 知识库管理（配方积累、参考文章）
#   archive/         → 归档模块（OCR/UIA 旧通道、meta_learner、image_gen 等）
#   rich_progress.py  → Rich 终端进度面板
# ============================================================

import ctypes

# DPI 感知（Windows 高分屏适配）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import sys
import os
import time
import logging
from datetime import datetime

# 首启引导（必须在 config 导入之前）：安装态把 example 配置复制为
# llm_providers.json（否则 config 导入即抛 FileNotFoundError），并
# 迁移旧版（解压目录）数据到 %APPDATA%\AutoQuill。源码态均为无操作。
from core.paths import (
    data as _data_path,
    ensure_provider_file,
    migrate_legacy_data,
)
ensure_provider_file()
migrate_legacy_data()

from config import (
    LLM_MODE,
    LLM_API_KEY,
    WEB_DRIVER_NAME,
    WAIT_BETWEEN_CYCLES,
    random_delay,
)
from config.story import (
    QUESTION_SELECT_MODE,
    ENABLE_STORY_FILTER,
    STORY_MATERIAL_MODE,
    DEFAULT_BATCH_GENERATE_COUNT,
    DEFAULT_BATCH_PUBLISH_COUNT,
    MAX_TOTAL_ATTEMPTS,
)

# ============================================================
# 基础设置
# ============================================================

os.makedirs(_data_path("logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            _data_path("logs",
                       f"autoquill_{datetime.now():%Y%m%d_%H%M%S}.log"),
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


def _cleanup_old_logs(days=30, keep_recent=20):
    from pathlib import Path
    """清理 N 天前的 autoquill_*.log（保留最近若干份，防止日志无限累积）。"""
    try:
        log_dir = Path(_data_path("logs"))
        files = sorted(log_dir.glob("autoquill_*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        now = time.time()
        removed = 0
        for i, p in enumerate(files):
            if i >= keep_recent and (now - p.stat().st_mtime) > days * 86400:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("清理历史日志 %d 个（%s）", removed, log_dir)
    except Exception:
        pass  # 清理失败不影响启动


_cleanup_old_logs()

# ============================================================
# 交互辅助
# ============================================================

def _ask_int(prompt, default_value, minimum=1):
    """
    询问整数输入，支持默认值（直接回车）和重试。

    参数：
        prompt: 显示给用户的提示前缀（不含默认值提示）
        default_value: 直接回车时使用的默认值
        minimum: 允许的最小值（小于此值会要求重新输入）
    """
    while True:
        raw = input(f"  {prompt}（回车用默认 {default_value}）>> ").strip()
        if not raw:
            return default_value
        try:
            val = int(raw)
            if val >= minimum:
                return val
            print(f"    必须 ≥ {minimum}，请重新输入")
        except ValueError:
            print("    请输入整数")


def ask_batch_params():
    """
    交互式询问本次批量任务的参数。

    返回：
        (gen_count, pub_count, rounds)
        rounds: [{"round": 1, "gen_count": 20, "pub_count": 12}, ...]
    """
    print("\n  ── 本次批量任务参数 ──")
    from config.story import (
        BATCH_AUTO_GENERATE_COUNT,
        BATCH_GENERATE_REDUNDANCY_RATIO,
        BATCH_GENERATE_MIN_EXTRA,
        BATCH_ROUND_SPLIT_ENABLE,
        BATCH_MAX_PUBLISH_PER_ROUND,
    )

    def _auto_gen_count(n):
        import math
        ratio_count = math.ceil(n * (1 + BATCH_GENERATE_REDUNDANCY_RATIO))
        extra_count = n + max(0, BATCH_GENERATE_MIN_EXTRA)
        return max(n, ratio_count, extra_count)

    def _split_even(total, max_per_round):
        import math
        if not max_per_round or total <= max_per_round:
            return [total]
        round_count = math.ceil(total / max_per_round)
        base = total // round_count
        rem = total % round_count
        return [base + (1 if i < rem else 0)
                for i in range(round_count)]

    if BATCH_AUTO_GENERATE_COUNT:
        pub_count = _ask_int("要发布多少篇故事？",
                             DEFAULT_BATCH_PUBLISH_COUNT)
        pub_rounds = _split_even(
            pub_count,
            BATCH_MAX_PUBLISH_PER_ROUND
            if BATCH_ROUND_SPLIT_ENABLE else 0
        )
        rounds = []
        for i, pub_n in enumerate(pub_rounds, start=1):
            rounds.append({
                "round": i,
                "gen_count": _auto_gen_count(pub_n),
                "pub_count": pub_n,
            })
        gen_count = sum(r["gen_count"] for r in rounds)
        if len(rounds) > 1:
            plan = " / ".join(str(r["pub_count"]) for r in rounds)
            print(f"  → 总发布 {pub_count} 篇，单轮上限 "
                  f"{BATCH_MAX_PUBLISH_PER_ROUND} 篇，"
                  f"拆分为 {len(rounds)} 轮：{plan}")
            for r in rounds:
                print(f"     第 {r['round']} 轮：生成 {r['gen_count']} 篇 "
                      f"→ 择优发布 {r['pub_count']} 篇")
        else:
            print(f"  → 自动计算生成数：{gen_count} 篇"
                  f"（发布 {pub_count} 篇，冗余 "
                  f"{BATCH_GENERATE_REDUNDANCY_RATIO:.0%}，"
                  f"至少多 {BATCH_GENERATE_MIN_EXTRA} 篇）")
    else:
        gen_count = _ask_int("要生成多少篇故事？",
                             DEFAULT_BATCH_GENERATE_COUNT)
        pub_count = _ask_int("要发布多少篇故事？",
                             DEFAULT_BATCH_PUBLISH_COUNT)
        rounds = [{
            "round": 1,
            "gen_count": gen_count,
            "pub_count": pub_count,
        }]

    print()
    if len(rounds) > 1:
        print(f"  → 合计生成 {gen_count} 篇 → 分轮择优发布 {pub_count} 篇")
    elif pub_count > gen_count:
        print(f"  ⚠ 发布数 {pub_count} > 生成数 {gen_count}，"
              f"将按实际生成数发布，无需评分")
    elif pub_count == gen_count:
        print(f"  → 生成 {gen_count} 篇，全部发布（跳过评分）")
    else:
        print(f"  → 生成 {gen_count} 篇 → 评分择优发布 {pub_count} 篇")

    return gen_count, pub_count, rounds


# ============================================================
# 主入口
# ============================================================

def main():
    # --web 必须先于 banner 处理：banner 含 emoji，GBK 控制台打印即崩；
    # 且 Web 控制台不需要 OCR/API 检查
    if '--headless' in sys.argv:
        from config import set_runtime_browser_headless
        set_runtime_browser_headless(True, persist=False)
    if '--web' in sys.argv or '--service' in sys.argv:
        from webui.server import run as run_web
        run_web()
        return

    select_str = "手动" if QUESTION_SELECT_MODE == "manual" else "自动"
    llm_str = "API 流式" if LLM_MODE == "api" else "浏览器"
    filter_str = "开" if ENABLE_STORY_FILTER else "关"
    from core.version import VERSION

    print(f"""
    ╔══════════════════════════════════════════════╗
    ║       ✒️ AutoQuill v{VERSION}                    ║
    ║                                              ║
    ║  选题：{select_str}  生成：{llm_str}  故事筛选：{filter_str}  ║
    ║                                              ║
    ║  无参数      批量模式（默认）                ║
    ║  --single    传统模式（逐轮生成即发布）      ║
    ║  --test-api  测试 API 连接                   ║
    ║  --headless  浏览器无头运行（工作模式）       ║
    ║  --web       本地 Web 控制台（127.0.0.1）    ║
    ║                                              ║
    ║  v4.0：安装版发布（首启引导 + 用户数据目录） ║
    ║  安全：鼠标左上角 或 Ctrl+C 终止             ║
    ╚══════════════════════════════════════════════╝
    """)

    print(f"  选题：{QUESTION_SELECT_MODE} | LLM：{LLM_MODE} | "
          f"故事筛选：{filter_str}")

    # 知识库（kb_manager v2.1）已于 2026-08 退役；数据留存见
    # data/knowledge_base.json，后续闭环接口见 core/feedback_loop.py
    print()

    # CLI 命令分发
    if '--test-api' in sys.argv:
        from llm_client import test_api_connection
        test_api_connection()
        return

    # API/Web 模式检查
    if LLM_MODE == "api":
        from llm_client import test_api_connection
        if not test_api_connection():
            print("\n  API 连接失败，请检查 config.py 中的 LLM_API_KEY")
            return
        print()
    else:
        from config import WEB_DRIVERS
        drv_cfg = WEB_DRIVERS.get(WEB_DRIVER_NAME, {})
        mode_name = ("快速模式" if drv_cfg.get("mode") == "fast"
                     else "专家模式")
        extras = []
        if drv_cfg.get("deep_think"):
            extras.append("深度思考")
        if drv_cfg.get("smart_search"):
            extras.append("智能搜索")
        extras_str = "+".join(extras) if extras else "无"
        print(f"  Web 驱动：{WEB_DRIVER_NAME}（DOM）| {mode_name} | "
              f"附加功能：{extras_str}")

    # 素材模式
    mat_mode_names = {
        "recipe": "纯配方",
        "reference": "纯参考文章",
        "recipe_and_reference": "配方+参考文章"
    }
    print(f"  素材模式："
          f"{mat_mode_names.get(STORY_MATERIAL_MODE, STORY_MATERIAL_MODE)}")

    print("  ✓ 就绪\n")

    # 创建工作流实例
    import applications.zhihu_story.browser_adapter  # noqa: F401 注册浏览器工厂
    from workflows.zhihu import ZhihuWorkflow
    workflow = ZhihuWorkflow()

    # --- 选择运行模式 ---
    # 默认走批量模式（询问生成数/发布数）
    # --single 或 -s 走传统模式（每轮生成即发布）
    use_single_mode = ('--single' in sys.argv) or ('-s' in sys.argv)

    if use_single_mode:
        # 传统模式：逐轮生成→发布，直到成功 target 轮
        try:
            target = int(input("要成功执行几轮？>> ").strip())
        except ValueError:
            target = 1

        print(f"\n  传统模式：每轮生成即发布")
        print(f"  目标：成功 {target} 轮"
              f"（最多尝试 {MAX_TOTAL_ATTEMPTS} 轮）\n")
        input("按 Enter 开始 >> ")

        done = 0
        attempts = 0
        time_start = time.time()

        # ★ 重置 Token 追踪器
        if LLM_MODE == "api":
            try:
                from llm_token_tracker import tracker
                tracker.reset()
            except Exception:
                pass

        while done < target and attempts < MAX_TOTAL_ATTEMPTS:
            attempts += 1
            log.info(f"\n{'='*60}")
            log.info(f"第 {attempts} 次尝试"
                     f"（已成功 {done}/{target}）")
            log.info(f"{'='*60}")
            try:
                if workflow.run_single():
                    done += 1
                    log.info(f"  ✓ 成功！（{done}/{target}）")
                else:
                    log.warning(f"  ✗ 失败"
                                f"（尝试 {attempts}/{MAX_TOTAL_ATTEMPTS}）")
            except KeyboardInterrupt:
                log.info("\n中断。")
                break
            except Exception as e:
                log.error(f"本轮失败: {e}")
                take_screenshot("error")
                log.warning(f"  ✗ 异常"
                            f"（尝试 {attempts}/{MAX_TOTAL_ATTEMPTS}）")

            # ★ 修复：run_single() 结束后重置 Web Driver
            # 避免下次迭代复用已污染的 DeepSeek 会话（旧对话历史累积导致崩溃）
            if LLM_MODE == "web":
                try:
                    from web_drivers import reset_driver
                    reset_driver()
                    log.info("  Web Driver 已重置，下次迭代将使用全新会话")
                except Exception:
                    pass

            if done < target and attempts < MAX_TOTAL_ATTEMPTS:
                random_delay(WAIT_BETWEEN_CYCLES)

        time_total = time.time() - time_start

        if done >= target:
            log.info(f"\n🎉 目标达成！成功 {done}/{target} 轮"
                     f"（共尝试 {attempts} 次）")
        else:
            log.warning(f"\n⚠ 未达目标：成功 {done}/{target} 轮"
                        f"（已用完 {attempts} 次尝试）")
        log.info(f"  总耗时：{time_total:.1f}s"
                 f"（{time_total/60:.1f}分钟）")

        # ★ Token 用量汇总
        if LLM_MODE == "api":
            try:
                from llm_token_tracker import tracker
                tracker.summary()
                tracker.save(run_type="single")
            except Exception:
                pass

        return

    # --- 批量模式（默认） ---
    gen_count, pub_count, batch_rounds = ask_batch_params()

    gen_mode = "并行" if LLM_MODE == "api" else "Web"
    print(f"\n  🚀 流水线批量模式启动")
    if len(batch_rounds) > 1:
        print(f"     总目标：发布 {pub_count} 篇，拆分为 "
              f"{len(batch_rounds)} 轮")
        for r in batch_rounds:
            print(f"       第 {r['round']} 轮：收集/生成 "
                  f"{r['gen_count']} 篇 → 发布 {r['pub_count']} 篇")
    else:
        print(f"     阶段1：串行收集 {gen_count} 份素材（选题+OCR）")
    print(f"     阶段2：{gen_mode} 生成 {gen_count} 篇故事")
    if pub_count < gen_count:
        print(f"     阶段3：评分 → 择优发布 {pub_count} 篇")
    else:
        print(f"     阶段3：全部发布（{min(gen_count, pub_count)} 篇，"
              f"不评分）")
    print()
    input("按 Enter 开始流水线 >> ")

    total_published = 0
    time_batch_start = time.time()
    for i, r in enumerate(batch_rounds):
        if len(batch_rounds) > 1:
            print(f"\n  ══ 批量分轮 {i+1}/{len(batch_rounds)}："
                  f"生成 {r['gen_count']} 篇 → 发布 {r['pub_count']} 篇 ══")
            log.info(f"\n{'='*60}")
            log.info(f"批量分轮 {i+1}/{len(batch_rounds)}："
                     f"生成 {r['gen_count']} 篇 → 发布 {r['pub_count']} 篇")
            log.info(f"{'='*60}")

        try:
            published = workflow.run_batch(
                r["gen_count"],
                publish_count=r["pub_count"],
            )
            total_published += published or 0
        except KeyboardInterrupt:
            print("\n  用户中断批量任务。")
            log.info("用户中断批量任务。")
            break

        if i < len(batch_rounds) - 1:
            print(f"\n  本轮完成，累计发布 {total_published}/{pub_count} 篇。")
            random_delay(WAIT_BETWEEN_CYCLES)

    time_batch_total = time.time() - time_batch_start
    print(f"\n  ✅ 批量任务结束：累计发布 {total_published}/{pub_count} 篇，"
          f"耗时 {time_batch_total/60:.1f} 分钟")
    log.info(f"\n批量任务结束：累计发布 {total_published}/{pub_count} 篇，"
             f"耗时 {time_batch_total:.1f}s")

    # ★ Token 用量汇总
    if LLM_MODE == "api":
        try:
            from llm_token_tracker import tracker
            tracker.summary()
            tracker.save(run_type="batch")
        except Exception:
            pass


if __name__ == "__main__":
    main()
