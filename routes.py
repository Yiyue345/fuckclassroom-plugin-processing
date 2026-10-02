from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from fuckclassroom.core.plugins import PluginServiceError
from .extraction import ExtractionError
from fuckclassroom.web.responses import task_started_response


def build_router(context) -> APIRouter:
    services = context.services
    router = APIRouter()
    templates = services.get("templates")
    task_manager = services.get("task_manager")
    extraction_service = services.get("extraction_service")
    auth_service = services.get("auth_service")

    @router.post("/courses/{course_id}/lessons/{lesson_id}/extract")
    def extract_lesson(request: Request, course_id: str, lesson_id: str) -> Response:
        result_url = f"/courses/{course_id}/lessons/{lesson_id}/outputs"

        def worker(progress):
            extraction_service.extract_lesson(
                course_id,
                lesson_id,
                run_ocr=context.registry.is_enabled("ocr"),
                progress=progress,
            )
            return result_url

        task = task_manager.start(
            "提取文字",
            worker,
            auto_open_result=context.config.auto_open_outputs_after_extract,
        )
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
        try:
            outputs = extraction_service.get_existing_outputs(course_id, lesson_id)
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
                "error": error,
            },
        )

    return router


__all__ = ["build_router"]
