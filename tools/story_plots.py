# ============================================================
# tools/story_plots.py — 故事段落分布可视化（从 core/story_text.py 迁出）
#
# 职责：段落长度统计与 KDE 分布图绘制。可视化不属于核心文本库，
# 故移至 tools；核心库保持零 matplotlib/numpy 依赖。
# ============================================================

import logging

from core.story_text import PARA_LENGTH_THRESHOLD

log = logging.getLogger(__name__)

def plot_paragraph_distribution(generated_materials, output_dir=None):
    """
    对所有已生成的故事做段落长度统计，绘制 KDE 分布图 + 输出统计信息到日志。

    参数：
        generated_materials: list of dict，每个 dict 包含 'story', 'title', 'index'
        output_dir: 图片保存目录，默认为项目 output/

    返回：
        图片保存路径
    """
    import os
    import numpy as np

    if not generated_materials:
        log.warning("  没有可分析的故事")
        return None

    # --- 收集数据 ---
    all_stats = []
    for m in generated_materials:
        story = m.get('story', '')
        if not story:
            continue
        paras = [l for l in story.split('\n') if l.strip() and not l.strip().startswith('#')]
        lengths = [len(p.strip()) for p in paras]
        if not lengths:
            continue
        arr = np.array(lengths)
        stats = {
            'index': m.get('index', '?'),
            'title': m.get('title', '')[:15],
            'lengths': arr,
            'median': float(np.median(arr)),
            'mean': float(np.mean(arr)),
            'p90': float(np.percentile(arr, 90)),
            'max': float(np.max(arr)),
            'total_paras': len(arr),
            'over_threshold': int(np.sum(arr > PARA_LENGTH_THRESHOLD)),
            'over_ratio': float(np.mean(arr > PARA_LENGTH_THRESHOLD)),
        }
        all_stats.append(stats)

    if not all_stats:
        return None

    # --- 输出统计到日志 ---
    log.info(f"\n{'─'*50}")
    log.info(f"段落长度分析（阈值 {PARA_LENGTH_THRESHOLD} 字）")
    log.info(f"{'─'*50}")
    log.info(f"  {'#':>3s}  {'中位':>5s}  {'P90':>5s}  {'最长':>5s}  {'段落数':>5s}  {'超标':>4s}  {'占比':>5s}  标题")
    log.info(f"  {'---':>3s}  {'---':>5s}  {'---':>5s}  {'---':>5s}  {'---':>5s}  {'---':>4s}  {'---':>5s}  ---")

    for s in sorted(all_stats, key=lambda x: x['index']):
        flag = '✗' if s['over_ratio'] > 0.10 else ('△' if s['over_ratio'] > 0.05 else '✓')
        log.info(f"  {s['index']:>3d}  {s['median']:>5.0f}  {s['p90']:>5.0f}  {s['max']:>5.0f}  "
                 f"{s['total_paras']:>5d}  {s['over_threshold']:>4d}  {s['over_ratio']:>5.0%}  "
                 f"{flag} {s['title']}")

    # 汇总
    all_lengths = np.concatenate([s['lengths'] for s in all_stats])
    log.info(f"\n  全局统计（{len(all_stats)} 篇，共 {len(all_lengths)} 段）：")
    log.info(f"    中位数={np.median(all_lengths):.0f}  均值={np.mean(all_lengths):.0f}  "
             f"P75={np.percentile(all_lengths, 75):.0f}  P90={np.percentile(all_lengths, 90):.0f}  "
             f"P95={np.percentile(all_lengths, 95):.0f}  最大={np.max(all_lengths):.0f}")
    log.info(f"    超 {PARA_LENGTH_THRESHOLD} 字段落：{np.sum(all_lengths > PARA_LENGTH_THRESHOLD)}/{len(all_lengths)} "
             f"（{np.mean(all_lengths > PARA_LENGTH_THRESHOLD):.1%}）")

    # --- 绘图 ---
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
    except ImportError:
        log.warning("  matplotlib 未安装，跳过绘图（pip install matplotlib）")
        return None

    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'Arial', 'Helvetica'],
        'font.size': 9,
        'axes.linewidth': 0.8,
        'axes.edgecolor': '#333333',
        'axes.labelcolor': '#333333',
        'xtick.color': '#333333',
        'ytick.color': '#333333',
        'figure.dpi': 150,
    })

    fig, (ax_main, ax_box) = plt.subplots(
        2, 1, figsize=(10, 7), height_ratios=[3, 1],
        gridspec_kw={'hspace': 0.25}
    )

    # -- 上图：KDE 分布 --
    colors = plt.cm.tab20(np.linspace(0, 1, len(all_stats)))

    for i, s in enumerate(sorted(all_stats, key=lambda x: x['index'])):
        lengths = s['lengths']
        # 手动 KDE（高斯核）
        x_grid = np.linspace(0, min(300, np.max(lengths) + 20), 200)
        bw = max(np.std(lengths) * 0.4, 3)  # 带宽
        kde = np.zeros_like(x_grid)
        for val in lengths:
            kde += np.exp(-0.5 * ((x_grid - val) / bw) ** 2)
        kde /= (len(lengths) * bw * np.sqrt(2 * np.pi))

        label = f"#{s['index']} (med={s['median']:.0f})"
        ax_main.plot(x_grid, kde, color=colors[i], alpha=0.7, linewidth=1.2, label=label)

    # 阈值线
    ax_main.axvline(x=PARA_LENGTH_THRESHOLD, color='#e74c3c', linestyle='--',
                     linewidth=1.5, alpha=0.8, label=f'阈值 {PARA_LENGTH_THRESHOLD} 字')

    ax_main.set_xlabel('段落长度（字符数）')
    ax_main.set_ylabel('密度')
    ax_main.set_title('各篇故事段落长度分布', fontsize=12, fontweight='bold', pad=10)
    ax_main.set_xlim(0, min(300, np.percentile(all_lengths, 99) + 20))
    ax_main.grid(True, alpha=0.3, linewidth=0.5)
    ax_main.legend(fontsize=7, loc='upper right', ncol=2,
                    framealpha=0.9, edgecolor='#cccccc')

    # -- 下图：箱线图 --
    box_data = [s['lengths'] for s in sorted(all_stats, key=lambda x: x['index'])]
    box_labels = [f"#{s['index']}" for s in sorted(all_stats, key=lambda x: x['index'])]

    try:
        bp = ax_box.boxplot(box_data, tick_labels=box_labels, vert=False, patch_artist=True,
                             showfliers=False, widths=0.6,
                             medianprops=dict(color='#e74c3c', linewidth=1.5),
                             boxprops=dict(linewidth=0.8))
    except TypeError:
        # matplotlib < 3.9 兼容
        bp = ax_box.boxplot(box_data, labels=box_labels, vert=False, patch_artist=True,
                             showfliers=False, widths=0.6,
                             medianprops=dict(color='#e74c3c', linewidth=1.5),
                             boxprops=dict(linewidth=0.8))

    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.5)

    ax_box.axvline(x=PARA_LENGTH_THRESHOLD, color='#e74c3c', linestyle='--',
                    linewidth=1.5, alpha=0.8)
    ax_box.set_xlabel('段落长度（字符数）')
    ax_box.set_xlim(ax_main.get_xlim())
    ax_box.grid(True, axis='x', alpha=0.3, linewidth=0.5)
    ax_box.set_title('各篇段落长度箱线图', fontsize=10, pad=5)

    plt.tight_layout()

    # 保存
    if output_dir is None:
        from core import paths
        output_dir = paths.data("output")
    os.makedirs(output_dir, exist_ok=True)

    from datetime import datetime
    filename = f"para_dist_{datetime.now():%Y%m%d_%H%M%S}.png"
    filepath = os.path.join(output_dir, filename)
    fig.savefig(filepath, bbox_inches='tight', facecolor='white')
    plt.close(fig)

    log.info(f"\n  分布图已保存：{filepath}")
    return filepath
