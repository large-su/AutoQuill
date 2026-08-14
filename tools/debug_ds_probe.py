# 临时排查脚本：DeepSeek 生成过程中，哪个 DOM 元素在增长？
# 目的：验证 web_drivers/deepseek.py 的 _RESULT_SELECTORS 命中的
#       到底是「正在生成的故事」还是页面上的其他元素（旧会话等）。
# 用法：PYTHONIOENCODING=utf-8 python tools/debug_ds_probe.py
# 会在真实 DeepSeek 账号发送一条测试 prompt，生成中每 2s 采样。
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import WEB_DRIVERS, WEB_DRIVER_NAME

cfg = WEB_DRIVERS[WEB_DRIVER_NAME]

# 与 web_drivers/deepseek.py 完全一致的候选列表
CANDIDATES = (
    "div[class*='message'] div[class*='markdown']",
    "div[class*='ds-markdown']",
    "div[class*='assistant'] div[class*='markdown']",
)
STOP_SELECTORS = (
    "button[aria-label*='停止']",
    "button[data-testid*='stop']",
    "button[class*='stop']",
    "div[role=button][class*='stop']",
    "div[aria-label*='停止']",
)

PROMPT = "请写一个约300字的小故事，主题：深夜的便利店。直接输出正文。"

DUMP_JS = """(SELS) => {
  const out = [];
  for (const s of SELS) {
    const els = document.querySelectorAll(s);
    out.push({sel: s, n: els.length,
              lens: Array.from(els).map(e => (e.innerText || '').length),
              heads: Array.from(els).map(e => (e.innerText || '').slice(0, 20))});
  }
  return out;
}"""

STOP_JS = """(SELS) => {
  const hits = [];
  for (const s of SELS) {
    if (document.querySelector(s)) hits.push(s);
  }
  return hits;
}"""


def dump(page, tag):
    print(f"\n=== {tag} ===")
    r = page.evaluate(DUMP_JS, list(CANDIDATES))
    for row in r:
        print(f"  {row['sel']}")
        print(f"    n={row['n']} lens={row['lens']}")
        for h in row["heads"]:
            print(f"      head: {h!r}")
    hits = page.evaluate(STOP_JS, list(STOP_SELECTORS))
    print(f"  停止按钮命中: {hits or '（无）'}")


def main():
    from web_drivers import create_driver

    driver = create_driver()
    driver.open_session()
    time.sleep(2)

    page = driver._page_instance()
    dump(page, "发送前页面（注意已有元素=旧会话/历史）")

    driver.input(PROMPT)
    driver.send()
    print(f"\n已发送，开始采样（2s 间隔，最长 60 轮）...")
    t0 = time.time()
    last = None
    stable = 0
    for i in range(60):
        time.sleep(2)
        r = page.evaluate(DUMP_JS, list(CANDIDATES))
        stops = page.evaluate(STOP_JS, list(STOP_SELECTORS))
        line = f"t={time.time() - t0:5.1f}s stop={'Y' if stops else '-'} "
        for row in r:
            line += f" | {row['lens']}"
        print(line, flush=True)
        sig = tuple(tuple(row["lens"]) for row in r)
        if sig == last:
            stable += 1
        else:
            stable = 0
        last = sig
        if stable >= 5:
            print("\n连续 5 轮无变化，结束采样")
            break

    dump(page, "结束后页面")
    driver.close_session()


if __name__ == "__main__":
    main()
