import re
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import uvicorn
from docx import Document

from evoreport.api import create_app
from evoreport.config import Settings

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    PlaywrightError = None
    expect = None
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "浏览器测试需要 Playwright")
class BrowserEndToEndTests(unittest.TestCase):
    def test_daily_report_flow_and_word_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "plain-project"
            project.mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]

            settings = Settings("127.0.0.1", port, root / "test.db", ark_api_key="", artifact_path=root)
            app = create_app(settings)
            server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=port, log_level="error", access_log=False))
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()
            base_url = f"http://127.0.0.1:{port}"
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(0.05)
                self.assertTrue(server.started, "临时测试服务未启动")

                with sync_playwright() as playwright:
                    try:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                    except PlaywrightError as exc:
                        self.skipTest(f"本机缺少可启动的 Microsoft Edge：{exc}")
                    try:
                        context = browser.new_context(accept_downloads=True)
                        page = context.new_page()
                        page.goto(base_url)
                        expect(page.locator("#service-status")).to_have_text("运行正常")

                        page.locator('[data-view="daily"]').click()
                        page.locator("#smart-parse").click()
                        expect(page.locator("#toast")).to_contain_text("请先输入一段工作描述")
                        self.assertEqual([], app.state.store.list_reports())

                        page.locator('[data-view="projects"]').click()
                        page.locator("#project-name").fill("浏览器测试项目")
                        page.locator("#project-path").fill(str(project))
                        page.locator("#project-type").select_option("plain")
                        page.locator("#project-form button[type=submit]").click()
                        expect(page.locator("#project-list")).to_contain_text("浏览器测试项目")

                        page.locator('[data-view="daily"]').click()
                        expect(page.locator("#project-options")).to_contain_text("浏览器测试项目")
                        page.locator("#report-date").fill("2026-10-06")
                        page.locator("#daily-completed").fill("完成本地联调")
                        page.locator("#daily-tomorrow").fill("补充回归测试")
                        submit = page.locator("#daily-form button[type=submit]")
                        page.route("**/api/reports/daily", lambda route: route.fulfill(status=503, json={"detail": "模拟生成失败"}))
                        submit.click()
                        expect(page.locator("#toast")).to_contain_text("模拟生成失败")
                        expect(submit).to_be_enabled()
                        expect(submit).to_have_text("根据事实生成初稿")
                        self.assertEqual([], app.state.store.list_reports())

                        page.unroute("**/api/reports/daily")
                        submit.click()
                        expect(page.locator("#report-editor")).to_have_value(re.compile("完成本地联调"))
                        page.locator("#report-editor").fill("# 工作日报\n\n## 今日完成\n\n1. 完成本地联调并确认结果\n今天复盘了上线流程")
                        expect(page.locator("#draft-status")).to_have_text("草稿已自动保存")
                        self.assertEqual([], app.state.store.list_evaluation_cases())
                        page.reload()
                        page.locator('[data-view="daily"]').click()
                        expect(page.locator("#report-editor")).to_have_value(re.compile("完成本地联调并确认结果"))
                        expect(page.locator("#draft-status")).to_contain_text("已恢复未确认草稿")

                        page.locator('[data-view="history"]').click()
                        page.locator("#history-list .report-item").first.click()
                        expect(page.locator("#resume-history")).to_be_visible()
                        page.locator("#resume-history").click()
                        expect(page.locator("#report-editor")).to_have_value(re.compile("完成本地联调并确认结果"))
                        page.locator("#confirm-report").click()
                        expect(page.locator("#final-review")).to_be_visible()
                        expect(page.locator("#final-review-issues")).to_contain_text("缺少章节")
                        page.locator("#review-acknowledge").click()
                        expect(page.locator("#draft-status")).to_contain_text("已确认")
                        expect(page.locator("#final-review")).to_be_hidden()
                        expect(page.locator("#report-editor")).to_have_attribute("readonly", "")

                        page.locator('[data-view="history"]').click()
                        report_item = page.locator("#history-list .report-item").first
                        expect(report_item).to_contain_text("2026-10-06")
                        report_item.click()
                        expect(page.locator("#history-preview")).to_contain_text("完成本地联调并确认结果")
                        expect(page.locator("#history-facts")).to_contain_text("手工记录第 1 行 · 未指定项目")
                        with closing(sqlite3.connect(app.state.store.path)) as connection, connection:
                            connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (
                                app.state.store.list_reports("daily")[0]["id"],
                            ))
                        page.locator('[data-view="daily"]').click()
                        page.locator('[data-view="history"]').click()
                        page.locator("#history-list .report-item").first.click()
                        expect(page.locator("#review-label-list [data-review-label-index]")).to_have_count(1)
                        page.locator("#review-label-list [data-review-label-index]").select_option("supported")
                        page.locator('#review-label-list [data-review-scenario="claim"][value="paraphrase"]').check()
                        page.locator("#review-missed-line").select_option("5")
                        page.locator("#review-missed-text").fill("确认结果")
                        page.locator("#review-missed-form button[type=submit]").click()
                        expect(page.locator("#review-missed-list")).to_contain_text("按父项计一次")
                        page.locator('#review-missed-list [data-review-scenario="missed"][value="compound"]').check()
                        expect(page.locator("#review-label-list [data-review-label-index]")).to_have_value("unsupported")
                        page.locator("#review-missed-line").select_option("6")
                        page.locator("#review-missed-text").fill("今天复盘了上线流程")
                        page.locator("#review-missed-form button[type=submit]").click()
                        expect(page.locator("#review-missed-list")).to_contain_text("今天复盘了上线流程")
                        page.locator("#save-review-labels").click()
                        expect(page.locator("#review-benchmark")).to_contain_text("样本不足")
                        expect(page.locator("#review-benchmark")).to_contain_text("复合子句 1 条")
                        expect(page.locator("#review-benchmark")).to_contain_text("复合事项 1 项")
                        expect(page.locator("#review-benchmark")).to_contain_text("同义改写 1 项")
                        expect(page.locator("#review-benchmark")).to_contain_text("今天复盘了上线流程")
                        page.locator('[data-view="evolution"]').click()
                        expect(page.locator("#critic-rule-list")).to_contain_text("标注不足")
                        page.locator("#critic-rule-threshold").fill("70")
                        page.locator("#critic-rule-form button[type=submit]").click()
                        expect(page.locator("#critic-rule-list")).to_contain_text(
                            "Skill v2 · 阈值 70% · 字符重合权重 0%"
                        )
                        expect(page.locator("[data-evaluate-critic='2']")).to_be_disabled()
                        page.locator('[data-view="history"]').click()
                        page.locator("#history-project").select_option(label="浏览器测试项目")
                        page.locator("#history-start").fill("2026-10-06")
                        page.locator("#history-end").fill("2026-10-06")
                        page.locator("#history-filter-form button[type=submit]").click()
                        expect(page.locator("#history-list .report-item")).to_have_count(1)
                        page.locator("#history-start").fill("2026-10-07")
                        page.locator("#history-end").fill("2026-10-07")
                        page.locator("#history-filter-form button[type=submit]").click()
                        expect(page.locator("#history-list .report-item")).to_have_count(0)
                        expect(page.locator("#history-facts")).to_contain_text("选择一份日报")
                        page.locator("#history-filter-clear").click()
                        expect(page.locator("#history-list .report-item")).to_have_count(1)
                        page.locator("#history-list .report-item").first.click()
                        with page.expect_download() as download_info:
                            page.locator("#export-history-word").click()
                        download = download_info.value
                        self.assertTrue(download.suggested_filename.endswith(".docx"))
                        target = root / "download.docx"
                        download.save_as(target)
                        paragraphs = [paragraph.text for paragraph in Document(target).paragraphs]
                        self.assertIn("1. 完成本地联调并确认结果", paragraphs)
                        self.assertEqual(1, len(app.state.store.list_evaluation_cases()))

                        page.locator('[data-view="evolution"]').click()
                        page.locator("#agent-run-list [data-replay-run]").first.click()
                        expect(page.locator("#agent-run-list [data-replay-result]").filter(has_text="回放核验通过")).to_be_visible()
                        expect(page.locator("#dataset-gate")).to_contain_text("尚未建立固定基准")
                        expect(page.locator("#benchmark-coverage")).to_contain_text("事实来源")
                        expect(page.locator("#post-activation-monitor")).to_contain_text("尚无策略切换记录")
                        expect(page.locator("#writer-prompt-list")).to_contain_text("默认写作指令")
                        page.locator("#writer-prompt-addendum").fill("表达简洁，突出结果。")
                        page.locator("#writer-prompt-form button").click()
                        expect(page.locator("#writer-prompt-list")).to_contain_text("表达简洁，突出结果。")
                        expect(page.locator("#writer-prompt-list .stack-item").filter(has_text="v2").locator("[data-activate-prompt]")).to_be_disabled()
                        expect(page.locator("#writer-prompt-list .stack-item").filter(has_text="v2").locator("[data-evaluate-prompt]")).to_be_disabled()
                        expect(page.locator("#create-benchmark")).to_be_disabled()
                        page.locator("#run-evolution").click()
                        expect(page.locator("#evaluation-explanation")).to_contain_text("未运行")
                        expect(page.locator("#memory-list")).to_contain_text("还没有记忆")
                        page.locator("#memory-key").select_option("numbered")
                        page.locator("#memory-boolean").select_option("false")
                        page.locator("#personal-memory-form button").click()
                        expect(page.locator("#memory-list")).to_contain_text("编号列表")
                        page.locator("#memory-project").select_option(app.state.store.list_projects()[0]["id"])
                        page.locator("#memory-note").fill("长期维护本地联调流程")
                        page.locator("#project-memory-form button").click()
                        expect(page.locator("#memory-list")).to_contain_text("长期维护本地联调流程")
                        page.once("dialog", lambda dialog: dialog.accept("启用"))
                        page.locator("#memory-list .stack-item").filter(has_text="编号列表").locator("[data-correct-memory]").click()
                        expect(page.locator("#memory-list .stack-item").filter(has_text="编号列表")).to_contain_text("启用")
                        page.locator("#memory-list .stack-item").filter(has_text="长期维护本地联调流程").locator("[data-remove-memory]").click()
                        expect(page.locator("#memory-list")).not_to_contain_text("长期维护本地联调流程")
                        project_id = app.state.store.list_projects()[0]["id"]
                        for day in ("2026-10-07", "2026-10-08"):
                            source = {"kind": "daily", "projects": [{"id": project_id}],
                                      "manual_completed": "", "manual_problems": "接口必须兼容旧版本",
                                      "manual_tomorrow": "", "facts": []}
                            report = app.state.store.create_report("daily", day, source, "# 初稿", 1)
                            app.state.store.confirm_report(report["id"], "# 终稿")
                            with closing(sqlite3.connect(app.state.store.path)) as connection, connection:
                                connection.execute(
                                    "UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],)
                                )
                        page.locator('[data-view="history"]').click()
                        page.locator('[data-view="evolution"]').click()
                        expect(page.locator("#memory-suggestion-list")).to_contain_text("接口必须兼容旧版本")
                        expect(page.locator("#memory-suggestion-list")).to_contain_text("遇到的问题第 1 行")
                        page.once("dialog", lambda dialog: dialog.accept(""))
                        page.locator("#memory-suggestion-list [data-accept-suggestion]").click()
                        expect(page.locator("#memory-list")).to_contain_text("归档建议采纳")
                        expect(page.locator("#memory-suggestion-list")).not_to_contain_text("接口必须兼容旧版本")
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                server_thread.join(timeout=5)
                self.assertFalse(server_thread.is_alive(), "临时测试服务未能停止")


if __name__ == "__main__":
    unittest.main()
