from __future__ import annotations

from .extraction import LessonExtractionService
from fuckclassroom.core.plugins import PluginContext


def setup_services(context: PluginContext) -> None:
    services = context.services
    services.add(
        "extraction_service",
        LessonExtractionService(
            context.config,
            classroom=services.get("classroom_client"),
            service_lookup=services.maybe,
        ),
    )


__all__ = ["setup_services"]
