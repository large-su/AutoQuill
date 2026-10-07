# -*- coding: utf-8 -*-
# ============================================================
# applications/zhihu_story/browser_interact.py
# 互动 DOM 原语：打卡挑战（关注 / 赞同）+ 评论采集与回复
#
# 真机探针结论（2026-09-27 实测，详见 docs/CHECKIN-REPLY-PLAN.md 第 11 节）：
#   - 关注 / 取关：button.FollowButton，一次点击直接生效，无确认弹窗；
#   - 赞同 / 取消赞同：button.VoteButton，一次点击直接生效，无确认弹窗；
#   - 打卡页今日任务：div.tasktree-item（-top 是分组容器，必须排除），
#     按钮文案 = 已完成 / 去发布 / 去提问 / 去评论 / 去收听；
#   - 当期入口：创作中心首页 https://www.zhihu.com/creator 的「去打卡」链接
#     （每期换 campaign id，靠这个自动跟上，不用手工改配置）；
#   - 评论管理页：div.CommentManage-CommentCard（首屏 20 张），
#     作者 / 回答链接 / 正文 / 时间都能读到；页面没有「未回复」筛选，
#     卡片上也没有已回复标记 —— 防重复回复靠台账 + 回答页核实。
#
# 分层：本模块只做 DOM（读状态 + 点击 + 采集），业务决策在 core/checkin.py；
# 所有页面交互走 self._safe_evaluate（browser_pool 的有界等待），
# 绝不出现裸 page.evaluate（tests/test_browser_adapter.py 有源码锚点守护）。
# ============================================================

import logging
import re
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

CREATOR_HOME_URL = 'https://www.zhihu.com/creator'
COMMENT_MANAGE_URL = 'https://www.zhihu.com/creator/manage/comment/answer'
# 当期打卡页 URL 的缓存位置：data/state/checkin.json（由 core/checkin.py 读写）

# 关注按钮三态 → 语义（探针实测：关注 / 已关注 / 互相关注）
FOLLOWED_TEXTS = ('已关注', '互相关注')
NOT_FOLLOWED_TEXTS = ('关注', '+ 关注')
VOTED_PREFIX = '已赞同'
NOT_VOTED_PREFIX = '赞同'

_ZW_CHARS = ('\u200b', '\u200c', '\u200d', '\ufeff')
_ZW_RE = re.compile('[' + ''.join(_ZW_CHARS) + ']')


def normalize_button_text(text):
    '''剥离零宽字符与首尾空白（知乎按钮文本常带 ​\n，trim 去不掉）。'''
    return _ZW_RE.sub('', str(text or '')).strip()


_WS_RE = re.compile(r'\s+')


def flat_text(text):
    '''零宽字符 + 所有空白全部去掉（与页面里 JS 的 flat() 同一口径）。

    评论卡片正文里换行/空格数量两边常常不一致（DOM 重排后更明显），
    用「去空白后包含」比「原样包含」稳得多。'''
    return _WS_RE.sub('', _ZW_RE.sub('', str(text or '')))


def follow_state(text):
    '''按钮文案 → followed / none / unknown。'''
    t = normalize_button_text(text)
    if not t:
        return 'unknown'
    if t in FOLLOWED_TEXTS:
        return 'followed'
    if t in NOT_FOLLOWED_TEXTS:
        return 'none'
    return 'unknown'


def vote_state(text):
    '''赞同按钮文案 → voted / none / unknown（已赞同 747 / 赞同 746）。'''
    t = normalize_button_text(text)
    if not t:
        return 'unknown'
    if t.startswith(VOTED_PREFIX):
        return 'voted'
    if t.startswith(NOT_VOTED_PREFIX):
        return 'none'
    return 'unknown'


# 打卡任务标题 → 稳定 key（标题里的数字会变，用关键词匹配）
TASK_KEY_PATTERNS = (
    ('follow', ('关注',)),
    ('vote', ('赞同',)),
    ('comment', ('评论',)),
    ('answer', ('回答',)),
    ('question', ('提问', '问题')),
    ('pin', ('想法',)),
    ('morning_call', ('收听', 'Morning Call')),
)

# 打卡页上「已完成」的按钮文案（其余（去发布/去评论…）= 未完成）
DONE_TEXTS = ('已完成', '已完成')


def checkin_task_key(title):
    '''打卡任务标题 → 稳定 key；识别不了返回空串。'''
    t = normalize_button_text(title)
    for key, kws in TASK_KEY_PATTERNS:
        for kw in kws:
            if kw in t:
                return key
    return ''


def parse_checkin_tasks(raw_tasks):
    '''打卡页原始条目 → {key: {title, desc, action, done}}（纯逻辑，可单测）。

    raw_tasks: [{'title','desc','action'}]，来自 _CHECKIN_TASKS_JS。
    '''
    out = {}
    for it in raw_tasks or []:
        if not isinstance(it, dict):
            continue
        title = normalize_button_text(it.get('title'))
        if not title:
            continue
        key = checkin_task_key(title)
        if not key:
            continue
        action = normalize_button_text(it.get('action'))
        out[key] = {
            'title': title,
            'desc': normalize_button_text(it.get('desc')),
            'action': action,
            'done': action in DONE_TEXTS,
        }
    return out


_TIME_HM = re.compile(r'(\d{1,2}):(\d{2})')
_TIME_MD = re.compile(r'(\d{1,2})-(\d{1,2})')
_TIME_YMD = re.compile(r'(\d{4})-(\d{1,2})-(\d{1,2})')
_AGO = re.compile(r'(\d+)\s*(分钟|小时|天)前')


def parse_comment_time(text, now=None):
    '''评论时间文案 → datetime（解析不了返回 None，调用方按「未知」处理）。

    实测格式：09-26 13:32 / 昨天 13:32 / 今天 13:32 / 20 小时前 / 3 天前。
    不做「7 天内」硬过滤（用户 2026-09-27 口径：以首屏 20 条为准），
    时间只用于展示与排序。
    '''
    t = normalize_button_text(text)
    if not t:
        return None
    now = now or datetime.now()
    m = _AGO.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == '分钟':
            return now - timedelta(minutes=n)
        if unit == '小时':
            return now - timedelta(hours=n)
        return now - timedelta(days=n)
    ymd = _TIME_YMD.search(t)
    hm = _TIME_HM.search(t)
    hour = int(hm.group(1)) if hm else 0
    minute = int(hm.group(2)) if hm else 0
    if ymd:
        try:
            return datetime(int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3)), hour, minute)
        except ValueError:
            return None
    md = _TIME_MD.search(t)
    if md:
        month, day = int(md.group(1)), int(md.group(2))
        year = now.year
        try:
            cand = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if cand - now > timedelta(days=1):      # 跨年（如 12-30 看 01-02）
            try:
                cand = cand.replace(year=year - 1)
            except ValueError:
                pass
        return cand
    if '昨天' in t:
        base = now - timedelta(days=1)
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if '今天' in t or '刚刚' in t:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0) if hm else now
    return None


def normalize_answer_url(href):
    '''回答链接 → 绝对 URL（评论卡片里的 href 可能是 // 或 / 开头）。'''
    h = str(href or '').strip()
    if h.startswith('//'):
        return 'https:' + h
    if h.startswith('/'):
        return 'https://www.zhihu.com' + h
    return h


def comment_key(answer_url, author, text):
    '''评论去重键：回答 + 作者 + 正文归一化后取短哈希。'''
    import hashlib
    raw = '|'.join([normalize_answer_url(answer_url),
                    normalize_button_text(author),
                    re.sub(r'\s+', '', str(text or ''))])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def parse_manage_card(raw, now=None):
    '''评论管理页卡片 → 归一化 dict（纯逻辑，可单测）。'''
    card = raw or {}
    lines = [normalize_button_text(x) for x in (card.get('lines') or [])]
    lines = [x for x in lines if x]
    time_text = ''
    for x in lines:
        if parse_comment_time(x, now=now) is not None and not x.startswith('《'):
            time_text = x
            break
    text = normalize_button_text(card.get('text'))
    if not text:                        # 兜底：正文 = 时间行之后、按钮行之前那段
        skip = ('喜欢', '回复', '推荐', '评论了你的回答')
        body = [x for x in lines if x not in skip and not x.startswith('《')
                and x != time_text and x != normalize_button_text(card.get('author'))]
        text = ' '.join(body[:1])
    answer_url = normalize_answer_url(card.get('answer_href'))
    author = normalize_button_text(card.get('author'))
    return {
        'index': int(card.get('index') or 0),
        'author': author,
        'author_href': normalize_answer_url(card.get('author_href')),
        'answer_url': answer_url,
        'answer_title': normalize_button_text(card.get('answer_title')),
        'time_text': time_text,
        'time': parse_comment_time(time_text, now=now),
        'text': text,
        'key': comment_key(answer_url, author, text),
    }


# ------------------------------------------------------------
# 页面 JS（零宽字符清理 + 元素工具的统一前奏）
# ------------------------------------------------------------
_ZW_HELPERS = '''
  const ZW = String.fromCharCode(8203,8204,8205,65279);
  const zwRe = new RegExp('[' + ZW + ']', 'g');
  const clean = s => (s || '').replace(zwRe, ' ').replace(/[ ]+/g, ' ').trim();
  // flat：零宽字符 + 所有空白（含换行）全部去掉——知乎按钮文本常是
  // 「零宽字符 + 换行 + 回复」，用 clean 比不出「回复」（注释里不要写转义字符，
  // Python 会把反斜杠转义先解释掉，把 JS 注释截断成代码——真机踩过）
  const wsRe = new RegExp('[' + String.fromCharCode(9, 10, 13, 32) + ']', 'g');
  const flat = s => (s || '').split(ZW).join('').replace(wsRe, '');
  const txt = el => clean((el && (el.innerText || el.textContent)) || '');
  const cls = el => (el && typeof el.className === 'string' ? el.className : '');
  const answerItems = () => {
    const a = Array.from(document.querySelectorAll('.AnswerItem'));
    return a.length ? a : Array.from(document.querySelectorAll('.QuestionAnswer-content'));
  };
  const answerIdOf = url => { const m = (url || '').match(/answer[/]([0-9]+)/); return m ? m[1] : ''; };
  // shown：元素**真的能被点到**（几何可见性）。
  // ★ 不能用 offsetParent !== null：知乎回答页大量用 position:fixed 的吸顶/悬浮栏，
  //   而 fixed 元素的 offsetParent 恒为 null —— 真机实测「添加评论」按钮所在的操作栏
  //   在 .AnswerItem 下 offsetParent 为 null（2026-09-29，把过滤条件改成几何判断才对）。
  const shown = e => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
'''


def _js(arg, body):
    '''把参数名 + 公共前奏 + 函数体拼成一个 JS 函数表达式（供 evaluate 调用）。'''
    return '(' + arg + ') => {' + _ZW_HELPERS + body + '}'


# 创作中心首页 → 当期打卡页 URL（「去打卡」链接；退化取第一个 campaign 链接）
_CAMPAIGN_ENTRY_JS = _js('', '''
  const links = Array.from(document.querySelectorAll('a[href*="/parker/campaign/"]'))
      .map(a => ({ text: txt(a), href: a.getAttribute('href') || '' }))
      .filter(l => l.href);
  const hit = links.find(l => l.text.indexOf('打卡') >= 0) || links[0];
  return { ok: !!hit, url: hit ? hit.href : '', text: hit ? hit.text : '',
           candidates: links.slice(0, 5) };
''')

# 打卡页「今日任务」：分组容器 tasktree-item-top 必须排除
_CHECKIN_TASKS_JS = _js('', '''
  const nodes = Array.from(document.querySelectorAll('[class*=tasktree-item]'))
      .filter(el => cls(el).indexOf('tasktree-item-top') < 0);
  const tasks = nodes.map(it => ({
    title: txt(it.querySelector('[class*=tasktree-task-title], [class*=tasktree-title]')),
    desc: txt(it.querySelector('[class*=tasktree-desc]')),
    action: txt(it.querySelector('[class*=tasktree-btn]'))
  })).filter(t => t.title);
  const groups = Array.from(document.querySelectorAll('[class*=tasktree-item-top]'))
      .map(el => txt(el)).filter(t => t);
  const body = txt(document.body);
  return { ok: true, url: location.href, title: document.title, tasks: tasks,
           groups: groups.slice(0, 8), body_head: body.slice(0, 400) };
''')

# 回答页：列出各回答条目的作者与关注/赞同状态（只读）
_LIST_TARGETS_JS = _js('', '''
  const items = answerItems();
  const out = items.map((it, i) => {
    const named = Array.from(it.querySelectorAll('a[href*="/people/"]')).filter(a => txt(a).length > 0);
    const f = it.querySelector('.FollowButton');
    const v = it.querySelector('.VoteButton');
    const ans = it.querySelector('a[href*="/answer/"]');
    return { index: i,
             author: named.length ? txt(named[0]).slice(0, 30) : '',
             author_href: named.length ? (named[0].getAttribute('href') || '') : '',
             follow_text: f ? txt(f) : '',
             has_follow: !!f,
             vote_text: v ? txt(v) : '',
             has_vote: !!v,
             vote_disabled: v ? !!v.disabled : null,
             answer_href: ans ? (ans.getAttribute('href') || '') : '' };
  });
  return { ok: true, url: location.href, items: out };
''')

# 在指定回答条目内点「关注」或「赞同」按钮
_CLICK_INTERACT_JS = _js('arg', '''
  const items = answerItems();
  const it = items[arg.index];
  if (!it) return { ok: false, reason: 'no-item' };
  const sel = arg.kind === 'follow' ? '.FollowButton' : '.VoteButton';
  const btn = it.querySelector(sel);
  if (!btn) return { ok: false, reason: 'no-button:' + sel };
  const before = txt(btn);
  btn.click();
  return { ok: true, kind: arg.kind, before: before };
''')

# 评论管理页卡片内的「回复」：按评论正文定位卡片 → 点它的「回复」按钮
#   —— 2026-09-27：评论就是从管理页挑的，在这里回复最稳（回答页只渲染前 N 条，
#      评论一多就「找不到这条评论」）。
_MANAGE_CARD_REPLY_JS = _js('target', '''
  const want = flat(target);
  const cards = Array.from(document.querySelectorAll('.CommentManage-CommentCard'));
  let card = null;
  for (let i = 0; i < cards.length; i++) {
    if (flat(txt(cards[i])).indexOf(want) >= 0) { card = cards[i]; break; }
  }
  if (!card) return { ok: false, reason: 'no-card', cards: cards.length };
  const btns = Array.from(card.querySelectorAll('button'));
  const btn = btns.find(b => flat(txt(b)) === '回复');
  if (!btn) return { ok: false, reason: 'no-reply-button' };
  btn.click();
  return { ok: true, card: txt(card).slice(0, 60) };
''')

# 回复编辑器状态（管理页与回答页共用同一套 Draft 编辑器 + 「发布」按钮）
_REPLY_EDITOR_STATE_JS = _js('', '''
  const eds = Array.from(document.querySelectorAll('div.public-DraftEditor-content,[contenteditable=true],textarea'))
      .filter(e => e.offsetParent);
  const active = eds.find(e => document.activeElement === e || e.contains(document.activeElement));
  const ed = active || eds[eds.length - 1] || null;
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetParent && flat(txt(b)) === '发布');
  const pub = pubs[pubs.length - 1] || null;
  return { has_editor: !!ed, focused: !!active, text: ed ? txt(ed) : '',
           publish_found: !!pub, publish_disabled: pub ? !!pub.disabled : null };
''')

# 点「发布」（只在按钮可用时点）
_CLICK_MANAGE_PUBLISH_JS = _js('', '''
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetParent && flat(txt(b)) === '发布');
  const pub = pubs[pubs.length - 1];
  if (!pub) return { ok: false, reason: 'no-publish-button' };
  if (pub.disabled) return { ok: false, reason: 'publish-disabled' };
  pub.click();
  return { ok: true };
''')

# 页面上的报错/风控文案：发布被服务端拒绝时唯一能读到的证据
#   —— 2026-09-28：回复「点了发布但没落地」时必须能区分
#      「服务端拒绝了（可以换路径重试）」与「其实已经发出去了（绝不能重发）」。
_REPLY_ERROR_JS = _js('', '''
  const vis = e => e.offsetParent !== null;
  const hits = [];
  const nodes = Array.from(document.querySelectorAll('div,span,p,li'));
  for (let i = 0; i < nodes.length; i++) {
    const e = nodes[i];
    if (!vis(e)) continue;
    const t = clean(e.innerText);
    if (!t || t.length > 100) continue;
    if (/发送失败|发布失败|操作失败|操作频繁|系统繁忙|请稍后再试|内容违规|涉嫌违规|无法发布|不能发布|回复失败|账号异常/.test(t)) {
      hits.push(t);
      if (hits.length >= 3) break;
    }
  }
  return hits;
''')

# 评论管理页：首屏卡片采集（作者 / 回答 / 正文 / 时间行）
_MANAGE_COMMENTS_JS = _js('limit', '''
  const cards = Array.from(document.querySelectorAll('.CommentManage-CommentCard'));
  const out = cards.slice(0, limit).map((c, i) => {
    const author = Array.from(c.querySelectorAll('a.UserLink-link[href*="/people/"]'))
        .filter(a => txt(a).length > 0)[0];
    const ans = c.querySelector('a[href*="/answer/"]');
    const content = c.querySelector('.CommentRichText .RichText') || c.querySelector('.CommentRichText');
    const lines = ((c.innerText || '')).split(String.fromCharCode(10)).map(s => clean(s)).filter(s => s.length > 0);
    return { index: i,
             author: author ? txt(author).slice(0, 30) : '',
             author_href: author ? (author.getAttribute('href') || '') : '',
             answer_href: ans ? (ans.getAttribute('href') || '') : '',
             answer_title: ans ? txt(ans).slice(0, 80) : '',
             text: content ? txt(content).slice(0, 500) : '',
             lines: lines.slice(0, 12) };
  });
  return { ok: true, count: cards.length, cards: out };
''')

# 评论区的「加载更多」：滚动评论容器 + 点「更多/全部/展开」控件（最多几轮）
#   —— 2026-09-27 真机发现：回答页只加载前 N 条评论，目标评论若不在首批里，
#      回复链路就会「找不到这条评论」。这里把加载做足（有界，不做无限滚动）。
_LOAD_MORE_COMMENTS_JS = _js('', '''
  const scope = document.querySelector('.Comments-container, .CommentList, [class*=Comments-container], [class*=CommentList]') || document;
  let clicked = '';
  const all = scope.querySelectorAll('div,span,a,button');
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (!el.offsetParent) continue;
    const t = txt(el);
    if (!t || t.length > 20) continue;
    if (t.indexOf('更多') >= 0 || t.indexOf('全部') >= 0 || t.indexOf('展开') >= 0) {
      el.click(); clicked = t; break;
    }
  }
  const box = document.querySelector('.Comments-container, .CommentList, [class*=Comments-container], [class*=CommentList]');
  if (box) box.scrollTop = box.scrollHeight;
  window.scrollBy(0, 900);
  return clicked;
''')

# 回答页评论区：每条评论正文 + 它下面有没有嵌套回复（防重复回复的真相来源）
# ★ 嵌套回复的 DOM 尚未用真实回复校准过（见 docs/CHECKIN-REPLY-PLAN.md 11.4），
#   这里只做「有嵌套就报出来」的保守判断，拿到第一条真实回复后立刻校准。
_ANSWER_COMMENTS_JS = _js('', '''
  const contents = Array.from(document.querySelectorAll('.CommentContent'));
  const out = contents.map((cc, i) => {
    let cur = cc.parentElement;
    let holder = null;
    for (let k = 0; k < 5 && cur; k++) {
      const hasReply = Array.from(cur.querySelectorAll('button')).some(b => txt(b) === '回复');
      if (hasReply) { holder = cur; break; }
      cur = cur.parentElement;
    }
    let nested = 0;
    let people = [];
    if (holder) {
      nested = holder.querySelectorAll('.CommentContent').length - 1;
      people = Array.from(holder.querySelectorAll('a[href*="/people/"]')).map(a => txt(a)).filter(t => t);
      const asker = holder.querySelector('a[href*="/people/"]');
      people = [asker ? txt(asker) : ''].concat(people.filter(t => t !== (asker ? txt(asker) : '')));
    }
    return { index: i, text: txt(cc).slice(0, 300), nested_replies: nested > 0 ? nested : 0,
             people: people.slice(0, 6) };
  });
  return { ok: true, url: location.href, count: contents.length, comments: out };
''')


class InteractMixin:
    '''互动 DOM 通道：打卡页解析 + 关注/赞同 + 评论采集。'''

    # ---------------- 打卡页 ----------------

    def discover_campaign_url(self, timeout=40000):
        '''创作中心首页 → 当期打卡页 URL（每期 campaign id 会变，这里自动跟上）。'''
        self.page.goto(CREATOR_HOME_URL, wait_until='domcontentloaded',
                       timeout=timeout)
        time.sleep(4)
        info = self._safe_evaluate(_CAMPAIGN_ENTRY_JS) or {}
        url = normalize_answer_url((info or {}).get('url'))
        if not url:
            return {'ok': False, 'url': '', 'detail': '创作中心首页没找到「去打卡」链接'}
        return {'ok': True, 'url': url, 'text': (info or {}).get('text') or '',
                'candidates': (info or {}).get('candidates') or []}

    def read_checkin_tasks(self, url, timeout=45000, wait=8):
        '''打开打卡页 → 解析今日任务状态（返回原始 tasks + 归一化 tasks_by_key）。'''
        self.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        time.sleep(wait)
        raw = self._safe_evaluate(_CHECKIN_TASKS_JS) or {}
        tasks = parse_checkin_tasks((raw or {}).get('tasks'))
        return {'ok': bool(tasks), 'url': (raw or {}).get('url') or url,
                'title': (raw or {}).get('title') or '',
                'tasks': tasks, 'groups': (raw or {}).get('groups') or [],
                'raw_tasks': (raw or {}).get('tasks') or []}

    # ---------------- 回答页关注 / 赞同 ----------------

    def list_interact_targets(self):
        '''当前回答页上各条回答的作者与关注/赞同状态（只读，不点）。'''
        return self._safe_evaluate(_LIST_TARGETS_JS) or {'ok': False, 'items': []}

    def click_interact(self, kind, index=0):
        '''点指定回答条目的「关注」/「赞同」按钮（kind: follow / vote）。'''
        if kind not in ('follow', 'vote'):
            raise ValueError('kind 必须是 follow 或 vote：%r' % (kind,))
        r = self._safe_evaluate(_CLICK_INTERACT_JS, {'index': int(index), 'kind': kind})
        return r or {'ok': False, 'reason': 'evaluate-failed'}

    def set_follow(self, want=True, index=0, tries=2, pause=2.5):
        '''把「关注」设成想要的状态（幂等：已经是目标状态就直接返回）。

        实测：一次点击即生效，无确认弹窗；关注/取关都是同一个按钮。
        返回 {ok, changed, before, after, detail}。
        '''
        return self._set_interact('follow', want, index, tries, pause)

    def set_vote(self, want=True, index=0, tries=2, pause=2.5):
        '''把「赞同」设成想要的状态（幂等）。实测一次点击即生效/取消。'''
        return self._set_interact('vote', want, index, tries, pause)

    def _set_interact(self, kind, want, index, tries, pause):
        state_of = follow_state if kind == 'follow' else vote_state
        want_state = ('followed' if want else 'none') if kind == 'follow' \
            else ('voted' if want else 'none')
        last = {'ok': False, 'changed': False, 'before': '', 'after': '',
                'detail': '未执行'}
        for attempt in range(max(1, int(tries))):
            info = self.list_interact_targets()
            items = (info or {}).get('items') or []
            target = next((x for x in items if x.get('index') == int(index)), None)
            if target is None:
                return {'ok': False, 'changed': False, 'before': '', 'after': '',
                        'detail': '页面上找不到第 %d 条回答' % int(index)}
            key = 'follow_text' if kind == 'follow' else 'vote_text'
            before_text = target.get(key) or ''
            cur = state_of(before_text)
            if cur == want_state:
                return {'ok': True, 'changed': False, 'before': before_text,
                        'after': before_text, 'detail': '已是目标状态，跳过'}
            if cur == 'unknown':
                return {'ok': False, 'changed': False, 'before': before_text,
                        'after': before_text,
                        'detail': '按钮状态无法识别：%r' % before_text}
            clicked = self.click_interact(kind, index)
            if not clicked.get('ok'):
                last = {'ok': False, 'changed': False, 'before': before_text,
                        'after': before_text,
                        'detail': clicked.get('reason') or '点击失败'}
                time.sleep(pause)
                continue
            time.sleep(pause)
            after_info = self.list_interact_targets()
            after_items = (after_info or {}).get('items') or []
            after_target = next((x for x in after_items if x.get('index') == int(index)), None)
            after_text = (after_target or {}).get(key) or ''
            if state_of(after_text) == want_state:
                return {'ok': True, 'changed': True, 'before': before_text,
                        'after': after_text, 'detail': '已生效'}
            last = {'ok': False, 'changed': False, 'before': before_text,
                    'after': after_text,
                    'detail': '点了但状态没变（%s → %s）' % (before_text, after_text)}
            time.sleep(pause)
        return last

    # ---------------- 管理页回复（推荐路径，2026-09-27 新增）----------------

    def _ensure_manage_page(self, wait=6):
        '''确保当前在评论管理页（回复从这里发起：评论就是从这儿挑的、必然在场）。'''
        if 'creator/manage/comment' in (self.page.url or ''):
            return True
        try:
            self.page.goto(COMMENT_MANAGE_URL, wait_until='domcontentloaded',
                           timeout=45000)
            time.sleep(wait)
            return True
        except Exception as exc:            # noqa: BLE001
            log.warning('打开评论管理页失败：%s', exc)
            return False

    def open_manage_reply_editor(self, comment_text, pause=2.5, ensure_page=True):
        '''在管理页找到该评论所在卡片，点它的「回复」并确认编辑器出现。'''
        if ensure_page and not self._ensure_manage_page():
            return {'ok': False, 'editor': {}, 'detail': '打不开评论管理页'}
        r = self._safe_evaluate(_MANAGE_CARD_REPLY_JS, comment_text) or {}
        if not r.get('ok'):
            return {'ok': False, 'editor': {},
                    'detail': r.get('reason') or '找不到该评论的卡片'}
        time.sleep(pause)
        state = self._safe_evaluate(_REPLY_EDITOR_STATE_JS) or {}
        if not state.get('has_editor'):
            return {'ok': False, 'editor': state, 'detail': '点了回复但没出现编辑器'}
        return {'ok': True, 'editor': state, 'detail': ''}

    def _type_into_reply_editor(self, text, ready_timeout=10):
        '''往已打开的编辑器输入正文，等「发布」变为可用（发送就绪判据）。'''
        time.sleep(0.4)
        try:
            self.page.keyboard.type(text)
        except Exception as exc:            # noqa: BLE001
            return {'ok': False, 'detail': '输入失败：%s' % exc, 'state': {}}
        deadline = time.time() + max(2, int(ready_timeout))
        state = {}
        while time.time() < deadline:
            time.sleep(0.8)
            state = self._safe_evaluate(_REPLY_EDITOR_STATE_JS) or {}
            if state.get('publish_found') and not state.get('publish_disabled'):
                return {'ok': True, 'detail': '', 'state': state}
        return {'ok': False, 'detail': '输入后「发布」按钮仍不可用（页面可能改版）',
                'state': state}

    def manage_reply_visible(self, reply_text):
        '''当前页面（管理页）里有没有我们的回复正文（**只读当前 DOM，不导航**）。'''
        want = flat_text(reply_text or '')[:60]
        if not want:
            return False
        body = flat_text(self._page_body_text() or '')
        return want in body

    def wait_manage_reply_visible(self, reply_text, timeout=25, interval=2.0):
        '''等「我们的回复」出现在当前页面上（不导航、不重载）。

        发送后的**唯一**安全核实方式：点完「发布」到服务端回执之间有几秒，
        这段时间里任何 goto/reload 都会掐死请求（2026-09-28 真机定位）。
        '''
        deadline = time.time() + max(3, int(timeout))
        while time.time() < deadline:
            if self.manage_reply_visible(reply_text):
                return True
            time.sleep(max(0.5, float(interval)))
        return False

    def _reply_error_text(self):
        '''当前页面上的报错文案（发布被拒时唯一可读的证据）。'''
        try:
            hits = self._safe_evaluate(_REPLY_ERROR_JS) or []
        except Exception as exc:            # noqa: BLE001
            log.debug('读取页面报错文案失败：%s', exc)
            return ''
        return ' / '.join([str(h) for h in hits[:2] if h])

    def send_reply_from_manage(self, comment_text, reply_text, dry_run=False,
                               verify_wait=6, ensure_page=True, answer_url='',
                               verify_timeout=25):
        '''在评论管理页直接回复某条评论（**推荐路径**）。

        为什么推荐：评论就是从管理页挑出来的，卡片必然在场；回答页则按热度只渲染
        前 N 条（真机实测某回答 76 条评论只渲染 11 条），评论一多就「找不到」。
        返回 {ok, sent, detail}。dry_run=True 只填不点发布。

        ★ 发送后的核实纪律（2026-09-28 修「明明发出去了却报未确认」）：
          1. 点完「发布」**先原地等**（等回执 + 等嵌套渲染），**绝不立刻换页**——
             旧实现紧接着 collect_manage_comments() 会 goto 同一 URL 整页重载，
             把还在飞的发布请求掐死，于是每个回复都变成「已点发送，未确认到」；
          2. 原地读不到、且给了 answer_url 时，才跳到回答页做二次核实
             （verify_reply_landed 自带重载 + 展开「最新」）；
          3. 仍然读不到时按「已发送但未确认」返回（sent=True）——调用方据此
             **不重复发、也不算任务失败**（否则三连败就熔断停用整类任务）。
        '''
        text = str(reply_text or '').strip()
        if not text:
            return {'ok': False, 'sent': False, 'detail': '回复内容为空'}
        opened = self.open_manage_reply_editor(comment_text, ensure_page=ensure_page)
        if not opened.get('ok'):
            return {'ok': False, 'sent': False,
                    'detail': opened.get('detail') or '打不开编辑器'}
        typed = self._type_into_reply_editor(text)
        if not typed.get('ok'):
            self._clear_editor()
            return {'ok': False, 'sent': False,
                    'detail': typed.get('detail') or '输入失败'}
        if dry_run:
            self._clear_editor()
            return {'ok': True, 'sent': False,
                    'detail': '演练：已填入编辑器并确认可发布，未点击发送'}
        clicked = self._safe_evaluate(_CLICK_MANAGE_PUBLISH_JS) or {}
        if not clicked.get('ok'):
            return {'ok': False, 'sent': False,
                    'detail': clicked.get('reason') or '点发送失败'}
        # ① 原地等回执 + 等我们的回复渲染出来
        if self.wait_manage_reply_visible(text, timeout=verify_timeout):
            return {'ok': True, 'sent': True,
                    'detail': '已发送（管理页已显示回复）'}
        # ② 原地读不到：先把页面上的报错文案读出来（发布被拒时就是它）
        err = self._reply_error_text()
        if err:
            # 明确被服务端拒绝：此时**没发出去**，允许换路径重试/如实报失败
            return {'ok': False, 'sent': False,
                    'detail': '发送被拒：%s' % err}
        # ③ 二次核实：回答页（列表渲染慢/管理页不显示嵌套时）
        if answer_url:
            try:
                if self.verify_reply_landed(comment_text, text,
                                            answer_url=answer_url):
                    return {'ok': True, 'sent': True,
                            'detail': '已发送并确认（回答页评论区读到）'}
            except Exception as exc:        # noqa: BLE001
                log.debug('回答页二次核实失败：%s', exc)
        return {'ok': False, 'sent': True,
                'detail': '已点发送，但未确认到落地（已在回答页二次核实，未重复发送）'}

    def _page_body_text(self):
        '''当前页面正文（读不到返回空串，绝不抛）。'''
        try:
            return self.page.inner_text('body') or ''
        except Exception as exc:            # noqa: BLE001
            log.debug('读取页面正文失败：%s', exc)
            return ''

    # ---------------- 评论 ----------------

    def collect_manage_comments(self, limit=20, scrolls=4, wait=6, go=True):
        '''评论管理页 → 首屏 N 条评论（归一化后的 dict 列表）。

        go=False：**不导航**，直接读当前页面（发送回复后的核实必须用这个：
        点完「发布」立刻 goto 同一 URL 会整页重载，把还在飞的发布请求一起
        掐死——2026-09-28 真机：回复明明发出去了/或干脆没发出去，全都变成
        「已点发送，未在本页确认到」）。
        '''
        if go:
            self.page.goto(COMMENT_MANAGE_URL, wait_until='domcontentloaded',
                           timeout=45000)
            time.sleep(wait)
        for _ in range(max(0, int(scrolls))):
            self._safe_evaluate(
                '() => { window.scrollTo(0, document.body.scrollHeight); return true; }')
            time.sleep(1.2)
        raw = self._safe_evaluate(_MANAGE_COMMENTS_JS, int(limit)) or {}
        cards = [parse_manage_card(c) for c in ((raw or {}).get('cards') or [])]
        return {'ok': bool(cards), 'count': (raw or {}).get('count') or 0,
                'comments': cards}

    def read_answer_comments(self, newest_first=True):
        '''当前回答页的评论列表（含「这条评论下面有没有嵌套回复」的保守判断）。

        newest_first：展开后把排序切到「最新」（回答页默认按热度只渲染前 N 条，
        我们要回复的评论常常不在里面）。
        '''
        # 评论默认折叠：先点开「N 条评论」，再读
        self._safe_evaluate('''() => {
          const ZW = String.fromCharCode(8203,8204,8205,65279);
          const zwRe = new RegExp('[' + ZW + ']', 'g');
          const clean = s => (s || '').replace(zwRe, '').trim();
          // ★ 必须点「本回答」的评论按钮：页面顶部问题头部也有一个「N 条评论」，
          //   点错了展开的是问题的评论（真机踩过：只读到 1 条无关评论）
          const scope = document.querySelector('.AnswerItem .ContentItem-actions')
              || document.querySelector('.QuestionAnswer-content .ContentItem-actions');
          let hit = scope ? Array.from(scope.querySelectorAll('button'))
              .find(b => /条评论/.test(clean(b.innerText))) : null;
          if (!hit) {
            hit = Array.from(document.querySelectorAll('.ContentItem-actions button'))
                .find(b => /条评论/.test(clean(b.innerText)));
          }
          if (!hit) {
            hit = Array.from(document.querySelectorAll('button'))
                .find(b => /条评论/.test(clean(b.innerText)));
          }
          if (hit) { hit.click(); return clean(hit.innerText); }
          return '';
        }''')
        time.sleep(4)
        if newest_first:
            self._click_comment_sort('最新')
        # 有界加载：滚动评论容器 / 点「更多·全部·展开」，直到条数不再增长
        # （2026-09-27：只滚 3 屏时，较老的评论根本没加载出来）
        info = {}
        seen = -1
        for _round in range(8):
            info = self._safe_evaluate(_ANSWER_COMMENTS_JS) or {}
            n = len(info.get('comments') or [])
            if n <= seen:
                break
            seen = n
            self._safe_evaluate(_LOAD_MORE_COMMENTS_JS)
            time.sleep(1.4)
        return info or {'ok': False, 'comments': []}


# ------------------------------------------------------------
# 便捷函数：给非浏览器上下文（单测 / CLI）复用纯逻辑
# ------------------------------------------------------------
def summarize_checkin(tasks_by_key, want_follow=True, want_vote=True, want_comment=True):
    '''打卡状态摘要：今天还差哪几项（给台账/UI 用）。'''
    need = {}
    for key, want in (('follow', want_follow), ('vote', want_vote),
                      ('comment', want_comment)):
        task = (tasks_by_key or {}).get(key)
        if task is None:
            need[key] = 'unknown'
        elif not want:
            need[key] = 'disabled'
        else:
            need[key] = 'done' if task.get('done') else 'pending'
    return need


def is_target_followable(item):
    '''该回答条目能不能作为「关注」目标（未关注 + 有按钮 + 非自己）。'''
    it = item or {}
    if not it.get('has_follow'):
        return False
    return follow_state(it.get('follow_text')) == 'none'


def is_target_votable(item):
    '''该回答条目能不能作为「赞同」目标（未赞同 + 按钮可用）。'''
    it = item or {}
    if not it.get('has_vote') or it.get('vote_disabled'):
        return False
    return vote_state(it.get('vote_text')) == 'none'

# ============================================================
# 回复评论：编辑器 DOM（2026-09-27 真机探针 probe_reply_editor.py 实测）
#   1) 点某条评论的「回复」→ 出现 Draft.js 编辑器并**自动获得焦点**；
#   2) 编辑器空着时「发布」按钮是 disabled，输入后才可用（天然的发送就绪判据）；
#   3) 中文用 page.keyboard.type 直接输入即可；
#   4) 回复会嵌套在该评论下面（.CommentContent 数量 +1）——「已回复」的判据。
# 定位一律按**评论正文匹配**，不用序号（新评论进来会让序号漂移）。
# ============================================================
_OPEN_REPLY_JS = _js('target', '''
  const squash = s => clean(s).replace(/[ ]+/g, '');
  const want = squash(target);
  const items = Array.from(document.querySelectorAll('.CommentContent'));
  let cc = items.find(x => squash(txt(x)) === want);
  if (!cc) cc = items.find(x => squash(txt(x)).indexOf(want) >= 0);
  if (!cc) return { ok: false, reason: 'no-comment' };
  let cur = cc.parentElement;
  let holder = null;
  for (let k = 0; k < 5 && cur; k++) {
    if (Array.from(cur.querySelectorAll('button')).some(b => txt(b) === '回复')) { holder = cur; break; }
    cur = cur.parentElement;
  }
  if (!holder) return { ok: false, reason: 'no-holder' };
  const before = holder.querySelectorAll('.CommentContent').length;
  const btn = Array.from(holder.querySelectorAll('button')).find(b => txt(b) === '回复');
  if (!btn) return { ok: false, reason: 'no-reply-button' };
  btn.click();
  return { ok: true, nested_before: before, comment: txt(cc).slice(0, 60) };
''')

# 评论/回复共用的编辑器状态与「发布」按钮定位。
# ★ 判据与回复链路保持一致（已被真机验证无数次）：编辑器取最后一个可见的
#   Draft 编辑器；「发布」按钮取**最后一个**文案为「发布」的按钮。
_EDITOR_STATE_JS = _js('', '''
  const eds = Array.from(document.querySelectorAll('div.public-DraftEditor-content'))
      .filter(e => e.offsetParent);
  const active = eds.find(e => document.activeElement === e || e.contains(document.activeElement));
  const ed = active || eds[eds.length - 1] || null;
  // 「发布」按钮：直接量到文案为「发布」且可点的按钮。
  // ★ 不加 offsetParent 过滤 —— 真机实测：评论框的按钮在部分环境下
  //   offsetParent 为 null（fixed/惰性渲染），过滤掉就会误判「发布不可用」
  //   而永远发不出去（回复链路当年也踩过同一类坑）。
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => flat(txt(b)) === '发布');
  const pub = pubs[pubs.length - 1] || null;
  return { has_editor: !!ed, focused: !!active, text: ed ? txt(ed) : '',
           publish_found: !!pub, publish_disabled: pub ? !!pub.disabled : null,
           publish_candidates: pubs.length };
''')

# 给「这条回答」发新评论的入口（真机探针 2026-09-29 确认）：
#   零评论是「添加评论」，已有评论是「N 条评论」，展开后是「收起评论」；
#   ★ 必须限定在首条回答内——问题头部也有「N 条评论」，
#     点错了展开的是问题的评论（同类坑真机踩过）；
#   ★ 可见性用 shown()（几何判断）挑**优先**候选，但找不到可见的就退回第一个匹配：
#     回答页操作栏是 fixed 布局（offsetParent 恒为 null），不能拿它当过滤器。
_OPEN_ANSWER_COMMENT_JS = _js('', r'''
  const answer = document.querySelector('.AnswerItem')
      || document.querySelector('.QuestionAnswer-content');
  const scopes = answer ? Array.from(answer.querySelectorAll('.ContentItem-actions')) : [];
  const picks = [];
  for (const scope of scopes) {
    const btns = Array.from(scope.querySelectorAll('button'));
    for (const b of btns) {
      const label = flat(txt(b));
      if (label === '添加评论' || label === '收起评论'
          || /^\d[\d,\s]*条评论$/.test(label)
          || /添加评论/.test(b.getAttribute('aria-label') || '')) {
        picks.push(b);
      }
    }
  }
  if (!picks.length) return { ok: false, reason: 'no-comment-entry' };
  const hit = picks.find(shown) || picks[0];      // 优先可见的那个
  const label = flat(txt(hit));
  if (label !== '收起评论') hit.click();          // 已展开时保持编辑器打开
  return { ok: true, via: label || 'aria',
           shown: shown(hit), candidates: picks.length };
''')

_CLICK_PUBLISH_JS = _js('', '''
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => shown(b) && flat(txt(b)) === '发布');
  const pub = pubs[pubs.length - 1];
  if (!pub) return { ok: false, reason: 'no-publish-button' };
  if (pub.disabled) return { ok: false, reason: 'publish-disabled' };
  pub.click();
  return { ok: true };
''')

_THREAD_JS = _js('target', '''
  const squash = s => clean(s).replace(/[ ]+/g, '');
  const want = squash(target);
  const items = Array.from(document.querySelectorAll('.CommentContent'));
  let cc = items.find(x => squash(txt(x)) === want);
  if (!cc) cc = items.find(x => squash(txt(x)).indexOf(want) >= 0);
  if (!cc) return { ok: false, reason: 'no-comment' };
  let cur = cc.parentElement;
  let holder = null;
  for (let k = 0; k < 5 && cur; k++) {
    if (Array.from(cur.querySelectorAll('button')).some(b => txt(b) === '回复')) { holder = cur; break; }
    cur = cur.parentElement;
  }
  if (!holder) return { ok: false, reason: 'no-holder' };
  const all = Array.from(holder.querySelectorAll('.CommentContent'))
      .map(x => txt(x).slice(0, 200));
  return { ok: true, contents: all, nested: all.length - 1 };
''')


class ReplyActionsMixin:
    '''评论回复的页面动作：开编辑器 / 填入 / 发送 / 核实是否已回复。'''

    def _click_comment_sort(self, label='最新'):
        '''把评论区排序切到「最新」。

        回答页默认按热度只渲染前 N 条评论（真机实测：某回答 76 条评论只渲染 11 条），
        而我们要回复的评论往往就在「最新」那一批里——不切排序就会「找不到这条评论」。
        '''
        try:
            loc = self.page.get_by_text(label, exact=True)
            if loc.count() == 0:
                return False
            loc.first.click(timeout=5000)
            time.sleep(2.5)
            return True
        except Exception as exc:            # noqa: BLE001
            log.debug('切换评论排序到「%s」失败：%s', label, exc)
            return False

    def verify_reply_landed(self, comment_text, reply_text, reload_wait=6,
                            max_rounds=2, answer_url=''):
        '''发送后核实：重新加载页面 → 展开评论区（切最新）→ 找回复正文。

        2026-09-27 真机教训：点完「发布」立刻读当前 DOM 读不到（知乎要重新渲染
        嵌套结构），于是**明明发出去了却报「未确认到回复落地」**——假警报。
        现在改成重新加载后再找，判据是「评论区文本里出现我们的回复正文」。

        answer_url 非空时：**先导航到该回答页**再核实（从管理页发起的回复，
        回答页才是嵌套结构渲染最完整的地方）。注意本方法内部会 reload，
        只能用于「已经确认点过发布、且已等过回执」之后。
        '''
        text = flat_text(reply_text or '')[:60]
        if not text:
            return False
        for rnd in range(max(1, int(max_rounds))):
            try:
                if answer_url and rnd == 0:
                    self.page.goto(answer_url, wait_until='domcontentloaded',
                                   timeout=45000)
                else:
                    self.page.reload(wait_until='domcontentloaded', timeout=45000)
                time.sleep(reload_wait)
            except Exception as exc:        # noqa: BLE001
                log.debug('发送后重新加载失败：%s', exc)
            body = ''
            try:
                self.read_answer_comments()
                body = flat_text(self._page_body_text() or '')
            except Exception as exc:        # noqa: BLE001
                log.debug('发送后读评论区失败：%s', exc)
            if text in body:
                return True
        return False



    def open_reply_editor(self, comment_text, pause=2.5):
        '''按评论正文定位并点开「回复」编辑器。返回 {ok, editor, detail}。'''
        r = self._safe_evaluate(_OPEN_REPLY_JS, comment_text) or {}
        if not r.get('ok'):
            return {'ok': False, 'editor': {}, 'detail': r.get('reason') or '打不开编辑器',
                    'nested_before': 0}
        time.sleep(pause)
        state = self._safe_evaluate(_EDITOR_STATE_JS) or {}
        if not state.get('has_editor'):
            return {'ok': False, 'editor': state, 'detail': '点了回复但没出现编辑器',
                    'nested_before': r.get('nested_before') or 0}
        return {'ok': True, 'editor': state, 'detail': '',
                'nested_before': r.get('nested_before') or 0,
                'comment': r.get('comment') or ''}

    def read_comment_thread(self, comment_text):
        '''读某条评论所在的线程（含嵌套回复的正文列表）。'''
        return self._safe_evaluate(_THREAD_JS, comment_text) or {'ok': False,
                                                                 'contents': []}

    def comment_replied(self, comment_text, our_texts=()):
        '''这条评论下面有没有我们的回复。

        判据（探针实测）：回复会作为嵌套的 .CommentContent 挂在同一条评论下。
        两条并用：① 嵌套数量 > 0 且内容与我们台账里的某条回复对得上；
        ② 没有台账可比时，只要有嵌套就认为「有人回过」——保守跳过，
        宁可漏回一条，也不重复打扰读者。
        '''
        thread = self.read_comment_thread(comment_text)
        if not thread.get('ok'):
            return None
        contents = [str(x).strip() for x in (thread.get('contents') or [])]
        if len(contents) <= 1:
            return False
        nested = contents[1:]
        wanted = [str(t).strip() for t in (our_texts or []) if str(t).strip()]
        if wanted:
            for n in nested:
                for w in wanted:
                    if w and (w in n or n in w):
                        return True
            # 兜底（真机补）：嵌套结构读不到时，直接看页面正文里有没有我们的回复
            try:
                body = self.page.inner_text('body') or ''
            except Exception:               # noqa: BLE001
                body = ''
            for w in wanted:
                if w and w in body:
                    return True
            return False
        return True

    def open_answer_comment_editor(self, pause=2.5):
        '''点开「这条回答」的评论输入框（给别人的回答发**新评论**，不是回复）。

        回答操作栏里是 `添加评论` 或 `N 条评论` 按钮；已展开时不再次收起。
        点开出现 Draft.js 编辑器（自动聚焦），
        发表按钮文案是「发布」。
        ★ 必须限定在 `本回答` 的 .ContentItem-actions 内：问题头部也有
        「N 条评论」，点错了展开的是**问题**的评论（真机踩过同类坑）。
        '''
        r = self._safe_evaluate(_OPEN_ANSWER_COMMENT_JS) or {}
        if not r.get('ok'):
            return {'ok': False, 'detail': r.get('reason') or '找不到评论入口'}
        time.sleep(pause)
        state = self._safe_evaluate(_EDITOR_STATE_JS) or {}
        if not state.get('has_editor'):
            return {'ok': False, 'detail': '点了添加评论但没出现编辑器'}
        return {'ok': True, 'detail': '', 'state': state}

    def _focus_comment_editor(self):
        '''把输入焦点放到「当前可见的那个 Draft 编辑器」上。

        真机教训（2026-09-29）：点开评论框后编辑器**未必真的拿到焦点**
        （页面 JS 报 focused=true，但 keyboard.type 仍打在 body 上，文字进不去、
        「发布」永远禁用）。所以这里三级兜底：
          ① JS focus()；② Playwright 真实鼠标点进编辑器；③ 再校验一次。
        '''
        js_focus = """() => {
          const vis = e => {
            if (!e) return false;
            const r = e.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            const cs = getComputedStyle(e);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
          };
          const eds = Array.from(document.querySelectorAll(
              'div.public-DraftEditor-content, [contenteditable=true], textarea'))
              .filter(vis);
          const ed = eds[eds.length - 1];
          if (!ed) return false;
          ed.focus();
          return document.activeElement === ed || ed.contains(document.activeElement);
        }"""
        if self._safe_evaluate(js_focus):
            return True
        # ② 真实鼠标点进去（js focus 无效时最可靠）
        try:
            self.page.locator(
                'div.public-DraftEditor-content').last.click(timeout=5000)
        except Exception as exc:            # noqa: BLE001
            log.debug('点击评论编辑器失败：%s', exc)
            return False
        # ③ 校验
        return bool(self._safe_evaluate(js_focus))

    def _type_into_comment_box(self, text):
        '''往评论框输入正文；返回 (ok, how, error)。

        ★ 优先 Playwright 的「对元素 type」：它会自己滚动到元素、确保可交互、
          聚焦后再敲键——比「先 focus 再 keyboard.type」可靠（真机踩到过
          JS focus 报成功、实际按键仍打在 body 上，文字一个字都没进去）。
        '''
        try:
            loc = self.page.locator('div.public-DraftEditor-content').last
            loc.type(text, timeout=8000)
            return True, 'locator.type', ''
        except Exception as exc:            # noqa: BLE001
            log.debug('locator.type 失败，退回 keyboard.type：%s', exc)
        self._focus_comment_editor()
        try:
            self.page.keyboard.type(text)
            return True, 'keyboard.type', ''
        except Exception as exc:            # noqa: BLE001
            return False, 'keyboard.type', str(exc)

    def _restore_comment_toolbar(self):
        '''输入后工具栏收起时，重新触发同一编辑器的焦点事件。

        真机实测（2026-10-07）：Draft 编辑器仍有焦点和正文，但「发布」
        按钮已被卸载；单纯 focus() 无效，失焦再真实点击才会恢复工具栏。
        不重输正文、不按 Enter，只由调用方继续检查发送就绪状态。
        '''
        try:
            editor = self.page.locator('div.public-DraftEditor-content').last
            editor.blur(timeout=5000)
            editor.click(timeout=5000)
            return True
        except Exception as exc:            # noqa: BLE001
            log.debug('恢复评论工具栏失败：%s', exc)
            return False

    def send_answer_comment(self, text, dry_run=False, type_pause=0.4,
                           ready_timeout=10, verify_wait=8):
        '''在当前回答页给这条回答发一条新评论。

        返回 {ok, sent, detail}。与 send_reply 同一套判据：编辑器空时「发布」
        禁用、输入后可用 —— 「发布按钮可用」即发送就绪，不靠猜。
        发送后**重新加载页面**确认评论真的在（verify_reply_landed 同款逻辑）。
        '''
        text = str(text or '').strip()
        if not text:
            return {'ok': False, 'sent': False, 'detail': '评论内容为空'}
        opened = self.open_answer_comment_editor()
        if not opened.get('ok'):
            return {'ok': False, 'sent': False,
                    'detail': opened.get('detail') or '打不开评论框'}
        time.sleep(type_pause)
        ok, how, err = self._type_into_comment_box(text)
        if not ok:
            return {'ok': False, 'sent': False, 'detail': '输入失败：%s' % err}
        log.info('评论兜底：已用 %s 输入正文', how)
        deadline = time.time() + max(2, int(ready_timeout))
        state = {}
        restored_toolbar = False
        while time.time() < deadline:
            time.sleep(0.8)
            state = self._safe_evaluate(_EDITOR_STATE_JS) or {}
            if state.get('publish_found') and not state.get('publish_disabled'):
                break
            if (not state.get('publish_found') and state.get('has_editor')
                    and flat_text(state.get('text')) == flat_text(text)
                    and not restored_toolbar):
                restored_toolbar = True       # 每次发送最多恢复一次，仍受就绪超时约束
                if self._restore_comment_toolbar():
                    log.info('评论兜底：已重新聚焦编辑器，等待发布工具栏恢复')
        if state.get('publish_disabled') or not state.get('publish_found'):
            self._clear_editor()
            return {'ok': False, 'sent': False,
                    'detail': '输入后「发布」按钮仍不可用（页面可能改版）'}
        if dry_run:
            self._clear_editor()
            return {'ok': True, 'sent': False,
                    'detail': '演练：已填入评论并确认可发布，未点击发布'}
        clicked = self._safe_evaluate(_CLICK_PUBLISH_JS) or {}
        if not clicked.get('ok'):
            return {'ok': False, 'sent': False,
                    'detail': clicked.get('reason') or '点发布失败'}
        # 发送后核实：重新加载当前页，在评论区正文里找我们的评论
        if self.verify_answer_comment_landed(text, reload_wait=max(3, verify_wait)):
            return {'ok': True, 'sent': True, 'detail': '已发送并确认（重新加载后读到）'}
        return {'ok': True, 'sent': True,
                'detail': '已点发布，未在本页确认到（请人工核对）'}

    def verify_answer_comment_landed(self, text, reload_wait=6, max_rounds=2):
        '''核实评论是否落地：重新加载当前页 → 在正文里找我们的评论文本。'''
        want = flat_text(text or '')[:60]
        if not want:
            return False
        for _round in range(max(1, int(max_rounds))):
            try:
                self.page.reload(wait_until='domcontentloaded', timeout=45000)
                time.sleep(reload_wait)
            except Exception as exc:        # noqa: BLE001
                log.debug('发送评论后重新加载失败：%s', exc)
            try:
                body = flat_text(self._page_body_text() or '')
            except Exception:               # noqa: BLE001
                body = ''
            if want in body:
                return True
        return False

    def send_reply(self, comment_text, text, dry_run=False, type_pause=0.4,
                   ready_timeout=10, verify_wait=8):
        '''给某条评论发回复；dry_run=True 只把内容填进编辑器、不点发布。

        返回 {ok, sent, detail}。实测：编辑器空时「发布」禁用、输入后可用，
        所以「发布按钮可用」就是发送就绪判据，不靠猜。
        '''
        text = str(text or '').strip()
        if not text:
            return {'ok': False, 'sent': False, 'detail': '回复内容为空'}
        opened = self.open_reply_editor(comment_text)
        if not opened.get('ok'):
            return {'ok': False, 'sent': False, 'detail': opened.get('detail') or '打不开编辑器'}
        time.sleep(type_pause)
        try:
            self.page.keyboard.type(text)
        except Exception as exc:            # noqa: BLE001
            return {'ok': False, 'sent': False, 'detail': '输入失败：%s' % exc}
        deadline = time.time() + max(2, int(ready_timeout))
        state = {}
        while time.time() < deadline:
            time.sleep(0.8)
            state = self._safe_evaluate(_EDITOR_STATE_JS) or {}
            if state.get('publish_found') and not state.get('publish_disabled'):
                break
        if state.get('publish_disabled') or not state.get('publish_found'):
            self._clear_editor()
            return {'ok': False, 'sent': False,
                    'detail': '输入后「发布」按钮仍不可用（页面可能改版）'}
        if dry_run:
            self._clear_editor()
            return {'ok': True, 'sent': False,
                    'detail': '演练：已填入编辑器并确认可发布，未点击发送'}
        clicked = self._safe_evaluate(_CLICK_PUBLISH_JS) or {}
        if not clicked.get('ok'):
            return {'ok': False, 'sent': False,
                    'detail': clicked.get('reason') or '点发送失败'}
        time.sleep(max(2, int(verify_wait) * 0.5))
        # 快路径：当前 DOM 里嵌套回复数增加 / 正文出现我们的文本
        thread = self.read_comment_thread(comment_text)
        nested_after = int((thread or {}).get('nested') or 0)
        nested_before = int(opened.get('nested_before') or 0)
        if nested_after > nested_before:
            return {'ok': True, 'sent': True, 'detail': '已发送并确认出现在评论区'}
        body = ' '.join((thread or {}).get('contents') or [])
        if text[:8] and text[:8] in body:
            return {'ok': True, 'sent': True, 'detail': '已发送（在评论区读到回复内容）'}
        # ★ 慢路径（2026-09-27 真机补）：立刻读当前 DOM 会漏——知乎要重新渲染
        #   嵌套结构，于是出现过「明明发出去了却报未确认」的假警报。重新加载再找。
        if self.verify_reply_landed(comment_text, text):
            return {'ok': True, 'sent': True, 'detail': '已发送并确认（重新加载后读到）'}
        return {'ok': False, 'sent': True,
                'detail': '已点发送，但没确认到回复落地（请人工核对回答页评论区）'}

    def _clear_editor(self):
        '''清空编辑器（演练收尾/失败回滚用），不按 Enter、不发任何内容。'''
        try:
            self.page.keyboard.press('Control+A')
            self.page.keyboard.press('Delete')
            time.sleep(0.5)
        except Exception:                   # noqa: BLE001
            pass

