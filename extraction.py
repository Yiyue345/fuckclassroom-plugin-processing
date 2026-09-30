from __future__ import annotations

import hashlib
import html
import io
import math
import posixpath
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fuckclassroom.classroom.client import ClassroomClient, ClassroomClientError, CourseDetail, Lesson, LessonResource
from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PluginServiceError


class ExtractionError(RuntimeError):
    pass


ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], None]


@dataclass(frozen=True)
class ExtractionResult:
    course_id: str
    lesson_id: str
    output_dir: Path
    transcript_path: Path
    ppt_text_path: Path
    combined_path: Path
    summary_path: Path | None
    warnings: list[str]


@dataclass
class _PptImageCandidate:
    order: int
    name: str
    data: bytes
    digest: str
    perceptual_hash: int | None
    width: int
    height: int
    information_score: float


class LessonExtractionService:
    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        classroom: ClassroomClient | None = None,
        service_lookup: Callable[[str, object | None], object | None] | None = None,
    ) -> None:
        self.config = config or AppConfig()
        self.classroom = classroom or ClassroomClient(self.config)
        self._service_lookup = service_lookup
        # Optional feature services are injected by Plugin Runtime. Keeping these
        # attributes allows focused unit tests to inject lightweight fakes without
        # importing sibling plugin implementations.
        self.ai = None
        self.transcription = None
        self._legacy_ocr = None

    def _optional_service(self, key: str, legacy: object | None = None):
        if self._service_lookup is None:
            return legacy
        return self._service_lookup(key, None)

    def _transcription_service(self):
        return self._optional_service("transcription_service", self.transcription)

    def _ai_summary_service(self):
        return self._optional_service("ai_summary_service", self.ai)

    def _ocr_handlers(self):
        if self._service_lookup is None:
            return self._ocr_image, self._can_ocr
        service = self._optional_service("ocr_service")
        if service is None:
            return None, None
        return service.ocr_image, service.can_ocr

    def extract_lesson(
        self,
        course_id: str,
        lesson_id: str,
        run_ocr: bool = True,
        progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        _report(progress, 5, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        work_dir = self._lesson_dir(self.config.downloads_dir, detail, lesson)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        transcript_path = output_dir / "transcript.md"
        transcript_text = ""
        video_resource = _find_resource(lesson, "video")
        platform_transcript = _find_resource(lesson, "transcript")
        need_platform_fallback = not bool(video_resource and video_resource.url)

        if video_resource is not None and video_resource.url:
            _report(progress, 10, "检测到课堂录像，优先使用本地语音转写")
            _task_log(
                progress,
                "检测到课堂录像：平台转写不作为主文本，本次优先使用本地 Whisper。",
                "info",
            )
            try:
                def asr_progress(value: int, message: str) -> None:
                    _report(progress, 10 + int(value * 0.42), message)

                transcript_path = self._transcribe_lesson(
                    detail,
                    lesson,
                    progress=asr_progress,
                )
                transcript_text = transcript_path.read_text(encoding="utf-8")
            except PluginServiceError as exc:
                warning = f"本地语音转写失败，尝试平台转写兜底：{exc}"
                warnings.append(warning)
                _task_log(progress, warning, "warning")
                need_platform_fallback = True

        if need_platform_fallback and platform_transcript is not None:
            _report(progress, 12, "正在检查平台转写兜底")
            try:
                transcript_file, transcript_from_cache = self._download_export(
                    course_id, lesson_id, "transcript", work_dir
                )
                if transcript_from_cache:
                    _report(progress, 16, "已复用本地平台转写")
                else:
                    _report(progress, 16, "平台转写已下载")
                candidate = extract_docx_text(transcript_file)
                usable, reason = _platform_transcript_is_usable(
                    candidate,
                    course_title=detail.course.title,
                    lesson=lesson,
                )
                if usable:
                    transcript_text = candidate
                    transcript_path.write_text(
                        _section("平台转写（兜底）", transcript_text), encoding="utf-8"
                    )
                    _task_log(progress, f"平台转写兜底通过质量检查：{reason}", "warning")
                else:
                    warning = f"平台转写已拒绝，改用空转写占位：{reason}"
                    warnings.append(warning)
                    _task_log(progress, warning, "warning")
            except (
                ClassroomClientError,
                OSError,
                zipfile.BadZipFile,
                ET.ParseError,
            ) as exc:
                warning = f"平台转写兜底不可用：{exc}"
                warnings.append(warning)
                _task_log(progress, warning, "warning")
        elif need_platform_fallback and platform_transcript is None:
            warning = "本课次没有可用录像，平台也未提供可信转写。"
            warnings.append(warning)
            _task_log(progress, warning, "warning")

        if not transcript_text.strip():
            transcript_path.write_text(
                _section("语音转写", "（本课次暂无可用的可信转写）"), encoding="utf-8"
            )

        ppt_text_path = output_dir / "ppt_ocr.md"
        ppt_text = ""
        ppt_warnings: list[str] = []
        ppt_resource = _find_resource(lesson, "ppt")

        if ppt_resource is not None:
            _report(progress, 55, "正在检查课件")
            try:
                ppt_file, ppt_from_cache = self._download_export(
                    course_id, lesson_id, "ppt", work_dir
                )
                if ppt_from_cache:
                    _report(progress, 58, "已复用本地课件")
                else:
                    _report(progress, 58, "课件已下载")

                ocr_stats = {"cache_hits": 0, "inferences": 0}
                ocr_stats_lock = threading.Lock()

                ocr_image, can_ocr_check = self._ocr_handlers()

                def ocr_callback(image_bytes: bytes, image_name: str) -> str:
                    if ocr_image is None:
                        return ""
                    return ocr_image(
                        image_bytes,
                        image_name,
                        stats=ocr_stats,
                        stats_lock=ocr_stats_lock,
                    )

                if run_ocr and can_ocr_check is None:
                    ppt_warnings.append("OCR 插件未启用，已跳过课件图片识别。")
                can_ocr = bool(
                    run_ocr
                    and can_ocr_check is not None
                    and can_ocr_check(ppt_warnings)
                )
                selected_ocr_callback = ocr_callback if can_ocr else None

                def ppt_progress(done: int, total: int, image_name: str) -> None:
                    if total <= 0:
                        _report(progress, 64, "正在读取课件文本")
                        return
                    percent = 60 + int((done / total) * 25)
                    _report(progress, percent, f"正在 OCR 课件图片 {done}/{total}：{image_name}")

                _report(
                    progress,
                    60,
                    f"正在分析课件图片并准备 OCR（Worker {self.config.ocr_workers}）",
                )
                _task_log(
                    progress,
                    "PPT OCR 配置："
                    f"engine={self.config.ocr_engine}, workers={self.config.ocr_workers}, "
                    f"onnx_threads={self.config.ocr_onnx_threads}, max_side={self.config.ocr_max_side}, "
                    f"angle_cls={str(self.config.ocr_use_angle_cls).lower()}, "
                    f"cache={str(self.config.ocr_cache_enabled).lower()}, "
                    f"max_images={self.config.ocr_max_images}, dedup=true",
                    "debug",
                )
                cancel_check = getattr(progress, "raise_if_cancelled", None)
                ppt_text, ocr_warnings = extract_pptx_text(
                    ppt_file,
                    selected_ocr_callback,
                    self.config.ocr_max_images,
                    progress=ppt_progress,
                    ocr_workers=self.config.ocr_workers,
                    cancel_check=cancel_check if callable(cancel_check) else None,
                    event_log=lambda message, level="info": _task_log(progress, message, level),
                )
                if can_ocr:
                    with ocr_stats_lock:
                        cache_hits = ocr_stats["cache_hits"]
                        inferences = ocr_stats["inferences"]
                    _task_log(
                        progress,
                        f"OCR 运行统计：缓存命中 {cache_hits} 张，实际推理 {inferences} 张",
                        "info",
                    )
                ppt_warnings.extend(ocr_warnings)
                for warning in ocr_warnings:
                    _task_log(progress, warning, "warning")
            except (
                ClassroomClientError,
                OSError,
                zipfile.BadZipFile,
                ET.ParseError,
            ) as exc:
                warning = f"课件提取失败：{exc}"
                ppt_warnings.append(warning)
                _task_log(progress, warning, "warning")
        else:
            warning = "本课次没有可用 PPT，已跳过课件文字提取。"
            ppt_warnings.append(warning)
            _task_log(progress, warning, "warning")

        warnings.extend(ppt_warnings)
        warning_text = "\n".join(f"- {item}" for item in ppt_warnings)
        ppt_body = ppt_text
        if warning_text:
            ppt_body = f"{ppt_body}\n\n## 提醒\n{warning_text}".strip()
        ppt_text_path.write_text(_section("课件 OCR", ppt_body), encoding="utf-8")

        _report(progress, 88, "正在合并视频转写与 PPT 文本")
        combined_text = "\n\n".join(
            [
                f"# {detail.course.title} - {lesson.title}",
                transcript_path.read_text(encoding="utf-8"),
                ppt_text_path.read_text(encoding="utf-8"),
            ]
        )
        if warnings:
            combined_text += "\n\n## 提取提醒\n" + "\n".join(
                f"- {item}" for item in warnings
            )
        combined_path = output_dir / "combined.md"
        combined_path.write_text(combined_text, encoding="utf-8")

        # combined.md has just changed, so any previous summary is stale until it is
        # regenerated from the new video transcript + PPT text.
        summary_path = output_dir / "summary.md"
        summary_path.unlink(missing_ok=True)
        has_source_text = bool(transcript_text.strip() or ppt_text.strip())
        ai_summary = self._ai_summary_service()
        if self.config.auto_summarize_after_extract and ai_summary is not None:
            if not has_source_text:
                _task_log(
                    progress,
                    "视频转写与 PPT 文本均为空，已跳过自动 AI 总结。",
                    "warning",
                )
            elif self.config.ai_api_key:
                _report(progress, 92, "正在将视频转写与 PPT 文本发送给 AI 总结")
                _task_log(
                    progress,
                    "已合并视频转写与 PPT/OCR 文本，开始自动生成 AI 课程总结。",
                    "info",
                )
                try:
                    summary = ai_summary.summarize_course_text(combined_text)
                    summary_path.write_text(
                        _section("AI 课程总结", summary),
                        encoding="utf-8",
                    )
                    _task_log(progress, "AI 自动总结已写入 summary.md", "info")
                except (PluginServiceError, OSError, ValueError) as exc:
                    warning = f"AI 自动总结失败：{exc}"
                    warnings.append(warning)
                    _task_log(progress, warning, "warning")
            else:
                _task_log(
                    progress,
                    "未配置 AI API Key，已跳过自动总结；视频转写与 PPT 文本仍已正常保存。",
                    "warning",
                )

        if summary_path.exists():
            _report(progress, 100, "文字提取与 AI 总结完成")
        else:
            _report(progress, 100, "文字提取完成")
        return ExtractionResult(
            course_id=course_id,
            lesson_id=lesson_id,
            output_dir=output_dir,
            transcript_path=transcript_path,
            ppt_text_path=ppt_text_path,
            combined_path=combined_path,
            summary_path=summary_path if summary_path.exists() else None,
            warnings=warnings,
        )

    def transcribe_lesson(
        self,
        course_id: str,
        lesson_id: str,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> Path:
        _report(progress, 5, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        return self._transcribe_lesson(detail, lesson, progress=progress, force=force)

    def _transcribe_lesson(
        self,
        detail: CourseDetail,
        lesson: Lesson,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> Path:
        video_resource = _find_resource(lesson, "video")
        if video_resource is None or not video_resource.url:
            raise PluginServiceError("本课次没有可用的课堂录像")

        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        output_dir.mkdir(parents=True, exist_ok=True)
        cache_path = output_dir / "local_transcript.txt"
        transcription = self._transcription_service()
        if transcription is None:
            raise PluginServiceError("本地语音转写插件未启用")
        result = transcription.transcribe_url(
            video_resource.url,
            cache_path,
            progress=progress,
            force=force,
            duration_hint_seconds=lesson.duration_seconds,
        )
        transcript_path = output_dir / "transcript.md"
        transcript_path.write_text(
            _section("本地语音转写", result.text), encoding="utf-8"
        )
        return transcript_path

    def summarize_lesson(
        self,
        course_id: str,
        lesson_id: str,
        progress: ProgressCallback | None = None,
    ) -> Path:
        _report(progress, 10, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        combined_path = output_dir / "combined.md"
        if not combined_path.exists():
            raise ExtractionError("请先提取文字，再生成总结")
        _report(progress, 35, "正在读取合并文本")
        combined_text = combined_path.read_text(encoding="utf-8")
        ai_summary = self._ai_summary_service()
        if ai_summary is None:
            raise ExtractionError("AI 总结插件未启用")
        _report(progress, 55, "正在请求 AI 生成总结")
        summary = ai_summary.summarize_course_text(combined_text)
        summary_path = output_dir / "summary.md"
        _report(progress, 90, "正在写入总结")
        summary_path.write_text(_section("AI 课程总结", summary), encoding="utf-8")
        _report(progress, 100, "AI 总结完成")
        return summary_path

    def get_existing_outputs(self, course_id: str, lesson_id: str) -> dict[str, str]:
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        result: dict[str, str] = {}
        for name in ("transcript.md", "ppt_ocr.md", "combined.md", "summary.md"):
            path = output_dir / name
            if path.exists():
                result[name] = path.read_text(encoding="utf-8")
        return result

    def _download_export(self, course_id: str, lesson_id: str, kind: str, work_dir: Path) -> tuple[Path, bool]:
        target = self.classroom.get_lesson_export_target(course_id, lesson_id, kind)
        if target.exists:
            return target.path, True
        exported = self.classroom.download_lesson_export(course_id, lesson_id, kind)
        return exported.saved_path, exported.from_cache

    def _ocr_image(
        self,
        image_bytes: bytes,
        image_name: str,
        *,
        stats: dict[str, int] | None = None,
        stats_lock: threading.Lock | None = None,
    ) -> str:
        if self._legacy_ocr is None:
            raise ExtractionError("OCR 插件未启用")
        return self._legacy_ocr.ocr_image(
            image_bytes,
            image_name,
            stats=stats,
            stats_lock=stats_lock,
        )

    def _can_ocr(self, warnings: list[str]) -> bool:
        if self._legacy_ocr is None:
            warnings.append("OCR 插件未启用，已跳过课件图片识别。")
            return False
        return self._legacy_ocr.can_ocr(warnings)

    @staticmethod
    def _lesson_dir(root: Path, detail: CourseDetail, lesson: Lesson) -> Path:
        return root / _safe_path(detail.course.title) / _safe_path(lesson.title)


def extract_docx_text(path: Path) -> str:
    texts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        ]
        for name in names:
            if not any(part in name for part in ("document", "header", "footer")):
                continue
            root = ET.fromstring(archive.read(name))
            texts.extend(_xml_texts(root))
    return "\n".join(line for line in texts if line.strip()).strip()


def _platform_transcript_is_usable(
    text: str,
    *,
    course_title: str,
    lesson: Lesson,
) -> tuple[bool, str]:
    candidate = text.strip()
    if not candidate:
        return False, "内容为空"

    body = candidate
    if course_title.strip():
        body = body.replace(course_title.strip(), " ")
    if lesson.title.strip():
        body = body.replace(lesson.title.strip(), " ")
    body = re.sub(r"授课老师\s*[：:]\s*[\u4e00-\u9fffA-Za-z·.]{1,40}", " ", body)
    body = re.sub(r"\b\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b", " ", body)
    body = re.sub(r"第\s*\d+\s*[-—~～至到]\s*\d+\s*节", " ", body)
    body = re.sub(r"(?:课程名称|课程名|教师|老师|上课时间)\s*[：:]?", " ", body)
    meaningful = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", body)

    duration_seconds = max(0, int(getattr(lesson, "duration_seconds", 0) or 0))
    duration_minutes = duration_seconds / 60 if duration_seconds else 0.0
    minimum_chars = 120
    if duration_minutes:
        minimum_chars = max(120, min(1000, int(duration_minutes * 8)))

    if len(meaningful) < minimum_chars:
        return (
            False,
            f"去除课程/教师/日期/节次元信息后仅剩 {len(meaningful)} 个有效字符，"
            f"低于本课次最低可信阈值 {minimum_chars}",
        )
    return True, f"有效正文约 {len(meaningful)} 个字符"


def extract_pptx_text(
    path: Path,
    ocr_image: Callable[[bytes, str], str] | None,
    max_images: int = 120,
    progress: Callable[[int, int, str], None] | None = None,
    *,
    ocr_workers: int = 1,
    cancel_check: CancelCheck | None = None,
    event_log: Callable[[str, str], None] | None = None,
) -> tuple[str, list[str]]:
    lines: list[str] = []
    warnings: list[str] = []
    image_jobs: list[tuple[int, str, bytes]] = []

    _check_cancel(cancel_check)
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            (
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ),
            key=_slide_sort_key,
        )
        for index, name in enumerate(slide_names, start=1):
            _check_cancel(cancel_check)
            root = ET.fromstring(archive.read(name))
            slide_text = "\n".join(_xml_texts(root)).strip()
            if slide_text:
                lines.append(f"## 第 {index} 页 XML 文本\n{slide_text}")

        all_media_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/media/") and _is_image_name(name)
        )
        image_names = _referenced_ppt_images(archive, slide_names, all_media_names)
        if image_names and ocr_image is None:
            warnings.append(f"课件中检测到 {len(image_names)} 张被幻灯片引用的图片，未执行 OCR。")
            return "\n\n".join(lines).strip(), warnings

        if ocr_image is not None and image_names:
            image_jobs = _prepare_ocr_image_jobs(
                archive,
                image_names,
                max_images=max_images,
                cancel_check=cancel_check,
                event_log=event_log,
                total_media_count=len(all_media_names),
            )

    if ocr_image is not None and image_jobs:
        results, ocr_warnings = _run_ocr_jobs(
            image_jobs,
            ocr_image,
            max_workers=max(1, min(int(ocr_workers), len(image_jobs))),
            progress=progress,
            cancel_check=cancel_check,
        )
        warnings.extend(ocr_warnings)
        for index, image_name, text in sorted(results, key=lambda item: item[0]):
            if text:
                lines.append(f"## 图片 {index}: {Path(image_name).name}\n{text}")

    if not lines and not warnings:
        warnings.append("课件中没有检测到可提取的文本或图片")
    return "\n\n".join(lines).strip(), warnings


def _referenced_ppt_images(
    archive: zipfile.ZipFile,
    slide_names: list[str],
    all_media_names: list[str],
) -> list[str]:
    available = set(all_media_names)
    referenced: list[str] = []
    seen: set[str] = set()

    for slide_name in slide_names:
        rel_name = f"ppt/slides/_rels/{Path(slide_name).name}.rels"
        if rel_name not in archive.namelist():
            continue
        try:
            root = ET.fromstring(archive.read(rel_name))
        except ET.ParseError:
            continue
        for relationship in root.iter():
            rel_type = str(relationship.attrib.get("Type") or "")
            target = str(relationship.attrib.get("Target") or "")
            if not rel_type.endswith("/image") or not target:
                continue
            normalized = posixpath.normpath(posixpath.join("ppt/slides", target))
            if normalized in available and normalized not in seen:
                seen.add(normalized)
                referenced.append(normalized)

    return referenced if referenced else all_media_names


def _prepare_ocr_image_jobs(
    archive: zipfile.ZipFile,
    image_names: list[str],
    *,
    max_images: int,
    cancel_check: CancelCheck | None,
    event_log: Callable[[str, str], None] | None,
    total_media_count: int,
) -> list[tuple[int, str, bytes]]:
    exact_seen: dict[str, _PptImageCandidate] = {}
    candidates: list[_PptImageCandidate] = []
    exact_duplicates = 0

    for order, name in enumerate(image_names):
        _check_cancel(cancel_check)
        data = archive.read(name)
        digest = hashlib.sha256(data).hexdigest()
        if digest in exact_seen:
            exact_duplicates += 1
            continue
        fingerprint = _image_fingerprint(order, name, data, digest)
        exact_seen[digest] = fingerprint
        candidates.append(fingerprint)

    groups: list[_PptImageCandidate] = []
    similar_duplicates = 0
    for candidate in candidates:
        _check_cancel(cancel_check)
        match_index = _find_similar_image_group(groups, candidate)
        if match_index is None:
            groups.append(candidate)
            continue
        similar_duplicates += 1
        current = groups[match_index]
        if candidate.information_score > current.information_score:
            candidate.order = current.order
            groups[match_index] = candidate

    groups.sort(key=lambda item: item.order)
    before_limit = len(groups)
    limited = groups[: max(1, int(max_images))]
    if event_log is not None:
        event_log(
            "PPT 图片分析："
            f"媒体 {total_media_count} 张，幻灯片引用 {len(image_names)} 张，"
            f"完全重复移除 {exact_duplicates} 张，高度相似移除 {similar_duplicates} 张，"
            f"去重后 {before_limit} 张，最终 OCR {len(limited)} 张",
            "info",
        )
        if before_limit > len(limited):
            event_log(
                f"去重后仍超过 OCR 上限 {max_images}，已按幻灯片首次引用顺序保留前 {len(limited)} 张。",
                "warning",
            )

    return [
        (index, candidate.name, candidate.data)
        for index, candidate in enumerate(limited, start=1)
    ]


def _image_fingerprint(
    order: int,
    name: str,
    data: bytes,
    digest: str,
) -> _PptImageCandidate:
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            gray = image.convert("L")
            entropy = float(gray.entropy())
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_mean = float(ImageStat.Stat(edges).mean[0])
            resized = gray.resize((9, 8))
            get_flattened_data = getattr(resized, "get_flattened_data", None)
            pixels = list(get_flattened_data() if callable(get_flattened_data) else resized.getdata())
            phash = 0
            bit = 0
            for row in range(8):
                offset = row * 9
                for column in range(8):
                    if pixels[offset + column] > pixels[offset + column + 1]:
                        phash |= 1 << bit
                    bit += 1
            information_score = (
                math.log1p(max(1, width * height)) * 2.2
                + entropy * 4.0
                + edge_mean / 12.0
                + math.log1p(max(1, len(data)))
            )
            return _PptImageCandidate(
                order,
                name,
                data,
                digest,
                phash,
                width,
                height,
                information_score,
            )
    except Exception:
        return _PptImageCandidate(
            order,
            name,
            data,
            digest,
            None,
            0,
            0,
            math.log1p(max(1, len(data))),
        )


def _find_similar_image_group(
    groups: list[_PptImageCandidate],
    candidate: _PptImageCandidate,
) -> int | None:
    if candidate.perceptual_hash is None or not candidate.width or not candidate.height:
        return None
    candidate_ratio = candidate.width / candidate.height
    for index, current in enumerate(groups):
        if current.perceptual_hash is None or not current.width or not current.height:
            continue
        current_ratio = current.width / current.height
        if abs(math.log(candidate_ratio / current_ratio)) > 0.035:
            continue
        distance = (candidate.perceptual_hash ^ current.perceptual_hash).bit_count()
        if distance <= 3:
            return index
    return None


def _slide_sort_key(name: str) -> tuple[int, str]:
    match = re.search(r"slide(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 10**9, name)


def _run_ocr_jobs(
    jobs: list[tuple[int, str, bytes]],
    ocr_image: Callable[[bytes, str], str],
    *,
    max_workers: int,
    progress: Callable[[int, int, str], None] | None,
    cancel_check: CancelCheck | None,
) -> tuple[list[tuple[int, str, str]], list[str]]:
    total = len(jobs)
    completed = 0
    results: list[tuple[int, str, str]] = []
    warning_items: list[tuple[int, str]] = []
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ppt-ocr")
    pending: dict[Future[str], tuple[int, str, bytes]] = {}
    iterator = iter(jobs)
    aborted = False

    def submit_next() -> bool:
        try:
            item = next(iterator)
        except StopIteration:
            return False
        index, name, image_bytes = item
        future = executor.submit(ocr_image, image_bytes, Path(name).name)
        pending[future] = (index, name, image_bytes)
        return True

    for _ in range(max_workers):
        if not submit_next():
            break

    try:
        while pending:
            _check_cancel(cancel_check)
            done, _ = wait(tuple(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                index, name, _ = pending.pop(future)
                display_name = Path(name).name
                try:
                    text = future.result().strip()
                except Exception as exc:
                    text = ""
                    warning = f"{display_name} OCR 失败：{exc}"
                    warning_items.append((index, warning))
                    display_name = f"{display_name}（失败）"
                else:
                    results.append((index, name, text))

                completed += 1
                if progress:
                    progress(completed, total, display_name)
                _check_cancel(cancel_check)
                submit_next()
    except BaseException:
        aborted = True
        for future in pending:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=not aborted, cancel_futures=True)

    warnings = [message for _, message in sorted(warning_items, key=lambda item: item[0])]
    if jobs and not results and len(warnings) >= len(jobs):
        first_error = warnings[0] if warnings else "未知 OCR 后端错误"
        raise ExtractionError(
            f"课件 OCR 全部失败（{len(jobs)}/{len(jobs)}）：{first_error}"
        )
    return results, warnings


def _xml_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}t") and element.text:
            text = html.unescape(element.text.strip())
            if text:
                texts.append(text)
    return texts


def _find_lesson(detail: CourseDetail, lesson_id: str) -> Lesson:
    lesson = next((item for item in detail.lessons if item.id == lesson_id), None)
    if lesson is None:
        raise ExtractionError("未找到指定课次")
    return lesson


def _find_resource(lesson: Lesson, kind: str) -> LessonResource | None:
    return next(
        (
            resource
            for resource in lesson.resources
            if resource.kind == kind and resource.is_downloadable
        ),
        None,
    )


def _safe_path(value: str) -> str:
    cleaned = "".join(char if char not in r'\/:*?"<>|' else "_" for char in value).strip()
    return cleaned[:80] or "未命名"


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _section(title: str, body: str) -> str:
    return f"# {title}\n\n{body.strip() or '（无内容）'}\n"


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)


def _task_log(progress: ProgressCallback | None, message: str, level: str = "info") -> None:
    if progress is None:
        return
    logger = getattr(progress, "log", None)
    if callable(logger):
        logger(message, level)


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check:
        cancel_check()
