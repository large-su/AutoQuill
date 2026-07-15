# ============================================================
# visualize_usage.py — API Token 用量历史可视化
#
# 用法：
#   python visualize_usage.py              → 生成 PNG 图表
#   python visualize_usage.py --show       → 生成并弹出交互窗口
#   python visualize_usage.py --days 30    → 只看最近 30 天
#
# 依赖：pip install matplotlib
# 数据源：data/usage_history.jsonl（由 main.py 每次运行自动写入）
# ============================================================

import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import matplotlib
matplotlib.use("TkAgg")  # 兼容 Windows
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "data", "usage_history.jsonl")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "data", "usage_report.png")

# ============================================================
# 数据加载
# ============================================================


def load_history():
    records = []
    if not os.path.exists(DATA_FILE):
        print(f"[!] 未找到数据文件：{DATA_FILE}")
        print("    请先运行一次主程序（main.py），它会自动创建此文件。")
        return records

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def filter_by_days(records, days):
    if days <= 0:
        return records
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for r in records:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
            if ts >= cutoff:
                result.append(r)
        except (ValueError, KeyError):
            result.append(r)
    return result


# ============================================================
# 绘图
# ============================================================


def plot_dashboard(records, output_path, show=False):
    if not records:
        print("[!] 没有数据可绘制。")
        return

    # 解析时间戳
    timestamps = []
    costs = []
    tokens_list = []
    run_types = []
    model_costs_agg = defaultdict(float)

    for r in records:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            continue
        timestamps.append(ts)
        costs.append(r.get("total_cost", 0))
        tokens_list.append(r.get("total_tokens", 0))
        run_types.append(r.get("run_type", "?"))

        for model, m in r.get("models", {}).items():
            model_costs_agg[model] += m.get("cost", 0)

    if not timestamps:
        print("[!] 没有有效时间戳的数据。")
        return

    # --- 按日汇总 ---
    daily = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "runs": 0, "calls": 0})
    for r in records:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            continue
        day = ts.strftime("%Y-%m-%d")
        daily[day]["cost"] += r.get("total_cost", 0)
        daily[day]["tokens"] += r.get("total_tokens", 0)
        daily[day]["runs"] += 1
        daily[day]["calls"] += r.get("total_calls", sum(
            m.get("calls", 0) for m in r.get("models", {}).values()
        ))

    days_sorted = sorted(daily.keys())
    day_objs = [datetime.strptime(d, "%Y-%m-%d") for d in days_sorted]
    daily_costs = [daily[d]["cost"] for d in days_sorted]
    daily_tokens = [daily[d]["tokens"] for d in days_sorted]
    daily_runs = [daily[d]["runs"] for d in days_sorted]
    daily_calls = [daily[d]["calls"] for d in days_sorted]

    # 累积成本
    cum_cost = []
    running = 0.0
    for c in daily_costs:
        running += c
        cum_cost.append(running)

    # ============================================================
    # 创建图表（4 行 × 2 列）
    # ============================================================

    fig = plt.figure(figsize=(20, 18))
    fig.suptitle("AutoQuill API Token 用量 & 费用报表",
                 fontsize=18, fontweight='bold', y=0.98)

    # ----- 1. 每日费用（折线 + 柱状） -----
    ax1 = fig.add_subplot(4, 2, 1)
    bars = ax1.bar(day_objs, daily_costs, color='steelblue', alpha=0.7,
                   edgecolor='white', linewidth=0.5, label='每日费用')
    ax1.plot(day_objs, daily_costs, 'o-', color='darkblue', markersize=4, linewidth=1)
    for bar, val in zip(bars, daily_costs):
        if val > 0:
            ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.002,
                     f'元{val:.2f}', ha='center', va='bottom', fontsize=7, color='dimgray')

    ax1.set_ylabel('费用 (元)', fontsize=11)
    ax1.set_title('每日 API 费用', fontsize=13, fontweight='bold')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax1.xaxis.set_major_locator(mdates.DayLocator())
    ax1.tick_params(axis='x', rotation=45, labelsize=8)
    ax1.grid(axis='y', alpha=0.3)
    ax1.legend(fontsize=8)

    # ----- 2. 累积费用（面积图） -----
    ax2 = fig.add_subplot(4, 2, 2)
    ax2.fill_between(day_objs, 0, cum_cost, color='coral', alpha=0.4)
    ax2.plot(day_objs, cum_cost, 'o-', color='darkred', markersize=4, linewidth=1.5)
    if cum_cost:
        ax2.annotate(
            f'元{cum_cost[-1]:.2f}',
            xy=(day_objs[-1], cum_cost[-1]),
            xytext=(10, 10), textcoords='offset points',
            fontsize=10, fontweight='bold', color='darkred',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8),
        )
    ax2.set_ylabel('累积费用 (元)', fontsize=11)
    ax2.set_title('累积 API 费用', fontsize=13, fontweight='bold')
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax2.xaxis.set_major_locator(mdates.DayLocator())
    ax2.tick_params(axis='x', rotation=45, labelsize=8)
    ax2.grid(axis='y', alpha=0.3)

    # ----- 3. 每日 API 调用次数（柱状） -----
    ax3 = fig.add_subplot(4, 2, 3)
    bars3 = ax3.bar(day_objs, daily_calls, color='mediumpurple', alpha=0.7,
                    edgecolor='white', linewidth=0.5)
    ax3.plot(day_objs, daily_calls, 'o-', color='darkviolet', markersize=4, linewidth=1)
    for bar, val in zip(bars3, daily_calls):
        if val > 0:
            ax3.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                     str(val), ha='center', va='bottom', fontsize=7, color='dimgray')
    ax3.set_ylabel('API 调用次数', fontsize=11)
    ax3.set_title('每日 API 调用次数', fontsize=13, fontweight='bold')
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax3.xaxis.set_major_locator(mdates.DayLocator())
    ax3.tick_params(axis='x', rotation=45, labelsize=8)
    ax3.grid(axis='y', alpha=0.3)

    # ----- 4. 每日 Token 用量（堆叠面积：输入 vs 输出） -----
    ax4 = fig.add_subplot(4, 2, 4)
    daily_prompt = defaultdict(int)
    daily_completion = defaultdict(int)
    for r in records:
        try:
            ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
            day = ts.strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            continue
        for m in r.get("models", {}).values():
            daily_prompt[day] += m.get("prompt_tokens", 0)
            daily_completion[day] += m.get("completion_tokens", 0)

    all_days = sorted(set(list(daily_prompt.keys()) + list(daily_completion.keys())))
    all_objs = [datetime.strptime(d, "%Y-%m-%d") for d in all_days]
    prompt_vals = [daily_prompt.get(d, 0) / 1000 for d in all_days]
    completion_vals = [daily_completion.get(d, 0) / 1000 for d in all_days]

    ax4.fill_between(all_objs, 0, prompt_vals, color='skyblue', alpha=0.6, label='输入 (prompt)')
    ax4.fill_between(all_objs, prompt_vals,
                     [p + c for p, c in zip(prompt_vals, completion_vals)],
                     color='salmon', alpha=0.6, label='输出 (completion)')
    ax4.plot(all_objs, [p + c for p, c in zip(prompt_vals, completion_vals)],
             'o-', color='darkred', markersize=3, linewidth=1)
    ax4.set_ylabel('Tokens (K)', fontsize=11)
    ax4.set_title('每日 Token 用量（输入 vs 输出）', fontsize=13, fontweight='bold')
    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax4.xaxis.set_major_locator(mdates.DayLocator())
    ax4.tick_params(axis='x', rotation=45, labelsize=8)
    ax4.legend(fontsize=8)
    ax4.grid(axis='y', alpha=0.3)

    # ----- 5. 模型费用分布（饼图） -----
    ax5 = fig.add_subplot(4, 2, 5)
    model_names = sorted(model_costs_agg.keys(), key=lambda m: model_costs_agg[m], reverse=True)
    model_vals = [model_costs_agg[m] for m in model_names]
    colors = plt.cm.Set3(range(len(model_names)))
    wedges, texts, autotexts = ax5.pie(
        model_vals, labels=None, autopct='%1.1f%%',
        colors=colors, startangle=90, pctdistance=0.8,
    )
    for at in autotexts:
        at.set_fontsize(8)
    ax5.set_title('模型费用分布', fontsize=13, fontweight='bold')
    legend_labels = [f'{n}\n(元{v:.2f})' for n, v in zip(model_names, model_vals)]
    ax5.legend(
        wedges, legend_labels, title='模型', fontsize=7,
        loc='center left', bbox_to_anchor=(1, 0, 0.5, 1),
    )

    # ----- 6. 每次运行的费用（散点 + 趋势线） -----
    ax6 = fig.add_subplot(4, 2, 6)
    run_nums = list(range(1, len(timestamps) + 1))
    type_colors = {"batch": "steelblue", "single": "coral", "test": "green"}
    for i, (ts, c, rt) in enumerate(zip(timestamps, costs, run_types)):
        color = type_colors.get(rt, 'gray')
        ax6.scatter(run_nums[i], c, c=color, s=40, alpha=0.7, edgecolors='white', linewidth=0.5)

    if len(costs) >= 3:
        window = min(5, len(costs))
        ma = []
        for i in range(len(costs)):
            start_i = max(0, i - window + 1)
            ma.append(sum(costs[start_i:i + 1]) / (i - start_i + 1))
        ax6.plot(run_nums, ma, '--', color='darkred', linewidth=1, alpha=0.7, label=f'{window}次移动均线')

    ax6.set_xlabel('运行序号', fontsize=11)
    ax6.set_ylabel('费用 (元)', fontsize=11)
    ax6.set_title('每次运行费用', fontsize=13, fontweight='bold')
    ax6.grid(axis='y', alpha=0.3)

    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor='steelblue',
               markersize=8, label='批量 (batch)'),
        Line2D([0], [0], marker='o', color='w', markerfacecolor='coral',
               markersize=8, label='单篇 (single)'),
    ]
    ax6.legend(handles=legend_elements, fontsize=8, loc='upper left')

    # ----- 7. 每次运行的 API 调用次数（散点 + 趋势线） -----
    ax7 = fig.add_subplot(4, 2, 7)
    run_calls = []
    for r in records:
        tc = r.get("total_calls", sum(m.get("calls", 0) for m in r.get("models", {}).values()))
        run_calls.append(tc)

    for i, (rn, tc, rt) in enumerate(zip(run_nums, run_calls, run_types)):
        color = type_colors.get(rt, 'gray')
        ax7.scatter(rn, tc, c=color, s=40, alpha=0.7, edgecolors='white', linewidth=0.5)

    if len(run_calls) >= 3:
        window = min(5, len(run_calls))
        ma_c = []
        for i in range(len(run_calls)):
            start_i = max(0, i - window + 1)
            ma_c.append(sum(run_calls[start_i:i + 1]) / (i - start_i + 1))
        ax7.plot(run_nums, ma_c, '--', color='darkviolet', linewidth=1, alpha=0.7, label=f'{window}次移动均线')

    ax7.set_xlabel('运行序号', fontsize=11)
    ax7.set_ylabel('API 调用次数', fontsize=11)
    ax7.set_title('每次运行 API 调用次数', fontsize=13, fontweight='bold')
    ax7.grid(axis='y', alpha=0.3)
    ax7.legend(handles=legend_elements, fontsize=8, loc='upper left')

    # ----- 8. 统计摘要（文本面板） -----
    ax8 = fig.add_subplot(4, 2, 8)
    ax8.axis('off')

    total_cost = sum(costs)
    total_tokens = sum(tokens_list)
    total_calls = sum(r.get("total_calls", sum(m.get("calls", 0) for m in r.get("models", {}).values())) for r in records)
    avg_cost_per_run = total_cost / len(costs) if costs else 0
    avg_tokens_per_run = total_tokens / len(tokens_list) if tokens_list else 0
    avg_calls_per_run = total_calls / len(records) if records else 0
    total_runs = len(records)
    batch_runs = sum(1 for rt in run_types if rt == "batch")
    single_runs = sum(1 for rt in run_types if rt == "single")
    date_range = f"{days_sorted[0]} ～ {days_sorted[-1]}" if days_sorted else "无"

    summary_lines = [
        "=== 统计摘要 ===",
        "",
        f"数据范围：{date_range}",
        f"总运行次数：{total_runs}（批量 {batch_runs} / 单篇 {single_runs}）",
        f"总 API 调用次数：{total_calls:,}",
        f"总 Token 用量：{total_tokens:,}",
        f"总费用：元{total_cost:.4f}",
        f"平均每次费用：元{avg_cost_per_run:.4f}",
        f"平均每次 Token：{avg_tokens_per_run:,.0f}",
        f"平均每次 API 调用：{avg_calls_per_run:.1f}",
        "",
        "按模型分布：",
    ]
    for m in sorted(model_costs_agg.keys(), key=lambda m: model_costs_agg[m], reverse=True):
        summary_lines.append(f"  {m}: 元{model_costs_agg[m]:.4f}")

    summary_lines += [
        "",
        f"图表保存至：{output_path}",
    ]

    for i, line in enumerate(summary_lines):
        y_pos = 0.95 - i * 0.048
        fontweight = 'bold' if i == 0 else 'normal'
        fontsize = 13 if i == 0 else 10
        ax8.text(0.05, y_pos, line, transform=ax8.transAxes,
                 fontsize=fontsize, fontweight=fontweight,
                 verticalalignment='top')

    # ============================================================
    # 保存 & 显示
    # ============================================================

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    print(f"[OK] 报表已保存至：{output_path}")

    if show:
        plt.show()
    else:
        plt.close()


# ============================================================
# 入口
# ============================================================

def main():
    days = 0
    show = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--days' and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        elif args[i] == '--show':
            show = True
            i += 1
        elif args[i] in ('-h', '--help'):
            print(__doc__)
            return
        else:
            i += 1

    print("=" * 50)
    print("  AutoQuill API 用量可视化")
    print("=" * 50)

    records = load_history()
    print(f"  加载了 {len(records)} 条运行记录")

    if days > 0:
        records = filter_by_days(records, days)
        print(f"  筛选最近 {days} 天 → {len(records)} 条")

    print(f"  正在生成图表...")
    plot_dashboard(records, OUTPUT_FILE, show=show)


if __name__ == "__main__":
    main()
