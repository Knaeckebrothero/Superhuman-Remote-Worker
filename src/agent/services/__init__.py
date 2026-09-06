# Services package - helper services for the agent
#
# Lazy-loaded to avoid pulling in heavy dependencies (openai, pypdf, etc.)
# when only a specific service submodule is needed.

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "VisionHelper": ("agent.services.vision_helper", "VisionHelper"),
    "get_vision_helper": ("agent.services.vision_helper", "get_vision_helper"),
    "AudioHelper": ("agent.services.audio_helper", "AudioHelper"),
    "get_audio_helper": ("agent.services.audio_helper", "get_audio_helper"),
    "DocumentRenderer": ("agent.services.document_renderer", "DocumentRenderer"),
    "get_document_renderer": (
        "agent.services.document_renderer",
        "get_document_renderer",
    ),
    "DescriptionCache": ("agent.services.description_cache", "DescriptionCache"),
    "get_description_cache": (
        "agent.services.description_cache",
        "get_description_cache",
    ),
    "EmbeddingService": (
        "shared.runtime.services.embedding_service",
        "EmbeddingService",
    ),
    "get_embedding_service": (
        "shared.runtime.services.embedding_service",
        "get_embedding_service",
    ),
    "RecallStore": ("shared.runtime.services.recall_store", "RecallStore"),
    "MemoryRecord": ("shared.runtime.services.recall_store", "MemoryRecord"),
}

__all__ = list(_LAZY_IMPORTS.keys())


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path, package=__name__)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
