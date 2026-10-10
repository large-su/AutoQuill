# -*- coding: utf-8 -*-
# ============================================================
# applications/zhihu_story/reply_task.py
# 评论回复作业：挑最友善的读者评论 → 读回当时的题目与回答 → 网页版大模型写回复
#
# 链路（用户 2026-09-27 口径）：
#   1. 采集评论管理页首屏 20 条；
#   2. 规则预筛：踢掉引流、戾气、纯表情、已回复、没有回答链接的；
#   3. 网页版大模型挑「最友善、最适合友善回应」的一条（只喂编号+正文，不喂用户名）；
#   4. 打开该评论对应的回答页，读回当时的题目 + 我们的回答（节选）；
#   5. 同一会话第二轮写回复 → 本地硬校验 → 不达标带原因重写（≤2 次）；
#   6. 发送（演练模式只生成不发送）→ 落台账 → 删除本次网页会话。
#
# 分层：DOM 在 browser_interact、提示词在 reply_prompts、台账在 core.checkin；
# 本模块只做编排，不直接写 JS、不直接拼提示词。
# ============================================================

import logging
import re
import time

from applications.zhihu_story import reply_prompts as rp
from core import checkin

log = logging.getLogger(__name__)

# 引流/广告：直接踢掉（用户口径：只挑最友善的，广告不算）
SPAM_PATTERNS = (
    '看我主页', '关注我', '点我', '加微信', '微信', '私信我', '公众号',
    'http', 'www.', 'vx', '威信', '扣扣', 'qq群', '优惠', '代写', '兼职',
    '扩列', '互关', '回关', '引流', '广告',
)

# 戾气/攻击：直接踢掉（用户口径：特别不友好的直接忽略）
HOSTILE_PATTERNS = (
    '垃圾', '弱智', '智障', '脑残', '傻', '滚', '恶心', '狗屎', '屎', '呕',
    '去死', '贱', '下头', '什么玩意', '胡说', '瞎写', '骗', '丢人', '有病',
    '闭嘴', '滚蛋', '烂', '踩', '举报', '取关', '拉黑', '服了', '无语',
    '浪费时间', '辣眼睛', '看着就烦', '什么垃圾', '写得像', '狗屁',
)

# 友善先验（只用于候选排序，最终挑选仍交给大模型）
POSITIVE_HINTS = (
    '好看', '喜欢', '谢谢', '感谢', '写得', '写得好', '支持', '加油', '赞',
    '精彩', '看哭', '收藏', '蹲', '求更', '期待', '厉害', '棒', '爱了', '上头',
    '有感觉', '代入', '细腻', '好看', '太好', '不错', '牛', '催更',
)
QUESTION_HINTS = ('?', '？', '吗', '呢', '怎么', '为什么', '求', '能不能', '有没有')

MAX_CANDIDATES = 10          # 交给大模型挑选的候选上限（太多会稀释判断）


def _say(progress, text):
    log.info('评论回复：%s', text)
    if progress:
        try:
            progress(text)
        except Exception:                # noqa: BLE001
            pass


def _is_emoji_only(text):
    '''纯表情/纯标点/纯符号：没有可回应的信息。

    知乎评论里的表情是方括号形式（如 [蹲][哇]），先整体剥掉再判断；
    只剩标点/空白同样算没内容。
    '''
    t = re.sub(r'\[[^\]]{1,8}\]', '', text or '')
    t = re.sub(r'[\[\]\s，。！？、,.!?~～…\-—·:：;；"\'「」“”()（）]', '', t)
    return len(t) == 0


def friendliness_score(text):
    '''友善先验分（只影响候选排序，不决定最终选谁）。'''
    t = rp.clean_comment_text(text)
    if not t:
        return 0
    score = 0
    for h in POSITIVE_HINTS:
        if h in t:
            score += 2
            break
    for h in QUESTION_HINTS:
        if h in t:
            score += 1
            break
    if 6 <= len(t) <= 60:
        score += 1
    if len(t) > 120:
        score -= 1                  # 长篇大论多半是争论
    return score


def prefilter(cards, replied_keys=(), max_candidates=MAX_CANDIDATES):
    '''规则预筛：返回 {candidates, dropped}。纯函数，可单测。

    candidates 按友善先验排序后截断到 max_candidates；dropped 带原因，
    用于通知里解释「今天为什么没回」或者「过滤掉了什么」。
    '''
    replied = set(replied_keys or ())
    candidates, dropped = [], []
    for c in cards or []:
        text = rp.clean_comment_text((c or {}).get('text'))
        author = (c or {}).get('author') or ''
        key = (c or {}).get('key') or ''
        if not text or len(text) < 2 or _is_emoji_only(text):
            dropped.append({'reason': 'no-content', 'author': author, 'text': text[:40]})
            continue
        if key and key in replied:
            dropped.append({'reason': 'replied', 'author': author, 'text': text[:40]})
            continue
        if not (c or {}).get('answer_url'):
            dropped.append({'reason': 'no-answer-url', 'author': author, 'text': text[:40]})
            continue
        low = text.lower()
        hit = next((p for p in SPAM_PATTERNS if p in low), '')
        if hit:
            dropped.append({'reason': 'spam:%s' % hit, 'author': author, 'text': text[:40]})
            continue
        hit = next((p for p in HOSTILE_PATTERNS if p in text), '')
        if hit:
            dropped.append({'reason': 'hostile:%s' % hit, 'author': author, 'text': text[:40]})
            continue
        item = dict(c)
        item['score'] = friendliness_score(text)
        candidates.append(item)
    candidates.sort(key=lambda x: (-int(x.get('score') or 0),
                                   int(x.get('index') or 0)))
    return {'candidates': candidates[:max_candidates], 'dropped': dropped}


def parse_pick(raw, count):
    '''解析大模型回的编号；0/越界/没有数字 → 0（表示没挑出来）。'''
    m = re.search(r'\d+', str(raw or ''))
    if not m:
        return 0
    n = int(m.group())
    return n if 1 <= n <= int(count or 0) else 0


def pick_from_candidates(candidates, raw):
    '''编号 → 候选 dict（编号规则与提示词共用 reply_prompts.filter_comments）。'''
    ordered = rp.filter_comments(candidates)
    idx = parse_pick(raw, len(ordered))
    if idx <= 0:
        return None
    _n, text = ordered[idx - 1]
    for c in candidates or []:
        if rp.clean_comment_text((c or {}).get('text')) == text:
            return c
    return None


def ask_llm(prompt, driver=None, reuse_session=True):
    '''按当前生成通道提问：Web 网页版（默认）或 API 通道。'''
    from config import LLM_MODE
    if LLM_MODE == 'api':
        from llm_client import call_llm_non_streaming
        return call_llm_non_streaming(prompt, max_tokens=800)
    if driver is None:
        from web_drivers import get_driver
        driver = get_driver()
    return driver.generate(prompt, reuse_session=reuse_session)


def read_original(browser, answer_url, wait=6, timeout=45000):
    '''打开回答页，读回「当时的题目 + 我们的回答」。'''
    browser.page.goto(answer_url, wait_until='domcontentloaded', timeout=timeout)
    time.sleep(wait)
    data = browser.get_primary_answer(min_length=1) or {}
    return {'title': data.get('title') or '', 'answer': data.get('answer') or ''}


def compose_reply(driver, comment, question, answer, author='', progress=None,
                  max_retry=2, empty_retry=1, reuse_session=False):
    '''写回复 + 本地硬校验 + 带原因重写。返回 {ok, reply, issues}。

    ★ 2026-09-28 真机补：网页版驱动偶发「读回空内容」（日志里
    「判定完成（15s，1 字符）」= 只读到思考态/占位符），此时回复必然是空，
    带原因重写也没用（模型没收到反馈、会话里还挂着那次空应答）——直接**另起
    一轮提问**重试，避免白白浪费掉这次回复机会。
    '''
    prompt = rp.build_reply_prompt(comment, question, answer)
    reply, issues = '', []
    first = not reuse_session
    # 总预算 = 重写次数 + 空内容重试次数（空内容也占一次预算，不会无限重试）
    budget = max(1, int(max_retry) + 1) + max(0, int(empty_retry))
    for attempt in range(budget):
        raw = ask_llm(prompt, driver=driver, reuse_session=not first)
        first = False
        reply = rp.strip_wrapping(raw)
        if not reply and attempt + 1 < budget:
            # 驱动没读到内容：不算「这版不达标」，换一轮新提问重来
            # （绝不复用那次空会话，否则会把空上下文一起带进去）
            _say(progress, '第 %d 次没读回内容（网页版驱动偶发），重新提问'
                 % (attempt + 1))
            first = True
            continue
        issues = rp.check_reply(reply, comment, author=author)
        if not issues:
            return {'ok': True, 'reply': reply, 'issues': []}
        _say(progress, '第 %d 版不达标（%s），带原因重写'
             % (attempt + 1, '；'.join(issues)))
        prompt = rp.rewrite_feedback(issues)
    return {'ok': False, 'reply': reply, 'issues': issues}


def run_reply_job(browser, count=1, dry_run=True, progress=None, now=None,
                  manage_limit=20):
    '''评论回复作业主体。返回 {ok, units, detail, replies, dropped}。

    dry_run=True：只生成、只落台账，**不发送**（用户要求先演练两天）。
    '''
    count = max(1, int(count or 1))
    state = checkin.load_state(now=now)
    today = checkin.today_key(now)
    cached = state.get('reader_comments') or {}
    if (cached.get('checked_at') or '').startswith(today) and \
            bool(cached.get('dry_run')) == bool(dry_run) and \
            isinstance(cached.get('result'), dict):
        result = dict(cached['result'])
        result.setdefault('checked', True)
        result['detail'] = '今日已检查自有故事评论：' + (result.get('detail') or '')
        return result

    def finish(result, outcome=None, cache=True, stats=None):
        """Record every normal early return and cache only a complete check."""
        result = dict(result)
        result['checked'] = bool(cache)
        if cache:
            state['reader_comments'] = {
                'checked_at': checkin.now_str(now),
                'outcome': outcome or ('sent' if result.get('units') else 'not_sent'),
                'dry_run': bool(dry_run),
                'result': dict(result),
            }
            checkin.save_state(state)
        if stats is None:
            _record_run_from_result(result, dry_run, now=now)
        else:
            _record_run(stats[0], stats[1], stats[2], stats[3], stats[4],
                        dry_run, details=stats[5], now=now)
        return result

    # 去重口径：真发过的不再回；演练阶段另外跳过「已经生成过草稿」的那些
    # （换样本，别两天看同一条），但切自动后它们会重新变成可回复。
    replied = checkin.replied_keys()
    if dry_run:
        replied = replied | checkin.dryrun_keys()
    try:
        got = browser.collect_manage_comments(limit=manage_limit)
        if not isinstance(got, dict):
            raise RuntimeError('评论管理页返回格式异常')
    except Exception as exc:             # noqa: BLE001
        log.warning('采集评论失败：%s', exc)
        result = {'ok': False, 'units': 0,
                  'detail': '评论管理页读取失败：%s' % exc,
                  'replies': [], 'dropped': []}
        return finish(result, cache=False)
    cards = got.get('comments') or []
    _say(progress, '评论管理页读到 %d 条评论' % len(cards))
    if not cards:
        empty_ok = got.get('ok') is True
        result = {'ok': empty_ok, 'units': 0,
                  'detail': ('自有故事暂无评论' if empty_ok else
                             '评论管理页没读到评论（可能未登录/改版）'),
                  'replies': [], 'dropped': []}
        return finish(result, outcome='no_candidate', cache=empty_ok,
                      stats=(0, 0, {}, 0, 0, [result['detail']]))
    filtered = prefilter(cards, replied_keys=replied)
    candidates, dropped = filtered['candidates'], filtered['dropped']
    drop_counts = {}
    for d in dropped:
        reason = str(d.get('reason') or '').split(':')[0]
        drop_counts[reason] = drop_counts.get(reason, 0) + 1
    _say(progress, '预筛后剩 %d 条候选（过滤掉 %d 条：%s）'
         % (len(candidates), len(dropped),
            '、'.join(sorted({d['reason'].split(':')[0] for d in dropped})) or '无'))
    if not candidates:
        result = {'ok': True, 'units': 0,
                  'detail': '没有可回复的新评论（%d 条里已回复/引流/戾气/无内容全被过滤）'
                            % len(cards),
                  'replies': [], 'dropped': dropped}
        return finish(result, outcome='no_candidate',
                      stats=(len(cards), len(candidates), drop_counts, 0, 0,
                             [result['detail']]))
    driver = None
    done, skipped, details = [], [], []
    pick_failed = False
    model_no_candidate = False
    try:
        from config import LLM_MODE
        if LLM_MODE != 'api':
            from web_drivers import create_driver
            driver = create_driver()
        for _i in range(count):
            pool = [c for c in candidates if c['key'] not in replied]
            if not pool:
                details.append('候选已用完')
                break
            picked = None
            try:
                prompt = rp.build_pick_prompt(pool)
                raw = ask_llm(prompt, driver=driver, reuse_session=False)
                picked = pick_from_candidates(pool, raw)
                if picked is None:
                    model_no_candidate = str(raw or '').strip() == '0'
                    pick_failed = not model_no_candidate
                    detail = ('大模型判定这批没有适合友善回应的评论' if model_no_candidate
                              else '大模型选择结果无效，未完成自有评论检查（稍后可重试）')
                    _say(progress, detail)
                    details.append(detail)
                    break
            except Exception as exc:             # noqa: BLE001
                log.warning('挑选评论失败：%s', exc)
                details.append('挑选评论失败：%s' % exc)
                pick_failed = True
                break
            comment = rp.clean_comment_text(picked.get('text'))
            _say(progress, '选中：%s（%s）' % (comment[:24], picked.get('author') or '匿名'))
            try:
                qa = read_original(browser, picked['answer_url'])
            except Exception as exc:             # noqa: BLE001
                log.warning('读回答原文失败：%s', exc)
                qa = {'title': '', 'answer': ''}
            composed = compose_reply(driver, comment, qa.get('title'),
                                     qa.get('answer'),
                                     author=picked.get('author') or '',
                                     progress=progress, reuse_session=True)
            record = {
                'key': picked.get('key') or '',
                'author': picked.get('author') or '',
                'comment': comment,
                'answer_url': picked.get('answer_url') or '',
                'question': (qa.get('title') or '')[:120],
                'reply': composed.get('reply') or '',
                'issues': composed.get('issues') or [],
                'dry_run': bool(dry_run),
                'sent': False,
            }
            if not composed.get('ok'):
                skipped.append(record)
                details.append('生成不合格（%s），今天不回这条'
                               % '；'.join(record['issues'] or ['未知']))
                checkin.append_reply(dict(record, failed=True), now=now)
                replied.add(record['key'])
                continue
            if dry_run:
                checkin.append_reply(record, now=now)
                replied.add(record['key'])
                done.append(record)
                details.append('演练：%s → %s' % (comment[:16], record['reply']))
                _say(progress, '演练完成（未发送）：%s' % record['reply'])
                continue
            # ★ 主路径：在**评论管理页**直接回复（评论就是从这页挑的，卡片必然在场）。
            #   回答页按热度只渲染前 N 条，评论一多就「找不到这条评论」——2026-09-27 真机。
            record['attempted'] = True          # 点过发送即算消耗（防重复打扰）
            try:
                sent = browser.send_reply_from_manage(
                    comment, record['reply'], answer_url=record['answer_url'])
            except Exception as exc:             # noqa: BLE001
                log.warning('管理页发送回复异常：%s', exc)
                sent = {'ok': False, 'sent': False, 'detail': str(exc)}
            if not sent.get('sent') and '已点发送' not in (sent.get('detail') or ''):
                # 还没走到「点发布」就失败了（卡片不在首屏等）→ 回退回答页路径再试一次。
                # ★ 只要点过发布就绝不换路径重试：宁可漏确认，也不重复打扰读者。
                _say(progress, '管理页回复未成（%s），改用回答页路径'
                     % (sent.get('detail') or ''))
                try:
                    browser.read_answer_comments()
                    already = browser.comment_replied(
                        comment,
                        our_texts=[r.get('reply') for r in checkin.load_replies()
                                   if r.get('reply')])
                    if already:
                        details.append('这条评论下已有我们的回复，跳过')
                        replied.add(record['key'])
                        checkin.append_reply(dict(record, skipped='already-replied'), now=now)
                        continue
                    sent = browser.send_reply(comment, record['reply'])
                except Exception as exc:         # noqa: BLE001
                    log.warning('回答页发送回复异常：%s', exc)
                    sent = {'ok': False, 'sent': False, 'detail': str(exc)}
            record['sent'] = bool(sent.get('sent'))
            record['send_detail'] = sent.get('detail') or ''
            checkin.append_reply(record, now=now)
            replied.add(record['key'])
            if sent.get('ok') and sent.get('sent'):
                done.append(record)
                details.append('已回复：%s → %s' % (comment[:16], record['reply']))
            elif sent.get('sent'):
                # ★ 2026-09-28：评论**已经发出去了**，只是没能就地确认到。
                #   这种「已发送未确认」绝不能记成任务失败——它已经消耗掉这条评论，
                #   重发就是二次打扰读者；记失败还会三连败触发熔断，把整个「回复评论」
                #   任务自动停用（用户侧表现：任务莫名消失、再也不回复了）。
                #   按「完成（待人工核对）」处理：占配额、不补位、不熔断，通知里说明白。
                done.append(record)
                details.append('已发送（未确认）：%s' % (sent.get('detail') or ''))
                _say(progress, '已发送但未确认到落地，按完成处理（不重发）：%s'
                     % comment[:16])
            else:
                skipped.append(record)
                details.append('发送失败：%s' % (sent.get('detail') or ''))
        # ★ 评论真的发出去了 → 立刻在**本地记账**里把打卡「发布评论」置为达成
        #   （2026-10-01 用户反馈：评论发出去了，界面还显示打卡未完成）。
        #   根因是这里从来没记过账，「发布评论✓」完全依赖读打卡页——而打卡页
        #   有自己的统计延迟，刚发完就读往往还是「去评论」，于是那一格永远是 ✗。
        #   注意：只认「真的发出去了」（ok 或 sent），失败/跳过绝不记账。
        if any(r.get('sent') for r in done) and not dry_run:
            checkin.mark_done(
                state, 'comment',
                detail='回复读者评论 %d 条（%s）'
                       % (len(done), done[0].get('author') or '读者'), now=now)
            log.info('评论回复：已记账，打卡「发布评论」置为达成')
        # 顺带刷新打卡状态：评论任务在打卡页上算不算达成，一次页面就读得到
        # （本地已记账的项在 update_tasks 里只升不降，页面滞后也抹不掉）
        summary = _refresh_checkin_after_reply(browser, state, progress, now=now)
        if done and summary and not summary.get('ok'):
            # 页面还没跟上（打卡页统计有延迟）：以本地记账为准重算一次摘要，
            # 免得通知里又出现「发布评论✗」把用户搞糊涂。
            fresh = checkin.summary(state)
            checkin.set_result(state, fresh['ok'], fresh['line'], now=now)
            checkin.save_state(state)
            _say(progress, fresh['line'])
    finally:
        if driver is not None:
            try:
                driver.delete_current_session()   # 用完删会话（会话纪律）
            except Exception:                     # noqa: BLE001
                pass
            try:
                driver.close_session()             # 仅关闭本作业独立页
            except Exception:                     # noqa: BLE001
                pass
    units = len(done)
    prefix = '演练' if dry_run else '回复'
    # ★ 2026-09-28：本轮**一条都没回**时，只报本轮的最后一条原因。
    #   旧写法会把前几轮的失败原因一起带上，通知里就会出现
    #   「大模型判定这批没有适合友善回应的评论：发送未确认：…（上一轮的旧账）」，
    #   让人以为这轮又发失败了——台账/通知必须只说这一轮发生了什么。
    if units:
        detail = '%s %d 条' % (prefix, units)
        if details:
            detail += '：' + '；'.join(details[-3:])
    else:
        detail = details[-1] if details else '没有可回复的评论'
    result = {'ok': (not skipped or bool(done)) and not pick_failed,
              'units': units, 'detail': detail,
              'replies': [r.get('reply') for r in done],
              'records': done + skipped, 'dropped': dropped}
    outcome = 'sent' if (any(r.get('sent') for r in done) and not dry_run) else ('no_candidate' if model_no_candidate
              else 'not_sent')
    return finish(
        result, outcome=outcome, cache=not pick_failed,
        stats=(len(cards), len(candidates), drop_counts,
               len(done) + len(skipped), units, details))




def _record_run(collected, candidates, drop_counts, picked, units, dry_run,
                details=(), now=None):
    """把本次运行的概览写进 reply_runs.jsonl（UI 展示「今天抓了什么」）。"""
    try:
        checkin.append_reply_run({
            'collected': int(collected),
            'candidates': int(candidates),
            'dropped': dict(drop_counts or {}),
            'picked': int(picked),
            'units': int(units),
            'dry_run': bool(dry_run),
            'detail': '；'.join(list(details or [])[-3:])[:300],
            'at': checkin.now_str(now),
        })
    except Exception as exc:                     # noqa: BLE001
        log.debug('回复运行统计写入失败（不影响回复）：%s', exc)


def _record_run_from_result(result, dry_run, now=None):
    records = result.get('records') or []
    dropped = result.get('dropped') or []
    counts = {}
    for row in dropped:
        reason = str((row or {}).get('reason') or '').split(':')[0]
        counts[reason] = counts.get(reason, 0) + 1
    _record_run(len(records) + len(dropped), 0, counts, len(records),
                int(result.get('units') or 0), dry_run,
                details=[result.get('detail') or ''], now=now)



def _refresh_checkin_after_reply(browser, state, progress=None, now=None):
    '''回复完顺手读一次打卡页：把「发布 1 条评论」的达成情况记进当日快照。

    返回最新摘要（读不到返回 None）。★ 页面读到什么**不会覆盖本地已记账的达成**
    （core.checkin.update_tasks 只升不降）——打卡页有自己的统计延迟。
    '''
    try:
        url = state.get('campaign_url') or ''
        if not url:
            found = browser.discover_campaign_url()
            if not found.get('ok'):
                return None
            url = found['url']
            checkin.set_campaign(state, url, found.get('text') or '')
        info = browser.read_checkin_tasks(url)
        if info.get('tasks'):
            checkin.update_tasks(state, info['tasks'], now=now)
            if info.get('title'):
                state['campaign_title'] = info['title']
            # 台账追溯：已经发出去的评论补记达成（打卡页统计有延迟）
            healed = checkin.heal_from_ledger(state, now=now)
            if healed:
                log.info('评论回复：台账追溯补记打卡项 %s', healed)
            summary = checkin.summary(state)
            checkin.set_result(state, summary['ok'], summary['line'], now=now)
            checkin.save_state(state)
            _say(progress, summary['line'])
            return summary
    except Exception as exc:                     # noqa: BLE001
        log.debug('刷新打卡状态失败（不影响回复）：%s', exc)
    return None
