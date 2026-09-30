from __future__ import annotations

from pathlib import Path

from fuckclassroom.core.plugins import PluginContext, PluginSpec, SettingsPanel, UIAsset, UISlot


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="processing",
        ui_assets=(
            UIAsset("task_detail_progress.css?v=20260927-1", "style", ("course_detail", "lesson_outputs")),
            UIAsset("task_detail_progress.js?v=20260928-1", pages=("course_detail", "lesson_outputs")),
        ),
        ui_slots=(
            UISlot("classroom.lesson.transcript", "processing/lesson_transcript.html"),
            UISlot("classroom.lesson.actions", "processing/lesson_actions.html"),
        ),
        name="课次处理",
        order=20,
        requires=("classroom",),
        service_factory=setup_services,
        route_factory=build_routes,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        stylesheets=("/plugins/processing/static/processing.css?v=20260922-1",),
        settings_panels=(
            SettingsPanel(
                key="processing",
                label="课次处理",
                template="processing_settings.html",
                order=25,
                checkbox_fields=("auto_open_outputs_after_extract",),
            ),
        ),
    )


__all__ = ["build_plugin"]
