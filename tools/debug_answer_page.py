# 临时探测脚本：知乎创作中心「回答管理」页面结构
# 目的：定位回答条目容器、每条的统计字段（阅读/点赞/评论/收藏）、
#       分页/懒加载方式，为写成绩统计脚本做准备。
# 用法：PYTHONIOENCODING=utf-8 python tools/debug_answer_page.py
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = "https://www.zhihu.com/creator/manage/creation/answer"

STRUCT_JS = """() => {
  const out = {};
  // 条目容器候选
  const listCands = [
    "[class*='CreationManage']", "[class*='Card']", "[class*='List']",
    "[class*='Answer']", "main",
  ];
  const hits = [];
  for (const s of listCands) {
    const els = document.querySelectorAll(s);
    if (els.length) hits.push({sel: s, n: els.length});
  }
  out.containerCandidates = hits;

  // 按钮文本（找筛选/分页/更多）
  const btns = [];
  for (const el of document.querySelectorAll('button, [role=button]')) {
    const t = (el.innerText || '').trim().slice(0, 20);
    if (t) btns.push(t);
  }
  out.buttons = btns.slice(0, 40);

  // 页面文本片段
  out.bodyText = document.body.innerText.slice(0, 900);
  return out;
}"""


def main():
    from applications.zhihu_story.browser_adapter import get_browser

    browser = get_browser()
    page = browser.page
    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)

    r = page.evaluate(STRUCT_JS)
    print("=== 容器候选 ===")
    for h in r["containerCandidates"]:
        print(f"  {h['sel']}: {h['n']} 个")
    print("\n=== 按钮 ===")
    print(" ", r["buttons"])
    print("\n=== 页面文本片段 ===")
    print(" ", r["bodyText"][:700])

    print("\n页面保持打开 15 秒供人工查看…")
    time.sleep(15)
    browser.close()


if __name__ == "__main__":
    main()
