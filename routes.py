from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal
from fastapi.responses import HTMLResponse, Response

from fuckclassroom.core.plugins import PluginServiceError
from .extraction import ExtractionError
from .batch import BatchSelectionError, select_lessons, run_batch
from fuckclassroom.web.responses import task_started_response


class BatchRequest(BaseModel):
    lesson_ids: list[str] = Field(min_length=1, max_length=30)
    mode: Literal["extract", "retranscribe"] = "extract"


def build_router(context) -> APIRouter:
    services = context.services
    router = APIRouter()
    templates = services.get("templates")
    task_manager = services.get("task_manager")
    extraction_service = services.get("extraction_service")
    classroom_client = services.get("classroom_client")
    batch_guard = services.get("processing_batch_guard")
    auth_service = services.get("auth_service")

    @router.post("/courses/{course_id}/lessons/batch")
    def batch_lessons(request: Request, course_id: str, payload: BatchRequest) -> Response:
        """Validate all IDs before starting one cancellable host task."""
        try:
            detail = classroom_client.get_course_detail(course_id)
            selected = select_lessons(detail, payload.lesson_ids, mode=payload.mode)
        except BatchSelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PluginServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not batch_guard.acquire():
            raise HTTPException(
                status_code=409,
                detail="已有批量转写任务正在执行，请在任务中心查看进度，完成后再提交",
            )

        def worker(progress):
            try:
                ocr_enabled = context.registry.is_enabled("ocr") if payload.mode == "extract" else False
                if payload.mode == "extract" and not ocr_enabled:
                    progress.log("PPT OCR 未启用，仍进行录播转写及原生 PPT 文字提取", "warning")
                return run_batch(
                    course_id, selected, payload.mode, extraction_service,
                    progress, run_ocr=ocr_enabled,
                )
            finally:
                batch_guard.release()

        try:
            name = "批量提取文字" if payload.mode == "extract" else "批量重新语音转写"
            task = task_manager.start(f"{name}（{len(selected)} 个课次）", worker)
        except Exception:
            batch_guard.release()
            raise
        return task_started_response(request, task, f"/courses/{course_id}")

    @router.post("/courses/{course_id}/lessons/{lesson_id}/extract")
    def extract_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            ocr_enabled = context.registry.is_enabled("ocr")
            if not ocr_enabled:
                warning = "PPT OCR 插件未启用：本次仍会提取原生 PPT 文字，但不会识别图片；请到插件管理启用后重新 OCR。"
                report = getattr(progress, "log", None)
                if callable(report):
                    report(warning, "warning")
                progress(1, warning)
            extraction_service.extract_lesson(
                course_id,
                lesson_id,
                run_ocr=ocr_enabled,
                progress=progress,
            )
            return result_url

        task = task_manager.start(
            "提取文字",
            worker,
            auto_open_result=context.config.auto_open_outputs_after_extract,
        )
        return task_started_response(request, task, result_url)

    @router.post("/courses/{course_id}/lessons/{lesson_id}/retranscribe")
    def retranscribe_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.regenerate_transcript(course_id, lesson_id, progress)
            return result_url

        task = task_manager.start("重新语音转写", worker, auto_open_result=False)
        return task_started_response(request, task, result_url)

    @router.post("/courses/{course_id}/lessons/{lesson_id}/reocr")
    def reocr_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.regenerate_ppt_ocr(course_id, lesson_id, progress)
            return result_url

        task = task_manager.start("重新 PPT OCR", worker, auto_open_result=False)
        return task_started_response(request, task, result_url)

    @router.get(
        "/courses/{course_id}/lessons/{lesson_id}/outputs",
        response_class=HTMLResponse,
    )
    def lesson_outputs(
        request: Request,
        course_id: str,
        lesson_id: str,
    ) -> HTMLResponse:
        error = None
        outputs = {}
        ocr_status = {}
        try:
            outputs = extraction_service.get_existing_outputs(course_id, lesson_id)
            ocr_status = extraction_service.get_ocr_status(course_id, lesson_id)
        except (PluginServiceError, ExtractionError) as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "processing/lesson_outputs.html",
            {
                "session": auth_service.get_session_status(),
                "course_id": course_id,
                "lesson_id": lesson_id,
                "outputs": outputs,
                "ocr_status": ocr_status,
                "error": error,
            },
        )

    return router


__all__ = ["build_router"]
