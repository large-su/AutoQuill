# 生成给用户看的可读版扫描报告（data/cleanup/report_<date>.md）
import json, os, sys, collections
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "data/cleanup/scan_final.json")
d = json.load(open(SRC, encoding="utf-8"))
c = d["candidates"]
conf = [e for e in c if e["verdict"] == "confirmed"]
other = [e for e in c if e["verdict"] == "story_other"]
tpl = [e for e in other if e.get("story_kind") == "template"]
man = [e for e in other if e.get("story_kind") != "template"]
likely = [e for e in c if e["verdict"] == "likely"]
unrel = [e for e in c if e["verdict"] == "unrelated"]

fp = collections.Counter()
for e in conf:
    for h in e["fp_hits"]:
        fp[h] += 1
months = collections.Counter(e["updated_local"][:7] for e in conf)

lines = []
w = lines.append
w("# DeepSeek 历史会话清理 · 扫描报告")
w("")
w("| 项 | 值 |")
w("| --- | --- |")
w("| 站点 | %s |" % d["site"])
w("| 账号 | %s / %s |" % (d["account"].get("name"), d["account"].get("email")))
w("| 扫描时间 | %s |" % d["generated_at"])
w("| 年龄阈值 | ≥%s 天（cutoff %s） |" % (d["days"], d["cutoff_local"]))
w("| 会话总数 | %d |" % d["total_sessions"])
w("| 30 天前旧会话 | **%d** |" % d["old_sessions"])
w("| 新会话（不动） | %d |" % d["new_sessions"])
w("| 拉了内容的会话 | %d |" % d["content_checked"])
w("")
w("## 判定结果")
w("")
w("| 判定 | 条数 | 默认是否删除 |")
w("| --- | --- | --- |")
w("| 确认 AutoQuill 写故事链路（时间+内容指纹双命中） | **%d** | ✅ 删（delete --yes） |" % len(conf))
w("| 写故事、但提示词非 AutoQuill（模板式长提示词，疑似工具生成） | %d | ⛔ 默认不删（--include-other） |" % len(tpl))
w("| 写故事、用户手写请求（投稿咨询/拆书/小故事等） | %d | ⛔ 默认不删（--include-other） |" % len(man))
w("| 疑似（只有弱指纹、内容未确认） | %d | ⛔ 默认不删（--include-likely） |" % len(likely))
w("| 无关（内容已确认不是写故事链路） | %d | ⛔ 永不删 |" % len(unrel))
w("")
w("> 置顶会话：%d 条（默认跳过，加 --include-pinned 才删）"
  % d.get("pinned_confirmed_count", 0))
w("")
w("## 确认命中的 165 条 · 时间分布")
w("")
for m in sorted(months):
    w("- %s：%d 条" % (m, months[m]))
w("")
w("最早 %s（%.0f 天前）／最新 %s（%.0f 天前）"
  % (min(e["updated_local"] for e in conf), max(e["age_days"] for e in conf),
     max(e["updated_local"] for e in conf), min(e["age_days"] for e in conf)))
w("")
w("## 命中指纹分布（判定依据，可复核）")
w("")
w("| 指纹（AutoQuill 提示词原文） | 命中会话数 |")
w("| --- | --- |")
for k, v in fp.most_common():
    w("| %s | %d |" % (k, v))
w("")
w("## 确认命中清单（全部 %d 条）" % len(conf))
w("")
w("| 最后活动 | 天数 | 标题 | 命中指纹 |")
w("| --- | --- | --- | --- |")
for e in conf:
    w("| %s | %.0f | %s | %s |" % (e["updated_local"], e["age_days"],
                                   e["title"].replace("|", "/"),
                                   ", ".join(e["fp_hits"][:4])))
w("")
w("## 疑似工具生成但提示词未在仓库历史中（%d 条，默认不删）" % len(tpl))
w("")
w("证据：这批会话的首条消息是「你是一位故事创作者。+ ## 格式规范（硬性要求，必须首要严格遵守）"
  "+ ## 创作指引」，与 output/story_4_20260419_133411.md、output/story_17_20260419_231342.md "
  "里保存的提示词一模一样；两份产物文件的落盘时间（2026-04-19 13:34 / 23:13）与账号里"
  "两条会话的最后活动时间（13:33 / 23:13）同分钟对应，判断是同一条生成本地流水线写入的。"
  "但该模板不在本仓库 git 历史里（疑为早期未入库版本），故单独一档。")
w("")
w("| 最后活动 | 标题 |")
w("| --- | --- |")
for e in tpl:
    w("| %s | %s |" % (e["updated_local"], e["title"].replace("|", "/")))
w("")
w("## 用户手写写故事请求（%d 条，默认不删）" % len(man))
w("")
w("| 最后活动 | 标题 |")
w("| --- | --- |")
for e in man:
    w("| %s | %s |" % (e["updated_local"], e["title"].replace("|", "/")))

out = os.path.join(ROOT, "data/cleanup/report_%s.md"
                   % d["generated_at"][:10].replace("-", ""))
open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("已写出:", out, "行数", len(lines))
print("confirmed", len(conf), "template", len(tpl), "manual", len(man),
      "likely", len(likely), "unrelated", len(unrel))
