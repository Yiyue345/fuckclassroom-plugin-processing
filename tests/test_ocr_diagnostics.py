"""OCR skip/failure/partial-success diagnostics against the real plugin functions."""

from __future__ import annotations

import ast
import hashlib
import html
import io
import json
import math
import posixpath
import re
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginServiceError(RuntimeError):
    pass


class ExtractionError(RuntimeError):
    pass


def load_actual_functions():
    script = ROOT / "extraction.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names = {
        "_resolve_ocr_availability",
        "extract_pptx_text",
        "_referenced_ppt_images",
        "_prepare_ocr_image_jobs",
        "_image_fingerprint",
        "_find_similar_image_group",
        "_slide_sort_key",
        "_run_ocr_jobs",
        "_xml_texts",
        "_is_image_name",
        "_check_cancel",
        "_atomic_text",
        "_write_ocr_status",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and (node.name in names or node.name == "_PptImageCandidate")
    ]
    assert len(selected) == len(names) + 1
    target = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *selected,
    ], type_ignores=[]))
    env = {
        "hashlib": hashlib,
        "html": html,
        "io": io,
        "_task_log": lambda progress, message, level="info": (
            progress.log(message, level) if progress else None
        ),
        "json": json,
        "math": math,
        "posixpath": posixpath,
        "re": re,
        "tempfile": tempfile,
        "threading": threading,
        "ET": ET,
        "zipfile": zipfile,
        "Path": Path,
        "dataclass": dataclass,
        "datetime": datetime,
        "timezone": timezone,
        "ThreadPoolExecutor": ThreadPoolExecutor,
        "wait": wait,
        "FIRST_COMPLETED": FIRST_COMPLETED,
        "PluginServiceError": PluginServiceError,
        "ExtractionError": ExtractionError,
    }
    exec(compile(target, str(script), "exec"), env)
    return env


def build_pptx(folder, images):
    path = folder / "example.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>原生 XML 正文</a:t></p:sld>',
        )
        for index, data in enumerate(images, 1):
            archive.writestr(f"ppt/media/image{index}.png", data)
    return path


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, percent, message):
        self.events.append(("progress", message))

    def log(self, message, level="info"):
        self.events.append((level, message))


class OcrPreflightTests(unittest.TestCase):
    def setUp(self):
        self.available = load_actual_functions()["_resolve_ocr_availability"]
        self.progress = Recorder()

    def test_disabled_plugin_is_logged_and_user_gets_recovery_steps(self):
        called = []
        messages = []
        valid = self.available(False, lambda w: called.append(True) or True, messages, self.progress)
        self.assertFalse(valid)
        self.assertEqual(called, [])
        self.assertIn("插件未启用", messages[0])
        self.assertIn("插件管理", messages[0])
        self.assertIn("重新 OCR", messages[0])
        self.assertIn("插件未启用", str(self.progress.events))

    def test_enabled_but_service_not_registered_has_different_warning(self):
        messages = []
        self.assertFalse(self.available(True, None, messages, self.progress))
        self.assertIn("服务未注册", messages[0])
        self.assertIn("重新启用", messages[0])

    def test_missing_engine_dependency_is_reported(self):
        messages = []
        def unavailable(warnings):
            warnings.append("已跳过 OCR：缺少 rapidocr-onnxruntime。")
            return False
        self.assertFalse(self.available(True, unavailable, messages, self.progress))
        self.assertIn("rapidocr-onnxruntime", messages[0])
        self.assertIn("rapidocr-onnxruntime", str(self.progress.events))

    def test_engine_false_without_reason_produces_guidance(self):
        messages = []
        self.assertFalse(self.available(True, lambda warnings: False, messages, self.progress))
        self.assertIn("OCR 引擎不可用", messages[0])

    def test_engine_check_exception_is_reported_not_silently_skipped(self):
        messages = []
        def broken(warnings):
            raise RuntimeError("OCR worker crashed")
        self.assertFalse(self.available(True, broken, messages, self.progress))
        self.assertIn("RuntimeError", messages[0])
        self.assertIn("OCR worker crashed", messages[0])
        self.assertTrue(any(level == "error" for level, _ in self.progress.events))

    def test_healthy_ocr_logs_started_not_only_finished(self):
        self.assertTrue(self.available(True, lambda warnings: True, [], self.progress))
        self.assertIn("前置检查通过", str(self.progress.events))


class PptOcrDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.actual = load_actual_functions()["extract_pptx_text"]

    def test_skipped_images_are_counted_but_not_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_pptx(Path(tmp), [b"img-1", b"img-2"])
            diagnostics, logs = {}, []
            text, warnings = self.actual(
                path, None, diagnostics=diagnostics,
                event_log=lambda message, level: logs.append((level, message)),
            )
            self.assertIn("原生 XML 正文", text)
            self.assertEqual(diagnostics["referenced"], 2)
            self.assertEqual(diagnostics["attempted"], 0)
            self.assertTrue(any("未启动" in x for x in warnings))
            self.assertTrue(any("未启动" in x for _, x in logs))

    def test_success_reports_actual_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_pptx(Path(tmp), [b"pngA", b"pngB"])
            seen, diagnostics, logs = [], {}, []
            def worker(image_bytes, name):
                seen.append((image_bytes, name))
                return "识别成功"
            text, warnings = self.actual(
                path, worker, diagnostics=diagnostics, ocr_workers=2,
                event_log=lambda message, level: logs.append(message),
            )
            self.assertEqual(len(seen), 2)
            self.assertEqual(warnings, [])
            self.assertEqual(diagnostics["attempted"], 2)
            self.assertEqual(diagnostics["recognized"], 2)
            self.assertEqual(diagnostics["failed"], 0)
            self.assertIn("图片 1", text)
            self.assertIn("已尝试 2 张", str(logs))

    def test_partial_failure_is_not_counted_as_full_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_pptx(Path(tmp), [b"good", b"bad"])
            diagnostics = {}
            def worker(data, name):
                if data == b"bad":
                    raise RuntimeError("model missing")
                return "识别成功"
            text, warnings = self.actual(
                path, worker, diagnostics=diagnostics, ocr_workers=2,
            )
            self.assertEqual(diagnostics["attempted"], 2)
            self.assertEqual(diagnostics["recognized"], 1)
            self.assertEqual(diagnostics["failed"], 1)
            self.assertEqual(len(warnings), 1)
            self.assertIn("model missing", warnings[0])
            self.assertIn("识别成功", text)

    def test_no_images_is_explicit_not_a_fake_ocr_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_pptx(Path(tmp), [])
            diagnostics, logs = {}, []
            text, warnings = self.actual(
                path, lambda *args: "unexpected", diagnostics=diagnostics,
                event_log=lambda message, level: logs.append(message),
            )
            self.assertIn("原生 XML 正文", text)
            self.assertEqual(diagnostics["referenced"], 0)
            self.assertEqual(diagnostics["attempted"], 0)
            self.assertTrue(any("未发现" in item for item in logs))

    def test_persisted_ocr_status_has_correct_state_and_counts(self):
        status = load_actual_functions()["_write_ocr_status"]
        with tempfile.TemporaryDirectory() as tmp:
            status(Path(tmp), "skipped", "插件未启用", {"referenced": 136, "attempted": 0})
            record = json.loads((Path(tmp) / "ocr_status.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "skipped")
            self.assertEqual(record["referenced"], 136)
            self.assertEqual(record["attempted"], 0)
            self.assertNotIn("cookie", record)


class OcrUiGuidanceTests(unittest.TestCase):
    def test_ocr_state_uses_actual_status_not_always_green(self):
        page = (ROOT / "templates/processing/lesson_outputs.html").read_text(encoding="utf-8")
        self.assertIn('data-ocr-plugin-disabled', page)
        self.assertIn('data-ocr-result-status', page)
        self.assertIn('data-ocr-outcome-badge', page)
        self.assertIn("ocr_state == 'completed'", page)
        self.assertIn('插件管理', page)
        self.assertIn('状态未知', page)
        routes = (ROOT / "routes.py").read_text(encoding="utf-8")
        self.assertIn("get_ocr_status(course_id, lesson_id)", routes)
        self.assertIn("PPT OCR 插件未启用", routes)


if __name__ == "__main__":
    unittest.main()
