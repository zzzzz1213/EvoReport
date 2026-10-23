import unittest

from evoreport.writing_profile import apply_profile, build_writing_profile


class WritingProfileTests(unittest.TestCase):
    def test_builds_profile_from_confirmed_report_text(self):
        reports = [{"final_text": "## 今日完成\n1. 完成 EFC 对账优化\n\n## 遇到的问题\n- 修复空值\n\n## 明日计划\n1. 完成导出"}] * 4
        profile = build_writing_profile(reports)
        self.assertTrue(profile["ready"])
        self.assertEqual("编号列表", profile["preferred_list"])
        self.assertIn("EFC", profile["project_terms"])

    def test_does_not_apply_profile_until_enough_samples(self):
        profile = build_writing_profile([{"final_text": "## 今日完成\n- 完成记录"}])
        candidate = {"numbered": True, "include_statistics": True, "max_item_length": 100}
        self.assertEqual(candidate, apply_profile(candidate, profile))


if __name__ == "__main__":
    unittest.main()
