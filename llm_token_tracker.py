# ============================================================
# llm_token_tracker.py — API Token 用量 & 费用追踪
#
# 单例模式：所有 API 调用点通过 report() 上报 usage，
# 由 run_batch / run_single 结束时调用 summary() 打印汇总。
#
# 用法：
#   from llm_token_tracker import tracker
#   tracker.report(model, usage_dict)        # 每次 API 调用后上报
#   tracker.summary()                        # 打印汇总
#   tracker.reset()                          # 新一轮开始时清零
# ============================================================

import json
import os
import logging

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_PRICING_FILE = os.path.join(_PROJECT_ROOT, "config", "model_pricing.json")


def _load_pricing():
    """加载定价文件，返回 {model_id: {input_cache_hit, input_cache_miss, output}}"""
    if not os.path.exists(_PRICING_FILE):
        return {}
    try:
        with open(_PRICING_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        # 去掉 _comment 等非模型条目
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
    except Exception as e:
        log.warning(f"加载定价文件失败：{e}")
        return {}


class TokenTracker:
    """API Token 用量追踪器（单例）"""

    def __init__(self):
        self.reset()

    # ============================================================
    # 上报
    # ============================================================

    def report(self, model: str, usage: dict):
        """
        上报一次 API 调用的 token 用量。

        参数：
            model:  模型 ID（如 "deepseek-v4-flash"）
            usage:  API 响应中的 usage 字典，含：
                    - prompt_tokens
                    - completion_tokens
                    - total_tokens
                    - prompt_tokens_details.cached_tokens（可选，缓存命中数）
        """
        if not usage:
            return

        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        # 缓存命中（DeepSeek 在 prompt_tokens_details 中返回）
        details = usage.get("prompt_tokens_details", {}) or {}
        cached = details.get("cached_tokens", 0)

        # 确保 model 槽位存在
        if model not in self._models:
            self._models[model] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cache_hit_tokens": 0,
                "calls": 0,
            }

        m = self._models[model]
        m["prompt_tokens"] += prompt_tokens
        m["completion_tokens"] += completion_tokens
        m["total_tokens"] += total_tokens
        m["cache_hit_tokens"] += cached
        m["calls"] += 1

    # ============================================================
    # 费用计算
    # ============================================================

    def _calc_cost(self, model: str) -> dict | None:
        """计算单个模型的费用，返回 {cache_hit_cost, cache_miss_cost, output_cost, total_cost} 或 None"""
        pricing = _load_pricing()
        if model not in pricing:
            return None

        p = pricing[model]
        m = self._models.get(model, {})
        if not m:
            return None

        prompt_tokens = m.get("prompt_tokens", 0)
        completion_tokens = m.get("completion_tokens", 0)
        cache_hit = m.get("cache_hit_tokens", 0)
        cache_miss = max(0, prompt_tokens - cache_hit)

        # 价格单位是 元/百万 tokens
        cost_hit = cache_hit / 1_000_000 * p.get("input_cache_hit", 0)
        cost_miss = cache_miss / 1_000_000 * p.get("input_cache_miss", 0)
        cost_output = completion_tokens / 1_000_000 * p.get("output", 0)

        return {
            "cache_hit": cache_hit,
            "cache_miss": cache_miss,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_hit": cost_hit,
            "cost_miss": cost_miss,
            "cost_output": cost_output,
            "total_cost": cost_hit + cost_miss + cost_output,
        }

    # ============================================================
    # 汇总 & 重置
    # ============================================================

    def summary(self) -> str:
        """打印汇总到日志，返回汇总文本"""
        if not self._models:
            log.info("[Token] 本轮无 API 调用")
            return ""

        pricing = _load_pricing()
        lines = []
        lines.append("=" * 60)
        lines.append("📊 API Token 用量汇总")
        lines.append("=" * 60)

        grand_total_cost = 0.0
        any_priced = False

        for model in sorted(self._models.keys()):
            m = self._models[model]
            has_price = model in pricing

            lines.append(f"\n  🧠 {model}（{m['calls']} 次调用）")
            lines.append(f"     输入: {m['prompt_tokens']:>10,} tokens")
            lines.append(f"     输出: {m['completion_tokens']:>10,} tokens")
            lines.append(f"     合计: {m['total_tokens']:>10,} tokens")

            if has_price:
                c = self._calc_cost(model)
                if c:
                    any_priced = True
                    lines.append(f"     缓存命中: {c['cache_hit']:>10,} tokens")
                    lines.append(f"     缓存未命中: {c['cache_miss']:>10,} tokens")
                    lines.append(f"     ──────────────────────────")
                    lines.append(f"     输入费用（命中）: ¥{c['cost_hit']:.4f}")
                    lines.append(f"     输入费用（未命中）: ¥{c['cost_miss']:.4f}")
                    lines.append(f"     输出费用:           ¥{c['cost_output']:.4f}")
                    lines.append(f"     小计:               ¥{c['total_cost']:.4f}")
                    grand_total_cost += c['total_cost']
            else:
                lines.append(f"     ⚠ 无定价信息，费用未计算")

        lines.append(f"\n  {'─' * 40}")
        total_calls = sum(m['calls'] for m in self._models.values())
        lines.append(f"  📞 本轮 API 调用次数: {total_calls}")
        if any_priced:
            lines.append(f"  💰 本轮预估总费用: ¥{grand_total_cost:.4f}")
        else:
            lines.append(f"  💰 本轮无定价模型，费用未计算")
        lines.append("=" * 60)

        summary_text = "\n".join(lines)
        log.info(summary_text)
        return summary_text

    def save(self, run_type="auto", label=""):
        """
        将本轮用量汇总追加写入历史文件（JSONL 格式）。

        参数：
            run_type: 运行类型标识，如 "batch" / "single" / "test"
            label:    可选的备注标签

        返回：写入的文件路径
        """
        import datetime

        history_file = os.path.join(_PROJECT_ROOT, "data", "usage_history.jsonl")
        os.makedirs(os.path.dirname(history_file), exist_ok=True)

        total_calls = sum(m.get("calls", 0) for m in self._models.values())

        record = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "run_type": run_type,
            "label": label or "",
            "models": {},
            "total_cost": 0.0,
            "total_tokens": 0,
            "total_calls": total_calls,
        }

        for model in sorted(self._models.keys()):
            m = self._models[model]
            cost_info = self._calc_cost(model)
            entry = {
                "calls": m["calls"],
                "prompt_tokens": m["prompt_tokens"],
                "completion_tokens": m["completion_tokens"],
                "total_tokens": m["total_tokens"],
                "cache_hit_tokens": m.get("cache_hit_tokens", 0),
            }
            if cost_info:
                entry["cost"] = round(cost_info["total_cost"], 6)
                record["total_cost"] += cost_info["total_cost"]
            record["total_tokens"] += m["total_tokens"]
            record["models"][model] = entry

        record["total_cost"] = round(record["total_cost"], 6)

        with open(history_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

        log.info(f"[Token] 用量已保存至 {history_file}")
        return history_file

    def reset(self):
        """清零所有统计，新一轮开始时调用"""
        self._models = {}  # {model_id: {prompt_tokens, completion_tokens, ...}}


# ============================================================
# 全局单例
# ============================================================

tracker = TokenTracker()
