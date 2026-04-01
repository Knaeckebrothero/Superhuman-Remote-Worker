# Services package - helper services for the agent
#
# Lazy-loaded to avoid pulling in heavy dependencies (openai, pymupdf, etc.)
# when only a specific service submodule is needed.

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "VisionHelper": (".vision_helper", "VisionHelper"),
    "get_vision_helper": (".vision_helper", "get_vision_helper"),
    "AudioHelper": (".audio_helper", "AudioHelper"),
    "get_audio_helper": (".audio_helper", "get_audio_helper"),
    "DocumentRenderer": (".document_renderer", "DocumentRenderer"),
    "get_document_renderer": (".document_renderer", "get_document_renderer"),
    "DescriptionCache": (".description_cache", "DescriptionCache"),
    "get_description_cache": (".description_cache", "get_description_cache"),
    "EmbeddingService": (".embedding_service", "EmbeddingService"),
    "get_embedding_service": (".embedding_service", "get_embedding_service"),
    "RecallStore": (".recall_store", "RecallStore"),
    "MemoryRecord": (".recall_store", "MemoryRecord"),
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
