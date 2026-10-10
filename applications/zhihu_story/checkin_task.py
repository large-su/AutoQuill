# -*- coding: utf-8 -*-
# ============================================================
# applications/zhihu_story/checkin_task.py
# 打卡互动落地：把 core/checkin 的决策变成页面上真实的关注 / 赞同
#
# 两条入口：
#   run_interaction(browser, question_url, ctx)  写草稿顺带做（主路径）
#   run_checkin_job(browser, progress)           晚间兜底巡检（独立任务）
#
# 实测行为（2026-09-27 真机，见 docs/CHECKIN-REPLY-PLAN.md 11.1）：
#   - 关注 / 取关 / 赞同 / 取消赞同 都是一次点击直接生效，没有确认弹窗；
#   - 所以「翻转」就是两次点击：先取消，再重新做一次；
#   - 关注按钮文案：关注 / 已关注 / 互相关注；赞同按钮：赞同 N / 已赞同 N。
#
# 本模块只做「DOM + 决策落地」，不排班、不管配额（那是 automation/）。
# ============================================================

import logging
import re
import time

from core import checkin

from applications.zhihu_story import comment_fallback

log = logging.getLogger(__name__)

KIND_LABEL = {'follow': '关注', 'vote': '赞同'}


def _say(progress, text):
    log.info('打卡互动：%s', text)
    if progress:
        try:
            progress(text)
        except Exception:                # noqa: BLE001
            pass


def pick_target(items, prefer_index=0):
    '''从回答条目里挑可操作的目标：优先参考回答（第一条），否则第一条带按钮的。

    items 来自 browser.list_interact_targets() 的 items 字段。
    '''
    items = list(items or [])
    for it in items:
        if it.get('index') == prefer_index and (it.get('has_follow') or it.get('has_vote')):
            return it
    for it in items:
        if it.get('has_follow') or it.get('has_vote'):
            return it
    return None


def apply_action(browser, kind, index, action, pause=2.0):
    '''在指定回答条目上执行 do / toggle，返回 {ok, detail}。

    toggle = 先取消再重做（取关→关注 / 取消赞同→赞同）——只在当天最后
    一个写草稿、且打卡仍未达成时才会走到这里。
    '''
    if action == 'do':
        r = (browser.set_follow(True, index) if kind == 'follow'
             else browser.set_vote(True, index))
        return {'ok': bool(r.get('ok')), 'detail': r.get('detail') or '',
                'result': r}
    if action == 'toggle':
        back = (browser.set_follow(False, index) if kind == 'follow'
                else browser.set_vote(False, index))
        if not back.get('ok'):
            return {'ok': False, 'detail': '取消失败：' + (back.get('detail') or ''),
                    'result': back}
        time.sleep(pause)
        again = (browser.set_follow(True, index) if kind == 'follow'
                 else browser.set_vote(True, index))
        return {'ok': bool(again.get('ok')),
                'detail': '翻转：' + (again.get('detail') or ''), 'result': again}
    return {'ok': False, 'detail': '未知动作：%s' % action}


def run_interaction(browser, question_url, ctx=None, state=None, now=None,
                    progress=None, ask=None):
    '''写草稿提取成功后调用：按上下文完成打卡互动（关注 / 赞同 / 评论兜底）。

    ctx: {'follow': 'do'|'toggle'|'done'|'skip', 'vote': 同上,
          'comment': 同上, 'is_last': bool,
          'story': {'title','text','url'} 可选 —— 参考故事，供评论兜底用}
    返回 {ok, handled, skipped, detail}；ctx 为空时立刻返回，零开销。

    自有故事评论成功检查一次后复用当日结果；评论仍未达成就尝试当前
    参考故事。关注/赞同的翻转仍只允许末班。
    '''
    ctx = dict(ctx or {})
    kinds = [k for k in ('follow', 'vote') if ctx.get(k) in ('do', 'toggle')]
    is_last = bool(ctx.get('is_last'))
    state = state if state is not None else checkin.load_state(now=now)
    story = dict(ctx.get('story') or {})
    if story.get('text') and story.get('url'):
        state['reference_story'] = {
            'title': str(story.get('title') or '')[:120],
            'text': str(story['text'])[:1200], 'url': story['url'],
        }
        checkin.save_state(state)
    if ctx.get('reader_check'):
        # 在当前工作流线程内使用已有浏览器，避免调度线程创建实例后跨线程使用。
        from applications.zhihu_story import reply_task
        try:
            result = reply_task.run_reply_job(
                browser, count=ctx.get('reader_count') or 1,
                dry_run=bool(ctx.get('comment_dry_run')), now=now,
                progress=progress)
            _say(progress, '自有故事评论检查：%s；%s'
                 % (result.get('detail') or '检查结束',
                    '当天已评论' if not checkin.needs(checkin.load_state(now=now), 'comment')
                    else '评论仍未达成，尝试已读到的参考故事'))
        except Exception as exc:                      # noqa: BLE001
            _say(progress, '自有故事评论检查失败（稍后可重试）：%s' % exc)
        state = checkin.load_state(now=now)
        # 评论管理页/打卡页导航会离开参考故事。成功回复也必须回到原页再做互动。
        try:
            _open_reference_story(browser, story.get('url') or question_url)
        except Exception as exc:                      # noqa: BLE001
            _say(progress, '回到参考故事失败：%s' % exc)
            return {'ok': False, 'handled': [], 'skipped': kinds + ['comment'],
                    'detail': '回到参考故事失败，后续读取时再补互动'}
    if not kinds:
        # 关注/赞同今天都达成了——但「发布评论」可能还欠着（用户 2026-09-29
        # 口径：回复读者评论会因「挑不出合适的评论」整体跳过，打卡就永远差一项）。
        # 所以这里不能直接返回，仍要给评论兜底一次机会。
        r = maybe_comment_fallback(browser, ctx=ctx, state=state, now=now,
                                   progress=progress, ask=ask)
        return {'ok': bool(r.get('ok') or r.get('skipped')),
                'handled': ['comment'] if r.get('sent') else [],
                'skipped': [] if r.get('sent') else kinds,
                'detail': r.get('detail') or '无需互动'}
    _say(progress, '本次要完成：%s%s'
         % ('、'.join(KIND_LABEL[k] for k in kinds),
            '（当天最后一班，允许翻转）' if is_last else ''))
    if question_url:
        try:
            _open_reference_story(browser, story.get('url') or question_url)
        except Exception as exc:                         # noqa: BLE001
            log.warning('打卡互动：打开问题页失败：%s', exc)
    info = browser.list_interact_targets() or {}
    target = pick_target(info.get('items') or [])
    if target is None:
        detail = '页面上没有可操作的回答条目（没有关注/赞同按钮）'
        _say(progress, detail)
        # 关注/赞同没有目标，不该连带阻断独立的评论入口。
        fb = maybe_comment_fallback(browser, ctx=ctx, state=state, now=now,
                                    progress=progress, ask=ask)
        return {'ok': False, 'handled': ['comment'] if fb.get('sent') else [],
                'skipped': kinds, 'detail': detail + '；' + fb.get('detail', '')}
    index = target.get('index') or 0
    _say(progress, '目标：%s（第 %s 条回答）'
         % (target.get('author') or '未知作者', index))
    handled, skipped, details = [], [], []
    for kind in kinds:
        action = checkin.target_action(kind, target, is_last=is_last)
        if action == 'skip':
            reason = '已是目标状态' if not is_last else '状态不可操作'
            checkin.mark_tried(state, kind, author=target.get('author'),
                               reason=reason)
            skipped.append(kind)
            details.append('%s：跳过（%s）' % (KIND_LABEL[kind], reason))
            continue
        r = apply_action(browser, kind, index, action)
        if r.get('ok'):
            checkin.mark_done(state, kind, detail='%s %s'
                              % (KIND_LABEL[kind], target.get('author') or ''), now=now)
            handled.append(kind)
            details.append('%s：%s'
                           % (KIND_LABEL[kind],
                              '完成' if action == 'do' else '翻转完成'))
        else:
            checkin.mark_tried(state, kind, author=target.get('author'),
                               reason=r.get('detail') or '失败')
            skipped.append(kind)
            details.append('%s：失败（%s）' % (KIND_LABEL[kind], r.get('detail') or ''))
        _say(progress, details[-1])
    checkin.save_state(state)
    # 关注/赞同做完后，评论仍欠着就尝试当前故事，失败留给下一次读取。
    fb = maybe_comment_fallback(browser, ctx=ctx, state=state, now=now,
                                progress=progress, ask=ask)
    if fb.get('sent'):
        handled.append('comment')
        details.append(fb.get('detail') or '评论兜底已发送')
    elif not fb.get('skipped'):
        skipped.append('comment')
        details.append(fb.get('detail') or '评论兜底未完成')
    checkin.save_state(state)
    return {'ok': bool(handled) or not skipped, 'handled': handled,
            'skipped': skipped, 'detail': '；'.join(details) or '完成'}


# ------------------------------------------------------------
# 打卡评论兜底（每次读取参考故事都可补做）
# ------------------------------------------------------------

def maybe_comment_fallback(browser, ctx=None, state=None, now=None,
                           progress=None, ask=None, dry_run=False):
    own_ask = ask is None
    compose = _default_ask(browser) if own_ask else ask
    try:
        return _maybe_comment_fallback_impl(
            browser, ctx=ctx, state=state, now=now, progress=progress,
            ask=compose, dry_run=dry_run)
    finally:
        if own_ask:
            close = getattr(compose, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception:                   # noqa: BLE001
                    pass


def _maybe_comment_fallback_impl(browser, ctx=None, state=None, now=None,
                                 progress=None, ask=None, dry_run=False):
    '''当天「发布评论」还没达成时，在参考故事下补一条贴题评论。

    触发条件：自动化上下文要求补评论、当天评论未达成、已读到参考故事。
    失败不记完成，下一次读故事继续尝试；真实发出后当天不再补做。
    ★ 刻意不做的事：不在此处读打卡页。打卡快照由 `_refresh_checkin_after_reply`
      与独立巡检任务刷新；这里读的是**已保存的当日进度**，省一次导航。

    返回 {ok, sent, skipped, detail}；不满足条件时 skipped=True 且零开销。
    '''
    ctx = dict(ctx or {})
    if not (ctx.get('is_last') or ctx.get('reader_check')
            or ctx.get('comment') in ('do', 'toggle')):
        return {'ok': True, 'sent': False, 'skipped': True,
                'detail': '本次没有补评论的自动化上下文'}
    state = state if state is not None else checkin.load_state(now=now)
    if checkin.heal_from_ledger(state, now=now):
        checkin.save_state(state)
    if not checkin.needs(state, 'comment'):
        return {'ok': True, 'sent': False, 'skipped': True,
                'detail': '今天的评论打卡已达成'}
    story = dict(ctx.get('story') or {})
    text_src = story.get('text') or ''
    url = story.get('url') or ''
    if not (text_src and url):
        return {'ok': True, 'sent': False, 'skipped': True,
                'detail': '没拿到参考故事正文，跳过评论兜底'}
    try:
        _open_reference_story(browser, url)
    except Exception as exc:                          # noqa: BLE001
        log.warning('评论兜底：回到参考回答页失败：%s', exc)
        detail = '评论兜底：打开参考故事失败，后续读取时重试'
        checkin.mark_tried(state, 'comment', reason=detail, now=now)
        checkin.save_state(state)
        return {'ok': False, 'sent': False, 'skipped': False, 'detail': detail}
    already = _reply_texts_today(now=now)             # 今天已发过的评论（去重用）
    got = comment_fallback.compose(
        ask, story.get('title') or '', text_src,
        avoid_texts=already, progress=lambda t: _say(progress, t))
    if not got.get('ok'):
        detail = '评论兜底：生成不达标（%s），未发送' % '；'.join(got.get('issues') or [])
        checkin.mark_tried(state, 'comment', reason=detail, now=now)
        checkin.save_state(state)
        _say(progress, detail)
        return {'ok': False, 'sent': False, 'skipped': False, 'detail': detail}
    comment = got['comment']
    page = getattr(browser, 'page', None)
    log.info('评论兜底：目标 %s；当前页 %s', url, getattr(page, 'url', '未知'))
    _say(progress, '评论兜底：将在参考故事下评论：%s' % comment[:40])
    dry_run = bool(dry_run or ctx.get('comment_dry_run'))
    try:
        r = browser.send_answer_comment(comment, dry_run=dry_run)
    except Exception as exc:                          # noqa: BLE001
        log.warning('评论兜底：发送异常：%s', exc)
        r = {'ok': False, 'sent': False, 'detail': str(exc)}
    if r.get('sent') and not dry_run:
        checkin.mark_done(state, 'comment',
                          detail='评论兜底：%s' % comment[:30], now=now)
        checkin.append_reply({
            'key': '', 'author': '', 'comment': '',
            'answer_url': url, 'reply': comment, 'issues': [],
            'dry_run': bool(dry_run), 'sent': True,
            'attempted': True, 'source': 'checkin-fallback',
            'send_detail': r.get('detail') or '',
            'at': checkin.now_str(now),
        })
        checkin.save_state(state)
        return {'ok': True, 'sent': True, 'skipped': False,
                'detail': '评论兜底已发送：%s' % comment[:40]}
    if dry_run and r.get('ok'):
        return {'ok': True, 'sent': False, 'skipped': True,
                'detail': '评论兜底演练完成（未发送，评论打卡仍未达成）'}
    checkin.mark_tried(state, 'comment',
                       reason=(r.get('detail') or '发送失败'), now=now)
    checkin.save_state(state)
    return {'ok': False, 'sent': False, 'skipped': False,
            'detail': '评论兜底未发出：%s；后续读取故事时继续补做'
                      % (r.get('detail') or '未知原因')}


def _open_reference_story(browser, url):
    '''回答链接原样导航，避免 open_question 把回答 ID 丢掉。'''
    if re.search(r'/answer/\d+', str(url or '')):
        if browser.page.url != url:
            browser.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        browser._settle_answer_page(timeout=15)
    else:
        browser.open_question(url)


def _default_ask(browser):
    '''默认提问通道：惰性创建本兜底独立驱动，并附带 close。'''
    holder = {'driver': None}

    def close():
        driver = holder['driver']
        if driver is None:
            return
        try:
            driver.delete_current_session()
        except Exception:                           # noqa: BLE001
            pass
        try:
            driver.close_session()
        except Exception:                           # noqa: BLE001
            pass
        holder['driver'] = None

    def ask(prompt, reuse_session=True):
        from applications.zhihu_story import reply_task
        from config import LLM_MODE
        if LLM_MODE != 'api' and holder['driver'] is None:
            from web_drivers import create_driver
            holder['driver'] = create_driver()
        return reply_task.ask_llm(prompt, driver=holder['driver'],
                                  reuse_session=reuse_session)
    ask.close = close
    return ask


def _reply_texts_today(now=None):
    '''今天**真的发出去过**的评论文本（供兜底去重，避免又发一条差不多的）。'''
    out = []
    try:
        for r in checkin.replies_today(now=now):
            if r.get('sent') and r.get('reply'):
                out.append(str(r['reply']))
    except Exception as exc:                  # noqa: BLE001 台账读不到不该阻断
        log.debug('读今日评论台账失败（不影响兜底）：%s', exc)
    return out


def _first_recommend_question(browser, max_cards=6):
    '''兜底巡检用：从推荐问题页取一个可写的问题 URL（取不到返回空串）。'''
    try:
        browser.open_recommend_page()
        qs = browser.get_recommend_questions(max_cards=max_cards) or []
        for q in qs:
            if q.get('href'):
                return q['href']
    except Exception as exc:                             # noqa: BLE001
        log.warning('打卡巡检：取推荐问题失败：%s', exc)
    return ''


def run_checkin_job(browser, url='', progress=None, now=None, is_last=True,
                    question_url='', comment_ctx=None, ask=None):
    '''晚间兜底巡检：刷新打卡状态；仍未达成时自己找个目标补做。

    is_last=True：巡检发生在当天最后一班之后，允许翻转。
    返回 {ok, units, detail, summary}。
    '''
    state = checkin.load_state(now=now)
    campaign = url or state.get('campaign_url') or ''
    if not campaign:
        _say(progress, '不知道当期打卡页，去创作中心首页找「去打卡」…')
        found = browser.discover_campaign_url()
        if not found.get('ok'):
            return {'ok': False, 'units': 0,
                    'detail': found.get('detail') or '找不到当期打卡页',
                    'summary': checkin.summary(state)}
        campaign = found['url']
        checkin.set_campaign(state, campaign, found.get('text') or '')
    _say(progress, '读取打卡页：%s' % campaign)
    info = browser.read_checkin_tasks(campaign)
    tasks = info.get('tasks') or {}
    if not tasks:
        return {'ok': False, 'units': 0,
                'detail': '打卡页没解析到今日任务（可能改版或未登录）',
                'summary': checkin.summary(state)}
    checkin.update_tasks(state, tasks, now=now)
    checkin.heal_from_ledger(state, now=now)
    if info.get('title'):
        state['campaign_title'] = info['title']
    need = [k for k in ('follow', 'vote', 'comment') if checkin.needs(state, k)]
    summary = checkin.summary(state)
    _say(progress, summary['line'])
    if not need:
        checkin.set_result(state, summary['ok'], summary['line'], now=now)
        checkin.save_state(state)
        return {'ok': True, 'units': 0, 'detail': summary['line'],
                'summary': summary}
    ctx = dict(comment_ctx or {})
    ctx.update({k: checkin.decide(state, k, is_last=is_last)
                for k in ('follow', 'vote', 'comment')})
    ctx['is_last'] = is_last
    ctx['story'] = state.get('reference_story') or {}
    q = question_url or ctx['story'].get('url')
    if not q and any(k in need for k in ('follow', 'vote')):
        q = _first_recommend_question(browser)
    if not q:
        detail = '评论仍未达成，尚无已读取的参考故事；后续读取时补做'
        checkin.set_result(state, False, detail, now=now)
        checkin.save_state(state)
        return {'ok': False, 'units': 0,
                'incomplete': True,
                'detail': detail,
                'summary': summary}
    r = run_interaction(browser, q, ctx=ctx, state=state, now=now,
                        progress=progress, ask=ask)
    state = checkin.load_state(now=now)
    summary = checkin.summary(state)
    checkin.set_result(state, summary['ok'], summary['line'], now=now)
    checkin.save_state(state)
    return {'ok': summary['ok'], 'units': len(r.get('handled') or []),
            'incomplete': not summary['ok'],
            'detail': (r.get('detail') or '') + '；' + summary['line'],
            'summary': summary}
