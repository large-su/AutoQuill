# ============================================================
# core/feedback_loop.py — 发布数据反馈闭环【接口预留 · 未实现】
#
# 愿景（已论证、暂缓）：发布表现(data/published_answers_*.json 的
# 赞/读/评/藏) 关联回生成要素(题材/开头钩子/作者签名/历史配方)，
# 反哺选题策略与生成参数——即 v2.x「自生长知识库」的正确形态。
#
# 历史：kb_manager v2.1 配方库已于 2026-08 退役（2404 条配方零消费、
# 主流程 m['recipe'] 硬编码 None）。原始数据保留在
# data/knowledge_base.json 与 archive/kb_manager.py，可随时考古。
#
# 新方案定型前，本文件只提供挂载点，不写任何业务逻辑。
# ============================================================

import logging

log = logging.getLogger(__name__)


def record_story_published(url, title, meta=None):
    """[预留] 一篇故事成功发布时调用（run_single/run_batch 的落账点）。
    实现时应与 core.topic_ledger.record 合流，避免双写。
    """
    log.debug('feedback_loop.record_story_published: %s', url)
    return None


def attach_performance(url, likes=None, reads=None, comments=None,
                       collects=None):
    """[预留] 创作中心快照抓取到该篇的新互动数据时调用。
    数据源：webui/published.py 抓取管线（已有真实赞读评落数据）。
    """
    log.debug('feedback_loop.attach_performance: %s', url)
    return None


def summarize(genre=None):
    """[预留] 输出题材级爆款先验（哪个题材×哪种钩子真实跑赢），
    供选题打分与开头策略消费。当前返回 None=无先验可用。
    """
    return None
