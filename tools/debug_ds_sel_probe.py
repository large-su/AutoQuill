# 临时探测：已有对话场景下，流式期间 main-content 选择器匹配到什么
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMP = """() => {
  const out = {sel0: null, think: null};
  const el = document.querySelector(
      "div[class*='ds-assistant-message-main-content']");
  if (el) {
    const t = (el.innerText || '');
    out.sel0 = {
      len: t.length,
      head: t.slice(0, 24).replace(/\\n/g, ' '),
      hasThink: !!el.querySelector("[class*='ds-think-content']"),
    };
  }
  const th = document.querySelector("[class*='ds-think-content']");
  if (th) out.think = (th.innerText || '').length;
  return out;
}"""


def main():
    from web_drivers import create_driver
    driver = create_driver()
    driver.open_session()
    driver.setup()
    driver.input("写一个两百字的小段子，先认真思考再写。")
    driver.send()

    page = driver._page_instance()
    for i in range(8):
        time.sleep(2)
        r = page.evaluate(DUMP)
        print(f"  {i*2:>3}s  main-content: {r['sel0']}  think: {r['think']}")
    time.sleep(2)
    print("结束后:", page.evaluate(DUMP))

    import web_drivers
    web_drivers._driver_instance = None


if __name__ == "__main__":
    main()
