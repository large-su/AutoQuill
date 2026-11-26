"""Explicit question length contracts shared by writing and editorial review."""
import re


def explicit_range(question):
    match = re.search(r'(\d{3,5})\s*(?:字)?\s*(?:到|至|[-—–~～])\s*(\d{3,5})\s*字', str(question or ''))
    if not match:
        return None
    low, high = map(int, match.groups())
    return (low, high) if 0 < low <= high else None


def body_chars(story):
    body = re.sub(r'(?m)^\s*#{1,6}\s*\*?\*?\d+\*?\*?\s*$', '', story)
    return len(re.sub(r'\s+', '', body))


def fit_template(template, question):
    """Resolve defaults before interpolating reference text or author material."""
    if not explicit_range(question):
        return template
    template = re.sub(r'每节\s*(?:不少于\s*\d+|\d+\s*[-～]\s*\d+)\s*字',
                      '各节篇幅按本题总预算分配', template)
    return re.sub(r'(?:全书|总字数)\s*\*{0,2}不少于\s*\d+\s*字\*{0,2}',
                  '全篇服从本题字数区间', template)


def budget_instruction(question):
    bounds = explicit_range(question)
    if not bounds:
        return ''
    low, high = bounds
    target = (low + high) // 2
    return (f'\n\n## 本题总篇幅\n正文必须在{low}～{high}字符内，按去空白正文字符计算，'
            f'包含标点，不含Markdown章节标题。先以约{target}字符分配全篇，'
            '引言和结尾都计入；每节不另设最低字数。把篇幅留给关键选择与后果，'
            '不先写一篇无编号全文再按章节重复一遍，不输出篇幅规划或计数报告。')
