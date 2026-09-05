import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes = []
        self.sections = []
        self.fields = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.classes.extend(attributes.get("class", "").split())
        if tag in {"header", "section"} and attributes.get("data-section"):
            self.sections.append(attributes["data-section"])
        if attributes.get("data-field"):
            self.fields.append(attributes["data-field"])


class DashboardHtmlTests(unittest.TestCase):
    def setUp(self):
        parser = DashboardParser()
        parser.feed((ROOT / "editable-dashboard.html").read_text())
        self.parser = parser

    def test_reference_layout_contains_only_the_four_visible_sections(self):
        self.assertEqual(self.parser.sections, ["header", "quota", "usage", "tasks"])

    def test_legacy_usage_metrics_and_task_heading_are_removed_from_dom(self):
        self.assertNotIn("usage-unit", self.parser.classes)
        self.assertNotIn("stat-grid", self.parser.classes)
        self.assertNotIn("tasks-heading", self.parser.classes)

    def test_reference_layout_keeps_three_compact_task_rows(self):
        self.assertEqual(self.parser.classes.count("task-row"), 3)

    def test_header_uses_account_and_plan_fields_instead_of_brand_status(self):
        self.assertIn("account", self.parser.fields)
        self.assertIn("plan", self.parser.fields)
        self.assertNotIn("brand", self.parser.fields)
        self.assertNotIn("connection", self.parser.fields)


if __name__ == "__main__":
    unittest.main()
