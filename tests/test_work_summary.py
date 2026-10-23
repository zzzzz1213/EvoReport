import re
import unittest
from pathlib import Path

from evoreport.work_summary import parse_work_summary


class WorkSummaryTests(unittest.TestCase):
    def test_splits_completed_problem_and_tomorrow(self):
        result = parse_work_summary("今日完成修复导出报错；遇到的问题：接口字段缺失；明天继续优化详情展示")
        self.assertIn("修复导出报错", result["completed"])
        self.assertIn("接口字段缺失", result["problems"])
        self.assertIn("优化详情展示", result["tomorrow"])

    def test_explicit_labels_take_priority_over_action_keywords(self):
        result = parse_work_summary(
            "今日完成：修复问题栏误分类；遇到问题：排查后发现接口字段缺失，已调整映射；"
            "明日计划：继续排查导出异常。"
        )
        self.assertEqual("修复问题栏误分类", result["completed"])
        self.assertEqual("排查后发现接口字段缺失，已调整映射", result["problems"])
        self.assertEqual("继续排查导出异常", result["tomorrow"])

    def test_unlabeled_input_keeps_keyword_fallback(self):
        result = parse_work_summary("排查接口超时；接口字段缺失；明天继续优化导出")
        self.assertEqual("排查接口超时", result["completed"])
        self.assertEqual("接口字段缺失", result["problems"])
        self.assertIn("优化导出", result["tomorrow"])

    def test_five_documented_samples_keep_labeled_sections(self):
        sample_path = Path(__file__).resolve().parents[1] / "测试用随手记样例.md"
        document = sample_path.read_text(encoding="utf-8")
        paragraphs = re.findall(r"^### 晚上・自然语言整理段落[^\n]*\n\s*([^\n]+)", document, re.MULTILINE)
        expected = (
            ("完成接口联调并整理测试用例", "排查、调整", "继续用随手记记录工作"),
            ("白天记录三条随手记", "完成事项顺序与随手记输入顺序不一致", "对比多日初稿"),
            ("用随手记记录当天工作", "不同表述下的拆分结果不稳定", "整理本周已确认日报"),
            ("整理本周已确认日报", "普通项目与 Git 项目事实是否重复计入", "验证数据库备份与恢复流程"),
            ("验证数据库备份功能", "备份预览的报告数量与实际数据是否一致", "根据五天使用记录决定"),
        )
        self.assertEqual(len(expected), len(paragraphs))
        for day, (paragraph, (completed, problem, tomorrow)) in enumerate(zip(paragraphs, expected), start=1):
            with self.subTest(day=day):
                result = parse_work_summary(paragraph)
                self.assertIn(completed, result["completed"])
                self.assertIn(problem, result["problems"])
                self.assertNotIn(problem, result["completed"])
                self.assertIn(tomorrow, result["tomorrow"])


if __name__ == "__main__":
    unittest.main()
