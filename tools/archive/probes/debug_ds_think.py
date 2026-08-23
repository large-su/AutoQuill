# 临时探测脚本：DeepSeek 深度思考时思考容器的 DOM 表现
# 目的：定位「模型思考中」阶段的容器（class、文本增长、结束后状态），
#       为 wait_complete 增加思考阶段心跳提供依据。
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

DUMP_JS = """() => {
  const out = {think: [], body: 0, stop: false};
  // 思考容器候选
  for (const sel of ['[class*=ds-think-content]',
                     '[class*=ds-think]',
                     'div[class*=think]']) {
    for (const el of document.querySelectorAll(sel)) {
      const t = (el.innerText || '').trim();
      out.think.push({
        sel: sel,
        cls: (el.className || '').toString().slice(0, 70),
        len: t.length,
        head: t.slice(0, 30).replace(/\\n/g, ' '),
      });
    }
  }
  // 正文容器
  const b = document.querySelector('[class*=ds-assistant-message-main-content]');
  out.body = b ? (b.innerText || '').length : 0;
  // 停止按钮
  out.stop = !!document.querySelector('[class*=ds-btn][class*=stop], button[class*=stop]');
  return out;
}"""


def main():
    from web_drivers import create_driver
    driver = create_driver()
    driver.open_session()
    driver.setup()

    page = driver._page_instance()
    time.sleep(1)
    driver.input("请写一个三百字的小故事，先认真思考再写。")
    driver.send()

    print("=== 轮询观察思考/正文容器 ===")
    for i in range(14):
        time.sleep(2)
        r = page.evaluate(DUMP_JS)
        thinks = " | ".join(
            f"[{t['sel'][8:26]}...]{t['cls'][-22:]}:{t['len']}"
            for t in r["think"][:4])
        print(f"  {i*2:>3}s  思考容器: {thinks or '(无)'}")
        print(f"       正文: {r['body']}  停止按钮: {r['stop']}")

    print("\n=== 结束后再次 dump ===")
    time.sleep(3)
    r = page.evaluate(DUMP_JS)
    for t in r["think"][:6]:
        print(f"  cls={t['cls']!r} len={t['len']} head={t['head']!r}")
    print(f"  正文长度: {r['body']}  停止按钮: {r['stop']}")
    driver._page_instance().close() if hasattr(driver, "_page_instance") else None
    import web_drivers
    web_drivers._driver_instance = None


if __name__ == "__main__":
    main()
