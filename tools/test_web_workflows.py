"""Explicit real-account acceptance for web model adapters.

Uses a caller-supplied isolated data directory/profile. Classic and clean runs
write real Zhihu drafts; they never click Publish or overwrite an existing draft.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--workflow", choices=("classic", "clean", "interactions", "business", "generation"), default="classic")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--checks", help="business 验收项，逗号分隔；未指定时运行全部")
    parser.add_argument("--human-verify", action="store_true",
                        help="前台测试遇到网站验证时保留当前请求，等待人工完成")
    args = parser.parse_args()
    data_dir, profile = args.data_dir.resolve(), args.profile.resolve()
    installed = Path(os.environ.get("APPDATA", "")) / "AutoQuill"
    if data_dir == installed.resolve() or profile == (installed / "data/browser_profile").resolve():
        parser.error("验收必须使用独立数据目录和复制的浏览器配置")
    os.environ["AQ_DATA_DIR"] = str(data_dir)
    os.environ["PYTHONIOENCODING"] = "utf-8"

    import config
    from config import story as sconfig
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    from web_drivers import browser_pool, get_driver, reset_driver
    from web_drivers.account_status import inspect_account, observe_account
    from web_drivers.capabilities import get_provider_settings, record_validation
    from webui.run_manager import TaskRunner, _RunSpec

    if args.provider not in config.WEB_DRIVERS:
        parser.error("未知provider")
    config.set_runtime_web_driver(args.provider, persist=False)
    config.set_runtime_mode("web", persist=False)
    config.set_runtime_browser_headless(not args.visible, persist=False)
    # The provider under test must produce the result. An API fallback cannot
    # serve as acceptance evidence for a web adapter.
    sconfig.WEB_FAILOVER_TO_API = False
    run_id = f"{args.provider}-{args.workflow}-{time.strftime('%Y%m%d-%H%M%S')}"
    out = data_dir / "logs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(out / "run.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)], force=True)
    if args.human_verify:
        if not args.visible:
            parser.error("--human-verify 需要 --visible")
        from web_drivers.text_chat import ConfiguredTextChatDriver
        from web_drivers.browser_pool import WebVerificationRequired
        original_check = ConfiguredTextChatDriver._raise_if_logged_out

        def check_with_handoff(driver, require_ready=True):
            try:
                return original_check(driver, require_ready=require_ready)
            except WebVerificationRequired:
                logging.warning("请在测试浏览器完成当前网页验证；保留本轮请求等待，不重复发送")
                page = driver._page_instance()
                page.bring_to_front()
                until = time.monotonic() + 900
                while time.monotonic() < until:
                    page.wait_for_timeout(1000)
                    state = inspect_account(driver.provider_name, driver._get_browser().context, page)
                    if state["status"] == "ready":
                        logging.info("网页验证已完成，继续本轮请求")
                        return state
                raise WebVerificationRequired("测试等待网页验证超时")

        ConfiguredTextChatDriver._raise_if_logged_out = check_with_handoff

    class AcceptanceBrowser(ZhihuBrowser):
        def publish_story(self, story, question_url=None, max_wait=60):
            qid = self._extract_question_id()
            if not qid and question_url:
                match = re.search(r"/question/(\d+)", question_url)
                qid = match.group(1) if match else ""
            if not qid:
                raise RuntimeError("验收无法确认知乎问题，已停止写入")
            response = self.page.request.get(
                f"https://www.zhihu.com/api/v4/questions/{qid}/draft", timeout=15000)
            if response.status == 404:
                previous = ""
            elif response.ok:
                previous = response.json().get("content") or ""
            else:
                raise RuntimeError("无法核对原有草稿，已停止验收写入")
            if re.sub(r"\s|<[^>]*>|&nbsp;", "", previous):
                raise RuntimeError("该问题已有草稿，验收拒绝覆盖；请为本次选用其他问题")
            result = super().publish_story(story, question_url=question_url, max_wait=max_wait)
            if result:
                (out / "draft.json").write_text(json.dumps({
                    "question_id": qid, "question_url": question_url,
                    "saved": True, "chars": len(story)}, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

    browser_pool.register_browser_factory(lambda headless: AcceptanceBrowser(
        user_data_dir=str(profile), storage_state=str(data_dir / "config/browser_state.json"),
        headless=headless))
    result = {"provider": args.provider, "workflow": args.workflow,
              "selection": get_provider_settings(args.provider)["selection"],
              "started_at": time.time(), "passed": False}
    cleanup = False
    try:
        browser = browser_pool.get_browser()
        page = browser.context.new_page()
        observe_account(args.provider, page)
        if args.provider == "Aizex":
            from web_drivers.aizex import enter_chat
            portal = page
            page = enter_chat(browser.context, page)
            if page is not portal:
                portal.close()
        else:
            page.goto(config.WEB_DRIVERS[args.provider]["url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        state = inspect_account(args.provider, browser.context, page)
        result["account"] = state["status"]
        page.close()
        if state["status"] != "ready":
            raise RuntimeError("网页账号未就绪：" + state["status"])
        if args.workflow in ("classic", "clean"):
            browser_pool.close_shared_browser()
            runner = TaskRunner()
            runner._set_state("running", "real web acceptance")
            runner._run_task(_RunSpec(mode="single" if args.workflow == "classic" else "clean", rounds=1))
            status = runner.status()
            result.update(state=status["state"], message=status["message"])
            result["passed"] = status["state"] == "done" and (out / "draft.json").exists()
            # TaskRunner already performs owned chat deletion and closes pages.
            # Cleanup logs and the driver's responsibility ledger remain part
            # of acceptance; success is not inferred from a closed browser.
            log_text = (out / "run.log").read_text(encoding="utf-8")
            cleanup_events = {}
            for line in log_text.splitlines():
                match = re.search(r"(已删除本任务网页会话|本任务会话删除未确认).*id=([^）)]+)", line)
                if match:
                    cleanup_events[match.group(2)] = match.group(1).startswith("已删除")
            cleanup = bool(cleanup_events) and all(cleanup_events.values())
        elif args.workflow == "generation":
            from tools.web_generation_acceptance import run_generation_workflows
            report = run_generation_workflows(out, data_dir,
                                             only=args.checks.split(',') if args.checks else None)
            result["workflows"] = report
            result["passed"] = report["all_passed"]
            cleanup = all(item["cleanup_confirmed"] for item in report["items"].values())
        elif args.workflow == "business":
            from tools.web_interaction_acceptance import run_interaction_workflows
            report = run_interaction_workflows(out, data_dir,
                                              only=args.checks.split(',') if args.checks else None)
            result["workflows"] = report
            result["passed"] = report["all_passed"]
            cleanup = all(item["cleanup_confirmed"] for item in report["items"].values())
        else:
            driver = get_driver()
            text = driver.generate("请用一句中文描写雨后的花园，不超过三十个字。", reuse_session=False)
            result["short_reply"] = {"chars": len(text or ""), "passed": bool(text)}
            raw = driver.generate('请把下面两种水果分类为JSON数组，只输出JSON：[苹果、香蕉]。每项含name和category两个字段。')
            from core.story_text import extract_json_block
            structured = extract_json_block(raw)
            result["structured"] = {"passed": isinstance(structured, list) and len(structured) == 2}
            driver.delete_current_session()
            driver.close_session()
            cleanup = not driver._session_owned and not driver._pending_sessions
            result["passed"] = result["short_reply"]["passed"] and result["structured"]["passed"]
        result["cleanup_confirmed"] = cleanup
        result["passed"] = result["passed"] and cleanup
    except Exception as exc:
        logging.exception("真实网页验收失败")
        result.update(error=type(exc).__name__, message=str(exc))
    finally:
        try:
            reset_driver()
            browser_pool.close_shared_browser()
        finally:
            result["ended_at"] = time.time()
            (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            record_validation(args.provider, args.workflow, result["selection"],
                              "passed" if result["passed"] else "failed", str(out / "result.json"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
