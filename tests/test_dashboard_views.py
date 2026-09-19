# -*- coding: utf-8 -*-
"""看板「统计 / 明细」双视图回归（2026-09-19 布局重构）。

用户诉求：原来 KPI、图表 tab、表格堆在同一列里，统计页与明细页互相打架、观感别扭；
希望能在「统计页 / 详细页」之间切换，并把统计页重新排版得好看一些。
本文件守住结构与接线：视图切换、统计栅格、图表容器不再藏在 tab 里、KPI 六卡、
旧 tab 逻辑彻底移除。
"""
import re
import unittest


class DashboardViewsTest(unittest.TestCase):
    def _src(self, rel):
        with open(rel, encoding="utf-8") as f:
            return f.read()

    def test_html_has_view_switch_and_two_views(self):
        html = self._src("webui/static/index.html")
        for needle in ("dashViewSwitch", 'data-view="stats"', 'data-view="detail"',
                       "dashViewStats", "dashViewDetail", "dashSummary",
                       "dashChartsEmpty"):
            self.assertIn(needle, html, needle)
        # 明细视图默认隐藏（默认看统计）
        self.assertRegex(html, r'id="dashViewDetail" hidden')

    def test_stat_cards_cover_all_charts(self):
        html = self._src("webui/static/index.html")
        for chart in ("chartTrend", "chartFunnel", "chartDist", "chartGenre",
                      "chartTop", "chartScatter"):
            self.assertIn('id="%s"' % chart, html, chart)
        # 六张图各占一个栅格卡（span-N 由 CSS 决定列宽）
        self.assertEqual(len(re.findall('class="stat-card span-', html)), 6)
        for span in ("span-8", "span-4", "span-6", "span-7", "span-5"):
            self.assertIn('class="stat-card ' + span, html, span)

    def test_old_chart_tabs_fully_removed(self):
        html = self._src("webui/static/index.html")
        js = self._src("webui/static/app.js")
        css = self._src("webui/static/style.css")
        self.assertNotIn("chartTabs", html)
        self.assertNotIn("chartPane-", html)
        self.assertNotIn("switchDashTab", js)
        self.assertNotIn("dashChartSection", js)
        self.assertNotIn(".chart-tab", css)
        self.assertNotIn(".dash-chart-pane", css)

    def test_view_switch_persists_and_resizes_charts(self):
        js = self._src("webui/static/app.js")
        self.assertIn("function applyDashView(", js)
        self.assertIn("aqDashView", js)                 # localStorage 记忆
        self.assertIn("c.resize()", js)                 # 隐藏容器切回来要重算尺寸
        # 初始化时必须应用一次视图状态（否则明细容器一直 hidden 或统计不显示）
        self.assertIn("applyDashView(dashView)", js)
        # 视图切换按钮接线
        self.assertIn("#dashViewSwitch .view-btn", js)

    def test_kpi_row_has_six_metrics(self):
        js = self._src("webui/static/app.js")
        block = js[js.index("function renderKpis("):js.index("function activeFilterItems(")]
        for label in ("已发布", "阅读合计", "赞同合计", "评论合计", "赞同率", "篇均互动"):
            self.assertIn(label, block, label)
        self.assertEqual(block.count("kpiCard("), 7)   # 6 张卡 + 1 处空态
        self.assertIn("dashSummary", block)            # 摘要条

    def test_css_defines_stats_grid(self):
        css = self._src("webui/static/style.css")
        for needle in (".view-switch", ".view-btn.sel", ".stats-kpis",
                       ".stats-grid", ".stat-card", ".stat-chart",
                       ".dash-summary", ".stats-empty"):
            self.assertIn(needle, css, needle)
        # 响应式：窄屏收敛为单列，KPI 降为 3 列；1440 以下收紧图表高度
        self.assertIn("@media (max-width: 1180px)", css)
        self.assertIn("@media (max-width: 1440px)", css)
        # 统计视图必须可滚动（应用窗口 1280x820，内容比看板卡高，不能裁掉）
        self.assertIn("#dashViewStats { overflow-y: auto", css)

    def test_auto_test_harness_matches_new_dom(self):
        src = self._src("tools/auto_test.py")
        self.assertIn("#dashViewSwitch .view-btn[data-view='detail']", src)
        self.assertIn("dashViewStats", src)
        self.assertNotIn("#chartTabs", src)


if __name__ == "__main__":
    unittest.main()
