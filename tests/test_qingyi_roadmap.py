"""清漪推进目录的结构与依赖边界。"""
import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import qingyi_roadmap


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        self.catalog = qingyi_roadmap.catalog()
        self.ids = {task["id"] for task in self.catalog}

    def test_catalog_includes_the_new_context_memory_and_governance_tasks(self):
        self.assertEqual(len(self.catalog), 72)
        self.assertEqual(len(self.ids), 72)
        for prefix, count in {"A": 8, "M": 8, "S": 8, "R": 8, "C": 6,
                              "G": 4, "L": 4, "V": 6, "D": 7, "U": 6, "Q": 7}.items():
            self.assertEqual(sum(task["id"].startswith(prefix) for task in self.catalog), count)

    def test_dependencies_are_present_and_acyclic(self):
        result = qingyi_roadmap.validate_catalog(self.catalog)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["missing_dependencies"], [])
        self.assertEqual(result["cycles"], [])

    def test_stage_two_five_does_not_wait_for_ripple(self):
        by_id = {task["id"]: task for task in self.catalog}
        self.assertNotIn("R01", by_id["C01"]["dev_requires"])
        self.assertNotIn("R07", by_id["C02"]["release_requires"])
        self.assertEqual(by_id["C01"]["dev_requires"], ["A01", "A07"])
        self.assertEqual(by_id["C02"]["dev_requires"], ["A01", "A07"])

    def test_development_overrides_do_not_erode_acceptance_requirements(self):
        by_id = {task["id"]: task for task in self.catalog}
        self.assertEqual(by_id["R01"]["dev_requires"], ["A05", "M01", "A07"])
        self.assertIn("A04", by_id["R01"]["acceptance_requires"])
        self.assertIn("M05", by_id["R01"]["acceptance_requires"])

    def test_new_delivery_tasks_have_tonight_ready_entries(self):
        by_id = {task["id"]: task for task in self.catalog}
        self.assertTrue(by_id["D01"]["implementation"])
        self.assertTrue(by_id["A02"]["implementation"])
        # R04 已建成可执行契约（受限一跳与根源去重）；R08 二跳/向量边契约已建成（默认全关，人工评审）。
        self.assertTrue(by_id["R04"]["implementation"])
        self.assertTrue(by_id["R08"]["implementation"])

    def test_projection_separates_engineering_observation_and_authorization(self):
        task = qingyi_roadmap.project_task("D01", {
            "tasks": {"D01": {"implementation": "implemented", "engineering": "not_verified",
                              "organic": "not_observed", "authorization": "not_required"}}
        })
        self.assertEqual(task["engineering"], "not_verified")
        self.assertFalse(task["release_ready"])
        self.assertTrue(task["dev_ready"])
        authorization = qingyi_roadmap.project_task("C06", {
            "tasks": {"C06": {"implementation": "planned", "engineering": "not_verified",
                              "organic": "not_observed", "authorization": "manual_review"}}
        })
        self.assertFalse(authorization["release_ready"])

    def test_unstarted_tasks_show_distinct_progress_instead_of_one_label(self):
        blocked = qingyi_roadmap.project_task("C03")
        self.assertEqual("planned", blocked["implementation"])
        self.assertEqual("not_verified", blocked["engineering"])
        self.assertEqual("not_observed", blocked["organic"])
        self.assertEqual("manual_review", blocked["authorization"])
        plain = qingyi_roadmap.project_task("A03")
        self.assertEqual("not_required", plain["organic"])
        self.assertEqual("not_required", plain["authorization"])

    def test_duplicate_ids_and_unknown_stages_are_reported_not_hidden(self):
        broken = [dict(task) for task in self.catalog]
        broken.append(dict(broken[0]))
        broken.append(dict(broken[1], id="ZZ1", stage="stage-typo"))
        result = qingyi_roadmap.validate_catalog(broken)
        self.assertFalse(result["ok"])
        self.assertEqual(result["duplicate_ids"], [broken[0]["id"]])
        self.assertEqual(result["unknown_stages"], ["stage-typo"])


if __name__ == "__main__":
    unittest.main()
