"""Batch lesson processing orchestration.

Runs in one host TaskManager job so transcription models are not instantiated
for multiple lessons at the same time. Existing per-lesson services remain the
single source of truth for transcription, OCR, caching and output files.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from fuckclassroom.core.plugins import PluginServiceError

BatchMode = Literal["extract", "retranscribe"]
MAX_BATCH_LESSONS = 30


class BatchSelectionError(ValueError):
    pass


class BatchProcessingError(RuntimeError):
    pass


class BatchGuard:
    """Permit one active bulk workload across the whole processing plugin."""

    def __init__(self):
        self._lock = threading.Lock()
        self._busy = False

    def acquire(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._lock:
            self._busy = False


@dataclass(frozen=True)
class BatchLesson:
    id: str
    title: str


def select_lessons(detail, requested: Sequence[str], *, mode: BatchMode) -> list[BatchLesson]:
    """Validate IDs against the actual course detail; never trust client titles."""
    if mode not in ("extract", "retranscribe"):
        raise BatchSelectionError("不支持的批量处理模式")
    if not requested or len(requested) > MAX_BATCH_LESSONS:
        raise BatchSelectionError(f"每批请选择 1～{MAX_BATCH_LESSONS} 个课次")
    if not all(isinstance(item, str) and item and len(item) <= 128 for item in requested):
        raise BatchSelectionError("课次编号不合法")
    if len(set(requested)) != len(requested):
        raise BatchSelectionError("不能重复选择同一课次")
    eligible = {lesson.id: lesson for lesson in detail.lessons}
    result: list[BatchLesson] = []
    for lesson_id in requested:
        lesson = eligible.get(lesson_id)
        if lesson is None:
            raise BatchSelectionError("所选课次不属于当前课程，请刷新页面重新选择")
        if lesson.is_live:
            raise BatchSelectionError(f"「{lesson.title}」正在直播，暂不能批量处理")
        if mode == "retranscribe" and not any(
            resource.kind == "video" and resource.is_downloadable
            for resource in lesson.resources
        ):
            raise BatchSelectionError(f"「{lesson.title}」没有可用录播，不能重新转写")
        result.append(BatchLesson(id=lesson.id, title=lesson.title))
    return result


class LessonProgress:
    """Map a lesson's 0–100 progress into the parent job and preserve cancel/log APIs."""

    def __init__(self, parent, index: int, total: int, title: str):
        self.parent = parent
        self.index = index
        self.total = total
        self.title = title
        self._last = 0

    def __call__(self, percent: int, message: str):
        self.raise_if_cancelled()
        current = max(self._last, max(0, min(100, int(percent))))
        self._last = current
        aggregate = int(((self.index + current / 100) / self.total) * 100)
        self.parent(aggregate, f"[{self.index + 1}/{self.total}] {self.title}：{message}")
        detail = getattr(self.parent, "detail", None)
        if callable(detail):
            detail(current, f"当前课次：{self.title} · {message}")

    def raise_if_cancelled(self):
        check = getattr(self.parent, "raise_if_cancelled", None)
        if callable(check):
            check()

    def log(self, message: str, level: str = "info"):
        reporter = getattr(self.parent, "log", None)
        if callable(reporter):
            reporter(f"[{self.index + 1}/{self.total}] {self.title}：{message}", level)

    def detail(self, percent: int, message: str):
        report = getattr(self.parent, "detail", None)
        if callable(report):
            report(max(0, min(100, int(percent))), f"{self.title}：{message}")

    def clear_detail(self):
        clear = getattr(self.parent, "clear_detail", None)
        if callable(clear):
            clear()


def run_batch(
    course_id: str,
    lessons: Sequence[BatchLesson],
    mode: BatchMode,
    extraction_service,
    reporter,
    *,
    run_ocr: bool = False,
) -> str:
    """Continue after ordinary per-lesson errors, but always honor cancellation."""
    if not lessons:
        raise BatchSelectionError("未选择课次")
    total = len(lessons)
    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    reporter(0, f"已开始批量处理，共 {total} 个课次")
    for index, lesson in enumerate(lessons):
        reporter.raise_if_cancelled()
        scoped = LessonProgress(reporter, index, total, lesson.title)
        scoped.log("开始处理")
        scoped(0, "准备处理")
        try:
            if mode == "extract":
                extraction_service.extract_lesson(
                    course_id, lesson.id, run_ocr=run_ocr, progress=scoped,
                )
            else:
                extraction_service.regenerate_transcript(
                    course_id, lesson.id, progress=scoped,
                )
        except Exception as exc:
            # Cooperative cancellation may arrive from transcribe_url during
            # a lesson. Do not swallow it as a recoverable per-lesson failure.
            scoped.raise_if_cancelled()
            message = str(exc).strip()[:300] or type(exc).__name__
            scoped.log(f"处理失败：{message}", "error")
            failures.append((lesson.title, message))
            scoped(100, f"失败：{message}")
        else:
            scoped.log("处理完成")
            successes.append(lesson.title)
            scoped(100, "完成")
        finally:
            scoped.clear_detail()
    reporter(100, f"批量处理结束：成功 {len(successes)}，失败 {len(failures)}")
    if failures:
        failures_summary = "；".join(f"{title}：{reason}" for title, reason in failures[:3])
        raise BatchProcessingError(
            f"批量处理完成：成功 {len(successes)}/{total}，失败 {len(failures)}；"
            f"失败课次：{failures_summary}。其他成功课次的结果已保存。"
        )
    return f"/courses/{course_id}"
