"""Regression tests for independent voice transcription / PPT OCR reruns."""

from __future__ import annotations

import ast
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


class PluginServiceError(RuntimeError):
    pass


class ExtractionError(RuntimeError):
    pass


def _load_processing(overrides=None):
    """Exercise the actual class without importing the host runtime."""
    script = ROOT / "extraction.py"
    parsed = ast.parse(script.read_text(encoding="utf-8"))
    selected = [
        node for node in parsed.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {
            "LessonExtractionService", "_atomic_text", "_write_ocr_status", "_section",
            "_safe_path", "_find_lesson", "_find_resource",
        }
    ]
    code = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *selected,
    ], type_ignores=[]))
    env = {
        "Path": Path,
        "json": json,
        "datetime": datetime,
        "timezone": timezone,
        "tempfile": tempfile,
        "threading": threading,
        "PluginServiceError": PluginServiceError,
        "ExtractionError": ExtractionError,
        "_report": lambda progress, value, message: progress(value, message) if progress else None,
        "_task_log": lambda *args: None,
    }
    env.update(overrides or {})
    exec(compile(code, str(script), "exec"), env)
    return env


def _fixtures(root: Path, overrides=None):
    env = _load_processing(overrides)
    lesson = SimpleNamespace(
        id="lesson1",
        title="Lesson One",
        duration_seconds=180,
        resources=[
            SimpleNamespace(kind="video", url="https://example.test/media.mp4", is_downloadable=True),
            SimpleNamespace(kind="ppt", url="https://example.test/slides.pptx", is_downloadable=True),
        ],
    )
    detail = SimpleNamespace(course=SimpleNamespace(title="Demo Course"), lessons=[lesson])
    config = SimpleNamespace(
        outputs_dir=root / "outputs", downloads_dir=root / "downloads",
        ocr_max_images=50, ocr_workers=2,
    )
    service = env["LessonExtractionService"](
        config,
        classroom=SimpleNamespace(get_course_detail=lambda cid: detail),
    )
    target = config.outputs_dir / "Demo Course" / "Lesson One"
    target.mkdir(parents=True)
    for name, content in (
        ("transcript.md", "# 本地语音转写\n\n旧语音\n"),
        ("ppt_ocr.md", "# 课件 OCR\n\n旧图片识别\n"),
        ("combined.md", "旧的合并文本"),
        ("summary.md", "旧 AI 总结"),
    ):
        (target / name).write_text(content, encoding="utf-8")
    return service, target, env


class RegenerateOutputTests(unittest.TestCase):
    def test_transcription_forces_new_model_and_rebuilds_only_dependent_files(self):
        with tempfile.TemporaryDirectory() as directory:
            service, target, _ = _fixtures(Path(directory))
            called = []

            def transcribe(url, output, *, progress, force, duration_hint_seconds):
                called.append((url, force, duration_hint_seconds))
                progress(50, "转写中")
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                Path(output).write_text("新模型的文本", encoding="utf-8")
                return SimpleNamespace(text="新模型的文本")

            service.transcription = SimpleNamespace(transcribe_url=transcribe)
            report = []
            result = service.regenerate_transcript(
                "course1", "lesson1", lambda percent, message: report.append(percent)
            )
            self.assertEqual(result, target / "transcript.md")
            self.assertEqual(called[0][1], True)
            self.assertIn("新模型的文本", (target / "transcript.md").read_text())
            self.assertIn("旧图片识别", (target / "ppt_ocr.md").read_text())
            merged = (target / "combined.md").read_text()
            self.assertIn("新模型的文本", merged)
            self.assertIn("旧图片识别", merged)
            self.assertFalse((target / "summary.md").exists())
            self.assertEqual(report, sorted(report))

    def test_failed_retranscription_preserves_previous_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            service, target, _ = _fixtures(Path(directory))

            def failing(*a, **kw):
                self.assertTrue(kw["force"])
                raise PluginServiceError("whisper model unavailable")

            service.transcription = SimpleNamespace(transcribe_url=failing)
            with self.assertRaises(PluginServiceError):
                service.regenerate_transcript("course1", "lesson1")
            self.assertIn("旧语音", (target / "transcript.md").read_text())
            self.assertEqual((target / "combined.md").read_text(), "旧的合并文本")
            self.assertEqual((target / "summary.md").read_text(), "旧 AI 总结")

    def test_ppt_ocr_bypasses_cache_and_preserves_existing_transcript(self):
        seen = []
        def run_ppt(path, callback, max_images, *, progress, **kwargs):
            text = callback(b"test-image", "slide.png")
            kwargs["diagnostics"].update(referenced=1, attempted=1, recognized=1, failed=0, empty=0)
            progress(1, 1, "slide.png")
            return "图片：" + text, []

        with tempfile.TemporaryDirectory() as directory:
            service, target, _ = _fixtures(Path(directory), {"extract_pptx_text": run_ppt})

            class Ocr:
                def can_ocr(self, warnings): return True
                def ocr_image(self, data, name, *, stats, stats_lock, force):
                    seen.append((data, name, force))
                    stats["inferences"] += 1
                    return "新引擎文字"

            service._legacy_ocr = Ocr()
            ppt_file = Path(directory) / "slides.pptx"
            ppt_file.write_bytes(b"cached PPT for test")
            service._download_export = lambda *a: (ppt_file, True)
            output = service.regenerate_ppt_ocr("course1", "lesson1")
            self.assertEqual(output, target / "ppt_ocr.md")
            self.assertEqual(seen, [(b"test-image", "slide.png", True)])
            self.assertIn("新引擎文字", (target / "ppt_ocr.md").read_text())
            self.assertIn("旧语音", (target / "transcript.md").read_text())
            self.assertIn("新引擎文字", (target / "combined.md").read_text())
            self.assertFalse((target / "summary.md").exists())
            status = json.loads((target / "ocr_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["attempted"], 1)

    def test_ppt_ocr_failure_keeps_old_outputs(self):
        def run_ppt(*args, **kwargs):
            return "", ["slide.png OCR 失败：模型加载异常"]

        with tempfile.TemporaryDirectory() as directory:
            service, target, _ = _fixtures(Path(directory), {"extract_pptx_text": run_ppt})
            service._legacy_ocr = SimpleNamespace(can_ocr=lambda warnings: True)
            service._download_export = lambda *a: (Path(directory) / "slides.pptx", True)
            with self.assertRaisesRegex(ExtractionError, "已保留旧结果"):
                service.regenerate_ppt_ocr("course1", "lesson1")
            self.assertIn("旧图片识别", (target / "ppt_ocr.md").read_text())
            self.assertEqual((target / "combined.md").read_text(), "旧的合并文本")
            self.assertEqual((target / "summary.md").read_text(), "旧 AI 总结")

    def test_routes_and_buttons_use_separate_task_paths(self):
        routes = (ROOT / "routes.py").read_text(encoding="utf-8")
        template = (ROOT / "templates/processing/lesson_outputs.html").read_text(encoding="utf-8")
        for suffix in ("retranscribe", "reocr"):
            self.assertIn('lessons/{lesson_id}/' + suffix, routes)
            self.assertIn('/lessons/{{ lesson_id }}/' + suffix, template)
        self.assertIn('data-task-group="lesson-processing"', template)


if __name__ == "__main__":
    unittest.main()
