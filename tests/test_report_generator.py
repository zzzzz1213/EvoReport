import unittest
from datetime import date

from evoreport.report_generator import generate_daily, generate_weekly, source_fact_records, source_facts


class ReportGeneratorTests(unittest.TestCase):
    def test_daily_uses_git_facts_and_redacts_secrets(self):
        projects = [{
            "name": "Demo",
            "activity": {
                "commits": [{"subject": "完成登录接口", "files": []}],
                "summary": {"commit_count": 1, "files_changed": 2, "insertions": 12, "deletions": 3},
            },
        }]
        report = generate_daily(
            date(2026, 10, 6), projects, "完成联调\napi_key=secret-value", "端口冲突", "补充测试"
        )
        self.assertIn("Demo：完成登录接口", report)
        self.assertIn("api_key=[REDACTED]", report)
        self.assertNotIn("secret-value", report)
        self.assertIn("1 次提交", report)

    def test_weekly_deduplicates_daily_items(self):
        daily = "# 日报\n\n## 今日完成\n1. 完成接口\n\n## 遇到的问题\n1. 端口冲突\n\n## 明日计划\n1. 补充测试\n"
        report = generate_weekly("2026-10-05 至 2026-10-11", [daily, daily])
        self.assertEqual(1, report.count("完成接口"))
        self.assertIn("## 本周完成", report)

    def test_fact_records_keep_aligned_manual_and_git_sources(self):
        projects = [{"name": "项目甲", "activity": {"commits": [
            {"subject": "完成联调", "hash": "a" * 40},
            {"subject": "修复查询异常", "hash": "b" * 40},
        ]}}]
        records = source_fact_records(projects, "- 完成联调\n\n2. 补充回归")
        self.assertEqual(["完成联调", "补充回归", "修复查询异常"], [item["fact"] for item in records])
        self.assertEqual({"source_type": "manual", "line": 1}, {key: value for key, value in records[0].items() if key != "fact"})
        self.assertEqual(3, records[1]["line"])
        self.assertEqual("b" * 40, records[2]["commit_hash"])
        self.assertEqual([item["fact"] for item in records], source_facts(projects, "- 完成联调\n\n2. 补充回归"))


if __name__ == "__main__":
    unittest.main()
