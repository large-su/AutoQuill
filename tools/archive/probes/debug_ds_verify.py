# 复测脚本：修复后的 DeepSeek 驱动链路完整验证
#   1. setup() 目标状态驱动：读模式 tab + 开关状态，与目标不一致才点
#   2. _current_reply_len() 抓的是正文容器（ds-assistant-message-main-content），
#      思考容器（ds-think-content）增长不应影响判定
#   3. wait_complete 在正文稳定后才判完成，read_result 读回完整故事
# 用法：PYTHONIOENCODING=utf-8 python tools/debug_ds_verify.py
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROMPT = "请写一个约300字的小故事，主题：雨夜末班车。直接输出正文。"

# 独立采样：分别读思考容器 / 正文容器长度，验证增长源
DUMP_JS = """() => {
  const out = {think: [], main: []};
  for (const el of document.querySelectorAll("div[class*='ds-markdown']")) {
    const cls = String(el.className);
    if (cls.includes('ds-think-content')) {
      out.think.push({l: (el.innerText || '').length, h: (el.innerText || '').slice(0, 15)});
    }
  }
  for (const el of document.querySelectorAll("div[class*='ds-assistant-message-main-content']")) {
    out.main.push({l: (el.innerText || '').length, h: (el.innerText || '').slice(0, 15)});
  }
  return out;
}"""


def main():
    from web_drivers import create_driver

    driver = create_driver()
    # 打开页面 + 目标状态驱动 setup（config 默认 fast：深思开 搜索开）
    driver.open_session()
    driver.setup()
    print("\n=== setup() 完成（应显示模式/开关的目标状态日志） ===")

    page = driver._page_instance()
    print("页面当前结构（确认思考/正文容器状态）:")
    print(" ", page.evaluate(DUMP_JS))

    driver.input(PROMPT)
    driver.send()
    print("\n=== 已发送，开始采样（2s 间隔） ===")
    t0 = time.time()
    last_main = 0
    stable_main = 0
    for i in range(50):
        time.sleep(2)
        r = page.evaluate(DUMP_JS)
        think = r["think"][0]["l"] if r["think"] else 0
        main = r["main"][0]["l"] if r["main"] else 0
        cur = driver._current_reply_len()
        print(f"t={time.time() - t0:5.1f}s 思考={think:4d} 正文={main:4d} "
              f"driver读到={cur:4d}",
              flush=True)
        if main != last_main:
            stable_main = 0
            last_main = main
        else:
            stable_main += 1
        if stable_main >= 3 and main > 200:
            print("正文连续 3 轮无变化，判定完成")
            break
    print("\n=== read_result() ===")
    story = driver.read_result()
    print(f"读回长度: {len(story)} 字符")
    print(f"读回前 120 字: {story[:120]}")
    print(f"读回是否像正文（非'嗯/用户要求'思考口吻）: "
          f"{'✓ 是正文' if not story.startswith('嗯') and len(story) > 150 else '✗ 可疑'}")

    driver.close_session()


if __name__ == "__main__":
    main()
