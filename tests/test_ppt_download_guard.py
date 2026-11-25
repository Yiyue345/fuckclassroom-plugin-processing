"""Regression: course PPT download must remain a native browser attachment."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PptDownloadGuardTests(unittest.TestCase):
    def test_guard_marks_ppt_link_as_native_download(self):
        js = (ROOT / "static/ppt_download_guard.js").read_text(encoding="utf-8")
        self.assertIn("'[data-ppt-download]'", js)
        self.assertIn("setAttribute('data-no-pjax', '')", js)
        self.assertIn("setAttribute('download', '')", js)

    def test_course_detail_loads_guard(self):
        plugin = (ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('UIAsset("ppt_download_guard.js?v=1", pages=("course_detail",))', plugin)

    def test_package_has_guard(self):
        builder = (ROOT / "scripts/build_registry_artifact.py").read_text(encoding="utf-8")
        self.assertIn('TREE_DIRS = ("templates", "static")', builder)


if __name__ == "__main__":
    unittest.main()
