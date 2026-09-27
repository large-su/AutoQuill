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
  const txt = el => clean((el && (el.innerText || el.textContent)) || '');
  const cls = el => (el && typeof el.className === 'string' ? el.className : '');
  const answerItems = () => {
    const a = Array.from(document.querySelectorAll('.AnswerItem'));
    return a.length ? a : Array.from(document.querySelectorAll('.QuestionAnswer-content'));
  };
  const answerIdOf = url => { const m = (url || '').match(/answer[/]([0-9]+)/); return m ? m[1] : ''; };
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

    # ---------------- 评论 ----------------

    def collect_manage_comments(self, limit=20, scrolls=4, wait=6):
        '''评论管理页 → 首屏 N 条评论（归一化后的 dict 列表）。'''
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

_EDITOR_STATE_JS = _js('', '''
  const eds = Array.from(document.querySelectorAll('div.public-DraftEditor-content'))
      .filter(e => e.offsetParent);
  const active = eds.find(e => document.activeElement === e || e.contains(document.activeElement));
  const ed = active || eds[eds.length - 1] || null;
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetParent && txt(b) === '发布');
  const pub = pubs[pubs.length - 1] || null;
  return { has_editor: !!ed, focused: !!active, text: ed ? txt(ed) : '',
           publish_found: !!pub, publish_disabled: pub ? !!pub.disabled : null };
''')

_CLICK_PUBLISH_JS = _js('', '''
  const pubs = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetParent && txt(b) === '发布');
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
                            max_rounds=2):
        '''发送后核实：重新加载页面 → 展开评论区（切最新）→ 找回复正文。

        2026-09-27 真机教训：点完「发布」立刻读当前 DOM 读不到（知乎要重新渲染
        嵌套结构），于是**明明发出去了却报「未确认到回复落地」**——假警报。
        现在改成重新加载后再找，判据是「评论区文本里出现我们的回复正文」。
        '''
        text = (reply_text or '').strip()
        if not text:
            return False
        for _round in range(max(1, int(max_rounds))):
            try:
                time.sleep(reload_wait)
                self.page.reload(wait_until='domcontentloaded', timeout=45000)
                time.sleep(reload_wait)
            except Exception as exc:        # noqa: BLE001
                log.debug('发送后重新加载失败：%s', exc)
            body = ''
            try:
                self.read_answer_comments()
                body = self.page.inner_text('body') or ''
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
            if not state.get('publish_disabled'):
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

