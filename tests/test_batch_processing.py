"""Regress batch selection and host TaskManager progress/cancellation boundaries."""
from __future__ import annotations

import ast
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def actual_batch():
    tree = ast.parse((ROOT / "batch.py").read_text(encoding="utf-8"))
    names = {
        "BatchSelectionError", "BatchProcessingError", "BatchGuard",
        "BatchLesson", "select_lessons", "LessonProgress", "run_batch",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    assert len(selected) == len(names)
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *selected,
    ], type_ignores=[]))
    env = {"dataclass": dataclass, "threading": threading, "MAX_BATCH_LESSONS": 30}
    exec(compile(module, str(ROOT / "batch.py"), "exec"), env)
    return env


batch = actual_batch()


def lesson(id, *, live=False, video=True):
    return SimpleNamespace(
        id=id, title="课次 " + id, is_live=live,
        resources=[SimpleNamespace(kind="video", is_downloadable=video)],
    )


def detail(*lessons):
    return SimpleNamespace(lessons=lessons)


class Reporter:
    def __init__(self):
        self.progress = []
        self.messages = []
        self.details = []
        self.cancelled = False
        self.clear_count = 0

    def __call__(self, percent, message):
        self.raise_if_cancelled()
        self.progress.append(percent)
        self.messages.append(message)

    def log(self, message, level="info"):
        self.raise_if_cancelled()
        self.messages.append((level, message))

    def detail(self, percent, message):
        self.raise_if_cancelled()
        self.details.append((percent, message))

    def clear_detail(self):
        self.clear_count += 1

    def raise_if_cancelled(self):
        if self.cancelled:
            raise Cancelled("cancelled")


class Cancelled(RuntimeError):
    pass


class FakeExtraction:
    def __init__(self, *, fail=""):
        self.calls = []
        self.fail = fail
        self.reporter = None

    def extract_lesson(self, course_id, lesson_id, *, run_ocr, progress):
        self.calls.append(("extract", lesson_id, run_ocr))
        self.reporter = progress
        progress(30, "正在转写")
        progress.log("本课次日志")
        progress(90, "正在合并文本")
        if lesson_id == self.fail:
            raise RuntimeError("模型不可用")
        return SimpleNamespace()

    def regenerate_transcript(self, course_id, lesson_id, progress):
        self.calls.append(("retranscribe", lesson_id))
        progress(35, "Whisper 推理")
        if lesson_id == self.fail:
            raise RuntimeError("转写失败")
        progress(100, "合并文本更新")


class BatchTests(unittest.TestCase):
    def test_select_validates_course_membership_and_bounds(self):
        select = batch["select_lessons"]
        d = detail(lesson("one"), lesson("two"))
        self.assertEqual([x.id for x in select(d, ["two", "one"], mode="extract")], ["two", "one"])
        for requested in ([], ["one", "one"], ["invalid"], ["one"] * 31):
            with self.subTest(requested=requested[:3]), self.assertRaises(batch["BatchSelectionError"]):
                select(d, requested, mode="extract")

    def test_live_lessons_and_no_recording_not_retranscribed(self):
        select = batch["select_lessons"]
        d = detail(lesson("live", live=True), lesson("missing", video=False))
        with self.assertRaisesRegex(batch["BatchSelectionError"], "直播"):
            select(d, ["live"], mode="extract")
        with self.assertRaisesRegex(batch["BatchSelectionError"], "录播"):
            select(d, ["missing"], mode="retranscribe")
        self.assertEqual(select(d, ["missing"], mode="extract")[0].id, "missing")

    def test_one_parent_task_runs_each_selected_lesson_in_order(self):
        reporter, extraction = Reporter(), FakeExtraction()
        selected = [batch["BatchLesson"](id=k, title=k) for k in ("A", "B", "C")]
        url = batch["run_batch"]("course1", selected, "extract", extraction, reporter, run_ocr=True)
        self.assertEqual(url, "/courses/course1")
        self.assertEqual(extraction.calls, [
            ("extract", "A", True), ("extract", "B", True), ("extract", "C", True),
        ])
        self.assertEqual(reporter.progress, sorted(reporter.progress))
        self.assertEqual(reporter.progress[-1], 100)
        self.assertEqual(reporter.clear_count, 3)
        self.assertIn("3/3", str(reporter.messages))

    def test_retranscription_uses_existing_force_path(self):
        reporter, extraction = Reporter(), FakeExtraction()
        batch["run_batch"](
            "course1", [batch["BatchLesson"]("one", "第一课")],
            "retranscribe", extraction, reporter,
        )
        self.assertEqual(extraction.calls, [("retranscribe", "one")])

    def test_failure_continues_and_marks_parent_failed_afterwards(self):
        reporter, extraction = Reporter(), FakeExtraction(fail="B")
        selected = [batch["BatchLesson"](id=k, title=k) for k in ("A", "B", "C")]
        with self.assertRaisesRegex(batch["BatchProcessingError"], "成功 2/3"):
            batch["run_batch"]("c", selected, "extract", extraction, reporter)
        self.assertEqual(len(extraction.calls), 3)
        self.assertTrue(any("模型不可用" in str(msg) for msg in reporter.messages))
        self.assertEqual(reporter.clear_count, 3)

    def test_cancellation_stops_subsequent_lessons(self):
        reporter, extraction = Reporter(), FakeExtraction()
        original = extraction.extract_lesson

        def cancel_mid_lesson(*args, **kwargs):
            reporter.cancelled = True
            kwargs["progress"](45, "取消中")

        extraction.extract_lesson = cancel_mid_lesson
        selected = [batch["BatchLesson"](id=k, title=k) for k in ("A", "B")]
        with self.assertRaises(Cancelled):
            batch["run_batch"]("course", selected, "extract", extraction, reporter)
        self.assertEqual(reporter.clear_count, 1)

    def test_guard_allows_only_one_batch_even_across_threads(self):
        guard = batch["BatchGuard"]()
        self.assertTrue(guard.acquire())
        self.assertFalse(guard.acquire())
        guard.release()
        self.assertTrue(guard.acquire())
        guard.release()

    def test_selector_js_and_route_expose_only_course_scoped_ids(self):
        route = (ROOT / "routes.py").read_text(encoding="utf-8")
        js = (ROOT / "static/batch_processing.js").read_text(encoding="utf-8")
        self.assertIn('"/courses/{course_id}/lessons/batch"', route)
        self.assertIn("select_lessons(detail, payload.lesson_ids", route)
        self.assertIn("data-lesson-row", js)
        self.assertIn("/api/tasks/", js)
        self.assertIn("sessionStorage", js)


if __name__ == "__main__":
    unittest.main()
