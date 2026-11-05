"""Real web-model acceptance for non-publishing AI workflows."""
from __future__ import annotations
import json
from pathlib import Path
import time

def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)

def _cleanup(driver):
    if driver is None: return False
    driver.delete_current_session()
    driver.close_session()
    return not driver._session_owned and not driver._pending_sessions

def _samples(runtime):
    from applications.zhihu_story.author_profiler import iter_collected_stories
    return list(iter_collected_stories(str(Path(runtime) / "data" / "collected_stories.jsonl")))

def _run_item(result, name, out, fn):
    from web_drivers import create_driver
    started, driver, raw = time.time(), create_driver(), []
    payload = {"raw": raw}
    try:
        passed, schema, extra = fn(driver, raw); payload.update(extra or {})
        result["items"][name] = {"passed": bool(passed), "schema": schema,
                                  "elapsed": time.time() - started,
                                  "evidence": _write(Path(out) / f"{name}.json", payload)}
    except Exception as exc:
        payload.update(error=type(exc).__name__, message=str(exc))
        result["items"][name] = {"passed": False, "schema": None,
                                  "elapsed": time.time() - started,
                                  "evidence": _write(Path(out) / f"{name}.json", payload)}
    finally:
        result["items"][name]["cleanup_confirmed"] = _cleanup(driver)
        result["items"][name]["passed"] &= result["items"][name]["cleanup_confirmed"]

def run_interaction_workflows(out: Path, runtime: Path, only=None) -> dict:
    names = ("screen", "score", "originality", "reply", "profile_general", "profile_author")
    selected = list(names if only is None else only)
    unknown = [x for x in selected if x not in names]
    if unknown: raise ValueError(f"未知验收项：{unknown}")
    out, runtime = Path(out), Path(runtime); out.mkdir(parents=True, exist_ok=True)
    stories = _samples(runtime)
    if len(stories) < 3: raise RuntimeError("交互验收至少需要3篇采集样本")
    candidates = [{"index": i + 1, "title": s.get("title") or "", "answer": s.get("answer") or ""} for i, s in enumerate(stories[:3])]
    result = {"started_at": time.time(), "included_checks": selected, "items": {}}

    def screen(driver, raw):
        import story_scoring
        from core.story_text import extract_json_block
        original = story_scoring._web_llm_generate
        def call(prompt, what="请求"):
            text = driver.generate(prompt, reuse_session=False); raw.append({"what": what, "response": text}); return text
        story_scoring._web_llm_generate = call
        try: selected_result = story_scoring.screen_question_pool(candidates)
        finally: story_scoring._web_llm_generate = original
        parsed = extract_json_block(raw[-1]["response"]) if raw else None; items = parsed.get("items") if isinstance(parsed, dict) else None
        valid = (isinstance(parsed, dict) and isinstance(items, list) and {int(x.get("index", -1)) for x in items} == {x["index"] for x in candidates} and all(isinstance(x.get("keep"), bool) for x in items))
        return valid, "dict.items[index,keep]", {"parsed": parsed, "selected": selected_result}

    def score(driver, raw):
        import story_scoring
        original = story_scoring._web_llm_generate
        def call(prompt, what="请求"):
            text = driver.generate(prompt, reuse_session=False); raw.append(text); return text
        story_scoring._web_llm_generate = call
        data = [{"index": i + 1, "title": s.get("title") or "", "story": s.get("answer") or "", "url": ""} for i, s in enumerate(stories[:2])]
        try: scored = story_scoring.score_stories(data)
        finally: story_scoring._web_llm_generate = original
        return bool(raw and all(x.get("score_detail") and x.get("score", 0) > 0 for x in scored)), "score_detail+score", {"scored": scored}

    def originality(driver, raw):
        from core import originality
        import story_scoring
        original = story_scoring._web_llm_generate
        def call(prompt, what="请求"):
            text = driver.generate(prompt, reuse_session=False); raw.append(text); return text
        story_scoring._web_llm_generate = call
        try: audit = originality.audit_originality("验收题目", stories[0]["answer"], stories[1]["answer"], enable_llm=True)
        finally: story_scoring._web_llm_generate = original
        return bool(raw and audit.get("llm_detail")), "llm_detail", {"audit": audit}

    def reply(driver, raw):
        from applications.zhihu_story.reply_task import compose_reply
        generate = driver.generate
        def record(prompt, *args, **kwargs):
            text = generate(prompt, *args, **kwargs)
            raw.append({'response': text, 'url': driver._page_instance().url,
                        'session_id': driver._session_id})
            return text
        driver.generate = record
        value = compose_reply(driver, "这段结局让我很意外", "验收题目", stories[0]["answer"], max_retry=1)
        return bool(value.get("ok")), "ok=true", {"reply": value}

    def profile_general(driver, raw):
        from applications.zhihu_story import author_profiler as ap
        original = ap._call_profile_llm_web
        def call(prompt):
            text = driver.generate(prompt, reuse_session=False); raw.append(text); return text
        ap._call_profile_llm_web = call
        try: profile = ap.profile_general(out_dir=str(Path(out) / "profiles"), max_stories=3)
        finally: ap._call_profile_llm_web = original
        sig = (profile or {}).get("signature") or {}
        return bool(raw and sig.get("opening_patterns") and sig.get("narrative_techniques")), "signature.opening_patterns+narrative_techniques", {"profile": profile}

    def profile_author(driver, raw):
        from applications.zhihu_story import author_profiler as ap
        author = next(a for a in {s.get("author") for s in stories} if sum(s.get("author") == a for s in stories) >= 2)
        original = ap._call_profile_llm_web
        def call(prompt):
            text = driver.generate(prompt, reuse_session=False); raw.append(text); return text
        ap._call_profile_llm_web = call
        try: profile = ap.profile_author(author, out_dir=str(Path(out) / "profiles"))
        finally: ap._call_profile_llm_web = original
        sig = (profile or {}).get("signature") or {}
        return bool(raw and sig.get("opening_patterns") and sig.get("narrative_techniques")), "signature.opening_patterns+narrative_techniques", {"author": author, "profile": profile}

    funcs = locals()
    for name in selected: _run_item(result, name, out, funcs[name])
    result["ended_at"] = time.time(); result["all_passed"] = bool(selected) and all(result["items"][n]["passed"] for n in selected)
    _write(out / "interaction-result.json", result)
    return result
