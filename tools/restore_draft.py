# 用新富文本发布通道恢复指定故事的草稿（验证用，等价于 publish_story 全路径）。
# 用法：python tools/restore_draft.py <story_md> <question_url>
import sys
import time

sys.path.insert(0, r"D:\Code\AutoQuill")

from applications.zhihu_story.browser_adapter import get_browser

story_path = sys.argv[1]
url = sys.argv[2]

with open(story_path, encoding="utf-8") as f:
    story = f.read()

browser = get_browser()
browser.open_question(url)
ok = browser.publish_story(story)
print("RESTORE-OK" if ok else "RESTORE-FAIL", flush=True)

d = browser.get_draft_content()
print(f"RESTORE-DRAFT({len(d)}): {d[:400]}", flush=True)
print(f"RESTORE-HAS-BOLD: {'<b>' in d}", flush=True)
time.sleep(3)
