# Porting Fessi Vision/Audio Functionality to Graph-RAG Workspace

This document outlines the plan for adding vision and audio capabilities to the Graph-RAG workspace agent, based on the patterns implemented in the Advanced-LLM-Chat (Fessi) backend.

## Problem Statement

The Graph-RAG workspace agent may use either multimodal or text-only models. When working with documents (PDF, PPTX, DOCX) and images, we need to handle both scenarios:

- **Multimodal models**: Can receive rendered page screenshots directly alongside extracted text
- **Text-only models**: Need AI-generated descriptions of visual content since they can't process images

Documents often contain charts, diagrams, flowcharts, and figures that are critical for understanding requirements and compliance rules.

## Current State

| Capability | Status | Implementation |
|------------|--------|----------------|
| PDF text extraction | ✅ | `pdfplumber.extract_text()` in `pdf_utils.py` |
| DOCX text extraction | ✅ | Langchain loaders in `document_tools.py` |
| PPTX text extraction | ✅ | Langchain loaders in `document_tools.py` |
| Page-based PDF reading | ✅ | `read_file(path, page_start, page_end)` |
| PDF metadata | ✅ | `get_document_info()` |
| Image file support | ❌ | None |
| Document page rendering | ❌ | None |
| Vision model integration | ❌ | None |
| Multimodal config flag | ❌ | None |
| Description caching | ❌ | None |
| Audio transcription | ❌ | None |

### Relevant Existing Files

- `src/tools/workspace_tools.py` - File operations including `read_file`
- `src/tools/pdf_utils.py` - PDF reading with `PDFReader` class
- `src/tools/document_tools.py` - Document extraction using langchain loaders
- `src/tools/context.py` - `ToolContext` for dependency injection
- `config/defaults.yaml` - Default agent configuration

## Core Concept

The `read_file` tool will be enhanced to support visual content from multiple document types:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           read_file tool                                │
│  - path: str                                                            │
│  - page_start/page_end: Optional[int]                                   │
│  - describe: Optional[str]  ← "What does this chart show?"              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              ┌─────────┐    ┌───────────┐    ┌─────────┐
              │   PDF   │    │ PPTX/DOCX │    │  Image  │
              └────┬────┘    └─────┬─────┘    └────┬────┘
                   │               │               │
                   ▼               ▼               ▼
         ┌─────────────────────────────────────────────────┐
         │              DocumentRenderer                    │
         │  - render_pdf_page() → PNG bytes                │
         │  - render_pptx_slide() → PNG bytes              │
         │  - render_docx_page() → PNG bytes               │
         └─────────────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────┴───────────────┐
                    │      Check: model.multimodal  │
                    └───────────────┬───────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
┌──────────────────────────┐              ┌──────────────────────────┐
│   Multimodal = true      │              │   Multimodal = false     │
│                          │              │                          │
│ Return:                  │              │ Return:                  │
│ - Extracted text         │              │ - Extracted text         │
│ - Rendered screenshots   │              │ - AI descriptions of     │
│   (base64 in message)    │              │   visual content         │
└──────────────────────────┘              └──────────────────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────┐
                                          │    VisionHelper      │
                                          │  (separate model)    │
                                          │  + DescriptionCache  │
                                          └──────────────────────┘
```

## Supported Formats

| Format | Text Extraction | Page Rendering | Notes |
|--------|-----------------|----------------|-------|
| **PDF** | pdfplumber | pdf2image (poppler) | Page-by-page rendering |
| **PPTX** | python-pptx | python-pptx + Pillow | Slide-by-slide rendering |
| **DOCX** | python-docx | docx2pdf + pdf2image | Convert to PDF first, then render |
| **Images** | N/A | Direct read | PNG, JPG, JPEG, GIF, WebP, BMP, TIFF |

## Fessi Reference Implementation

The Fessi backend provides the vision helper pattern we'll adapt:

### Key Source Files (Fessi)

| File | Purpose |
|------|---------|
| `backend/services/vision_helper.py` | Vision model client with `describe_image()` and `analyze_document_page()` |
| `backend/services/file_retrieval_service.py` | Orchestrates content retrieval by file type |
| `backend/services/cache/description_cache.py` | Database-backed cache for vision descriptions |
| `backend/services/audio_handler.py` | Whisper transcription (API + local fallback) |
| `backend/services/document_handler.py` | PDF text extraction and page rendering |
| `backend/services/image_handler.py` | Image/document preparation for LLM |
| `backend/services/tools/file_tools.py` | `get_file_content` tool exposed to agent |

---

## Fessi Implementation Analysis

This section provides detailed analysis of the Fessi codebase to guide porting.

### Architecture Overview

```
Fessi Backend Architecture
==========================

┌─────────────────────────────────────────────────────────────────────┐
│                        API Layer (files.py)                         │
│  POST /upload  │  GET /{file_id}  │  GET /{file_id}/text  │  DELETE │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    FileRetrievalService                              │
│  get_content(file_id, query, pages, model_receives_images)          │
│  Returns: FileContentResult(text, images, description, error)       │
└─────────────────────────────────────────────────────────────────────┘
                    │                           │
          ┌─────────┴─────────┐       ┌─────────┴─────────┐
          ▼                   ▼       ▼                   ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ DocumentHandler  │ │  ImageHandler    │ │  AudioHandler    │
│ - extract_pdf    │ │ - read_base64    │ │ - transcribe     │
│ - render_pages   │ │ - prepare_llm    │ │ - local/API      │
└──────────────────┘ └──────────────────┘ └──────────────────┘
          │                   │
          └─────────┬─────────┘
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       VisionHelper                                   │
│  describe_image(image_data, mime_type, prompt)                      │
│  analyze_document_page(page_image, mime_type, query)                │
└─────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     DescriptionCache                                 │
│  SHA256(file_id + query) → PostgreSQL table                         │
└─────────────────────────────────────────────────────────────────────┘
```

### VisionHelper (`backend/services/vision_helper.py`)

The core vision service using AsyncOpenAI:

```python
# Fessi Pattern
class VisionHelper:
    def __init__(self):
        self.api_key = os.getenv("VISION_API_KEY", os.getenv("OPENAI_API_KEY"))
        self.api_base = os.getenv("VISION_BASE_URL", os.getenv("OPENAI_BASE_URL"))
        self.model = os.getenv("VISION_MODEL", "gpt-4o-mini")
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.api_base)

    async def describe_image(
        self,
        image_data: Union[bytes, str],
        mime_type: str,
        prompt: Optional[str] = None
    ) -> str:
        """Generate description of an image."""
        # Convert bytes to base64 if needed
        if isinstance(image_data, bytes):
            base64_data = base64.b64encode(image_data).decode('utf-8')
        else:
            base64_data = image_data

        default_prompt = (
            "Describe this image in detail, including all visible text, "
            "objects, colors, layout..."
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or default_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"}
                    }
                ]
            }],
            max_tokens=1000
        )
        return response.choices[0].message.content

    async def analyze_document_page(
        self,
        page_image: Union[bytes, str],
        mime_type: str,
        query: Optional[str] = None
    ) -> str:
        """Analyze a document page with structured extraction."""
        # Similar to describe_image but with document-specific prompt
        # Extracts: text, tables, charts, layout
        # max_tokens=2000 for detailed analysis
        ...

# Singleton access
_vision_helper: Optional[VisionHelper] = None

def get_vision_helper() -> VisionHelper:
    global _vision_helper
    if _vision_helper is None:
        _vision_helper = VisionHelper()
    return _vision_helper
```

### DocumentHandler (`backend/services/document_handler.py`)

PDF processing with pdfplumber and pdf2image:

```python
# Fessi Constants
MAX_PDF_PAGES = 20
SUPPORTED_DOCUMENT_TYPES = {
    'application/pdf': 'pdf',
    'text/plain': 'txt',
    'text/markdown': 'md',
}

def extract_pdf_text(file_path: Path) -> Optional[str]:
    """Extract text from PDF using pdfplumber."""
    try:
        with pdfplumber.open(file_path) as pdf:
            text_parts = []
            for i, page in enumerate(pdf.pages[:MAX_PDF_PAGES]):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")
            return "\n\n".join(text_parts) if text_parts else None
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return None

def render_pdf_pages(
    file_path: Path,
    output_dir: Path,
    file_id: str
) -> list[Path]:
    """Render PDF pages as PNG images."""
    from pdf2image import convert_from_path

    images = convert_from_path(
        file_path,
        first_page=1,
        last_page=MAX_PDF_PAGES,
        dpi=150,              # Balance quality/size
        fmt='png'
    )

    output_paths = []
    for i, image in enumerate(images, 1):
        output_path = output_dir / f"{file_id}_page_{i}.png"
        image.save(output_path, 'PNG')
        output_paths.append(output_path)

    return output_paths

def process_pdf(file_path: Path, file_id: str) -> dict:
    """Orchestrate PDF processing: extract text + render pages."""
    output_dir = file_path.parent

    # Extract text → {file_id}_text.txt
    text = extract_pdf_text(file_path)
    if text:
        text_file = output_dir / f"{file_id}_text.txt"
        text_file.write_text(text)

    # Render pages → {file_id}_page_1.png, {file_id}_page_2.png, ...
    page_paths = render_pdf_pages(file_path, output_dir, file_id)

    return {
        "text": text,
        "page_count": len(page_paths),
        "page_paths": page_paths
    }
```

### ImageHandler (`backend/services/image_handler.py`)

Base64 encoding and LLM-ready formatting:

```python
SUPPORTED_IMAGE_TYPES = {
    'image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'
}

def read_image_as_base64(file_id: str) -> Optional[tuple[str, str]]:
    """Read image file and return (base64_data, mime_type)."""
    file_path = find_file_by_id(file_id)
    if not file_path:
        return None

    mime_type = mimetypes.guess_type(file_path)[0] or 'image/png'

    with open(file_path, 'rb') as f:
        image_data = f.read()

    base64_data = base64.b64encode(image_data).decode('utf-8')
    return (base64_data, mime_type)

def prepare_image_for_llm(
    file_id: str,
    mime_type: Optional[str] = None
) -> Optional[dict]:
    """Prepare image in LLM-ready format."""
    result = read_image_as_base64(file_id)
    if not result:
        return None

    base64_data, detected_mime = result
    mime = mime_type or detected_mime

    return {
        'type': 'image_url',
        'image_url': {
            'url': f"data:{mime};base64,{base64_data}"
        }
    }

def prepare_document_for_llm(
    file_id: str,
    mime_type: str,
    name: str
) -> Optional[dict]:
    """Prepare document with text + optional page images."""
    result = {"text": None, "images": []}

    # Get extracted text
    text = get_extracted_text(file_id)
    if text:
        result["text"] = text

    # Get rendered page images (for PDFs)
    if mime_type == 'application/pdf':
        page_paths = get_pdf_page_paths(file_id)
        for page_path in page_paths:
            img_content = prepare_image_for_llm(page_path.stem)
            if img_content:
                result["images"].append(img_content)

    return result
```

### DescriptionCache (`backend/services/cache/description_cache.py`)

Database-backed caching:

```python
class DescriptionCache:
    """Cache for vision-generated descriptions."""

    def _make_key(self, file_id: str, query: Optional[str] = None) -> str:
        """Generate SHA256 cache key."""
        key_data = {"file_id": file_id, "query": query or ""}
        return hashlib.sha256(
            json.dumps(key_data, sort_keys=True).encode()
        ).hexdigest()

    async def get(
        self,
        file_id: str,
        query: Optional[str] = None
    ) -> Optional[str]:
        """Retrieve cached description."""
        key = self._make_key(file_id, query)
        return self.db.get_cache_entry(key)

    async def set(
        self,
        file_id: str,
        description: str,
        query: Optional[str] = None
    ):
        """Store description in cache."""
        key = self._make_key(file_id, query)
        self.db.set_cache_entry(key, file_id, description, query)

    async def delete_by_file(self, file_id: str) -> int:
        """Delete all cache entries for a file."""
        return self.db.delete_cache_by_file(file_id)

# Database table (PostgreSQL)
# file_description_cache:
#   - cache_key (TEXT, PK) - SHA256 hash
#   - file_id (TEXT, indexed)
#   - query (TEXT, nullable)
#   - description (TEXT)
#   - created_at (TIMESTAMP)
```

### FileRetrievalService (`backend/services/file_retrieval_service.py`)

Unified orchestrator with dual output paths:

```python
@dataclass
class FileContentResult:
    text: Optional[str] = None
    images: Optional[List[bytes]] = None
    description: Optional[str] = None
    error: Optional[str] = None

class FileRetrievalService:
    """Orchestrates file content retrieval by type."""

    async def get_content(
        self,
        file_id: str,
        query: Optional[str] = None,
        pages: Optional[List[int]] = None,
        model_receives_images: Optional[bool] = None
    ) -> FileContentResult:
        """Get file content with appropriate handling."""

        file_path = find_file_by_id(file_id)
        mime_type = get_mime_type(file_path)

        # IMAGE FILES
        if mime_type in SUPPORTED_IMAGE_TYPES:
            if model_receives_images and not query:
                # Return raw image for multimodal model
                return FileContentResult(images=[file_path.read_bytes()])
            else:
                # Get/generate description for text-only model
                cached = await self.cache.get(file_id, query)
                if cached:
                    return FileContentResult(description=cached)

                image_data = file_path.read_bytes()
                description = await self.vision.describe_image(
                    image_data, mime_type, query
                )
                await self.cache.set(file_id, description, query)
                return FileContentResult(description=description)

        # PDF FILES
        if mime_type == 'application/pdf':
            text = get_extracted_text(file_id)

            if model_receives_images:
                # Return text + page images
                page_paths = get_pdf_page_paths(file_id)
                images = [p.read_bytes() for p in page_paths]
                return FileContentResult(text=text, images=images)
            else:
                # Return text + visual descriptions
                page_paths = get_pdf_page_paths(file_id)
                descriptions = []
                for i, page_path in enumerate(page_paths, 1):
                    page_data = page_path.read_bytes()
                    desc = await self.vision.analyze_document_page(
                        page_data, 'image/png', query
                    )
                    descriptions.append(f"[Page {i}]\n{desc}")
                return FileContentResult(
                    text=text,
                    description="\n\n".join(descriptions)
                )

        # AUDIO FILES
        if mime_type.startswith('audio/'):
            transcript = get_transcript(file_id)
            return FileContentResult(text=transcript)

        # TEXT FILES
        return FileContentResult(text=file_path.read_text())
```

### AudioHandler (`backend/services/audio_handler.py`)

Transcription with API + local fallback:

```python
USE_LOCAL_WHISPER = os.getenv("USE_LOCAL_WHISPER", "false").lower() == "true"
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", None)

async def _transcribe_with_openai(file_path: Path) -> Optional[str]:
    """Transcribe using OpenAI Whisper API."""
    client = AsyncOpenAI(
        api_key=os.getenv("WHISPER_API_KEY", os.getenv("OPENAI_API_KEY")),
        base_url=os.getenv("WHISPER_BASE_URL", os.getenv("OPENAI_BASE_URL"))
    )

    with open(file_path, "rb") as f:
        response = await client.audio.transcriptions.create(
            model=os.getenv("WHISPER_MODEL", "whisper-1"),
            file=f,
            language=WHISPER_LANGUAGE
        )
    return response.text

def _transcribe_local(
    file_path: Path,
    language: Optional[str] = None
) -> Optional[str]:
    """Fallback to local whisper model."""
    import whisper
    model = whisper.load_model(LOCAL_WHISPER_MODEL)
    result = model.transcribe(str(file_path), language=language)
    return result["text"]

async def process_audio(file_path: Path, file_id: str) -> dict:
    """Process audio file: transcribe and save."""
    try:
        # Try API first
        transcript = await _transcribe_with_openai(file_path)
    except Exception as e:
        if USE_LOCAL_WHISPER:
            transcript = _transcribe_local(file_path, WHISPER_LANGUAGE)
        else:
            raise

    # Save transcript
    transcript_path = file_path.parent / f"{file_id}_transcript.txt"
    transcript_path.write_text(transcript)

    return {"transcript": transcript, "path": transcript_path}
```

### Configuration (`backend/config.py`)

```python
# Document Processing
MAX_PDF_PAGES = 20
SUPPORTED_DOCUMENT_TYPES = {
    'application/pdf': 'pdf',
    'text/plain': 'txt',
    'text/markdown': 'md',
}

# Image Processing
MODEL_RECEIVE_IMAGES = os.getenv("MODEL_RECEIVE_IMAGES", "false") == "true"
MODEL_RECEIVE_IMAGES_PDF = os.getenv("MODEL_RECEIVE_IMAGES_PDF", "false") == "true"
SUPPORTED_IMAGE_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'}

# Vision Model (separate from primary LLM)
VISION_API_KEY = os.getenv("VISION_API_KEY")
VISION_BASE_URL = os.getenv("VISION_BASE_URL")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")

# Audio/Whisper
WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
USE_LOCAL_WHISPER = os.getenv("USE_LOCAL_WHISPER", "false") == "true"
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")

# File Storage
FILES_DIR = os.getenv("FILES_DIR", "./files")
```

---

## Implementation Roadmap

This roadmap maps Fessi components to Graph-RAG implementation tasks.

### Overview

```
Porting Strategy
================

Phase 1: Configuration ──────────────────────────────────────────────────
         No Fessi code needed - new Graph-RAG config

Phase 2: VisionHelper ───────────────────────────────────────────────────
         Port: vision_helper.py → src/services/vision_helper.py
         Changes: Singleton → ToolContext service, add sync wrapper

Phase 3: DocumentRenderer ───────────────────────────────────────────────
         Port: document_handler.py (partial) → src/services/document_renderer.py
         Extend: Add PPTX/DOCX rendering (not in Fessi)

Phase 4: DescriptionCache ───────────────────────────────────────────────
         Port: description_cache.py → src/services/description_cache.py
         Changes: PostgreSQL → file-based, file_id → content hash

Phase 5: Enhanced read_file ─────────────────────────────────────────────
         Port: file_retrieval_service.py logic → workspace_tools.py
         Integrate: DocumentRenderer, VisionHelper, DescriptionCache

Phase 6: Environment ────────────────────────────────────────────────────
         Port: config.py patterns → .env.example
```

### Phase 1: Agent Configuration (No Fessi Port)

**New Files:**
- `config/defaults.yaml` (modify)
- `config/schema.json` (modify)

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 1.1 | Add `model.multimodal` to defaults.yaml | Default: `false` |
| 1.2 | Add schema definition | Type: boolean, description |
| 1.3 | Update config loader | Ensure property is accessible via `config.model.multimodal` |

**Estimated Complexity:** Low

---

### Phase 2: VisionHelper Service

**Source:** `Advanced-LLM-Chat/backend/services/vision_helper.py`
**Target:** `src/services/vision_helper.py`

**Porting Map:**
| Fessi | Graph-RAG | Changes |
|-------|-----------|---------|
| `VisionHelper.__init__()` | `VisionHelper.__init__()` | Same pattern |
| `describe_image()` | `describe_image()` | Add sync wrapper |
| `analyze_document_page()` | `describe_document_page()` | Rename, add sync wrapper |
| `get_vision_helper()` singleton | ToolContext service | Register in tool context |

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 2.1 | Create `src/services/vision_helper.py` | Port class structure |
| 2.2 | Port `describe_image()` | Keep async, add sync wrapper |
| 2.3 | Port `analyze_document_page()` as `describe_document_page()` | Adjust prompts |
| 2.4 | Add `run_async()` helper | For sync tool signatures |
| 2.5 | Register in ToolContext | As `vision_helper` service |
| 2.6 | Add unit tests | Mock AsyncOpenAI client |

**Code to Reuse:**
```python
# Can copy directly with minimal changes:
# - __init__() constructor pattern
# - Base64 encoding logic
# - Message format for vision API
# - Default prompts
```

**Estimated Complexity:** Medium

---

### Phase 3: DocumentRenderer

**Source:** `Advanced-LLM-Chat/backend/services/document_handler.py` (partial)
**Target:** `src/services/document_renderer.py`

**Porting Map:**
| Fessi | Graph-RAG | Changes |
|-------|-----------|---------|
| `render_pdf_pages()` | `render_pdf_page()` | Single page, return bytes |
| N/A | `render_pptx_slide()` | New implementation |
| N/A | `render_docx_page()` | New implementation |
| `MAX_PDF_PAGES` constant | `max_pages` config | Make configurable |

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 3.1 | Create `src/services/document_renderer.py` | Class skeleton |
| 3.2 | Port `render_pdf_page()` | From `render_pdf_pages()`, single page |
| 3.3 | Implement `render_pptx_slide()` | LibreOffice → PDF → image |
| 3.4 | Implement `render_docx_page()` | LibreOffice → PDF → image |
| 3.5 | Implement `get_page_count()` | For each format |
| 3.6 | Register in ToolContext | As `document_renderer` service |
| 3.7 | Add unit tests | Test each format |

**Code to Reuse:**
```python
# From Fessi document_handler.py:
# - pdf2image convert_from_path() pattern
# - DPI settings (150)
# - PNG save to BytesIO pattern
```

**New Implementation Needed:**
```python
# PPTX/DOCX rendering via LibreOffice headless:
def _convert_to_pdf(file_path: Path) -> Path:
    """Convert PPTX/DOCX to PDF using LibreOffice."""
    import subprocess
    output_dir = Path(tempfile.mkdtemp())
    subprocess.run([
        'soffice', '--headless', '--convert-to', 'pdf',
        '--outdir', str(output_dir), str(file_path)
    ], check=True)
    return output_dir / f"{file_path.stem}.pdf"
```

**Estimated Complexity:** Medium-High (PPTX/DOCX are new)

---

### Phase 4: DescriptionCache

**Source:** `Advanced-LLM-Chat/backend/services/cache/description_cache.py`
**Target:** `src/services/description_cache.py`

**Porting Map:**
| Fessi | Graph-RAG | Changes |
|-------|-----------|---------|
| `_make_key(file_id, query)` | `_make_key(file_path, page, query)` | Content hash instead of file_id |
| PostgreSQL storage | File-based storage | Simpler, no DB dependency |
| `get()` async | `get()` sync | File I/O is fast |
| `set()` async | `set()` sync | File I/O is fast |
| `delete_by_file()` | Not needed | Content-addressable keys |

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 4.1 | Create `src/services/description_cache.py` | Class skeleton |
| 4.2 | Implement `_hash_file_content()` | SHA256 of file bytes |
| 4.3 | Implement `_make_key()` | Hash(content_hash + page + query) |
| 4.4 | Implement `get()` | Read from `{key}.txt` |
| 4.5 | Implement `set()` | Write to `{key}.txt` |
| 4.6 | Create cache directory on init | `workspace/.vision_cache/` |
| 4.7 | Register in ToolContext | As `description_cache` service |
| 4.8 | Add unit tests | Test cache hit/miss |

**Code to Reuse:**
```python
# From Fessi:
# - SHA256 key generation pattern
# - json.dumps(key_data, sort_keys=True) for deterministic keys
```

**Estimated Complexity:** Low

---

### Phase 5: Enhanced read_file Tool

**Source:** `Advanced-LLM-Chat/backend/services/file_retrieval_service.py`
**Target:** `src/tools/workspace_tools.py` (modify existing `read_file`)

**Porting Map:**
| Fessi | Graph-RAG | Changes |
|-------|-----------|---------|
| `FileContentResult` dataclass | Return dict or formatted string | Simpler |
| `get_content()` method | `_read_file_impl()` | Integrate into tool |
| `model_receives_images` param | `context.config.model.multimodal` | From config |
| `file_id` lookup | Direct path | Workspace-relative |

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 5.1 | Add `describe` parameter | To existing `read_file` signature |
| 5.2 | Create `_handle_image_file()` | Port image handling logic |
| 5.3 | Create `_handle_document_file()` | Port document handling logic |
| 5.4 | Implement `_get_mime_type()` | Helper for image types |
| 5.5 | Implement `_format_results()` | Format output for agent |
| 5.6 | Update tool description | Document new capabilities |
| 5.7 | Add integration tests | Test with real files |

**Code to Reuse:**
```python
# From Fessi file_retrieval_service.py:
# - Dual output path logic (model_receives_images check)
# - Cache check before vision call pattern
# - Base64 encoding for multimodal output
# - Text + description formatting
```

**Estimated Complexity:** High (core integration point)

---

### Phase 6: Environment Configuration

**Source:** `Advanced-LLM-Chat/backend/config.py`
**Target:** `.env.example`

**Porting Map:**
| Fessi | Graph-RAG | Changes |
|-------|-----------|---------|
| `VISION_API_KEY` | `VISION_API_KEY` | Same |
| `VISION_BASE_URL` | `VISION_BASE_URL` | Same |
| `VISION_MODEL` | `VISION_MODEL` | Same, default gpt-4o-mini |
| `MODEL_RECEIVE_IMAGES` | `model.multimodal` in config | Moved to agent config |
| `WHISPER_*` vars | `WHISPER_*` vars | Same pattern |

**Tasks:**
| # | Task | Details |
|---|------|---------|
| 6.1 | Add vision env vars to `.env.example` | With comments |
| 6.2 | Add whisper env vars to `.env.example` | Optional section |
| 6.3 | Update README/docs | Document new configuration |

**Estimated Complexity:** Low

---

### Implementation Order

```
Week 1: Foundation
├── Phase 1: Agent Configuration (1 day)
├── Phase 4: DescriptionCache (1 day)
└── Phase 2: VisionHelper (2-3 days)

Week 2: Rendering + Integration
├── Phase 3: DocumentRenderer (3-4 days)
│   ├── PDF rendering (1 day - port from Fessi)
│   ├── PPTX rendering (1-2 days - new)
│   └── DOCX rendering (1 day - new)
└── Phase 6: Environment (0.5 day)

Week 3: Tool Integration
└── Phase 5: Enhanced read_file (3-4 days)
    ├── Image handling (1 day)
    ├── Document handling (2 days)
    └── Testing + polish (1 day)
```

### File Mapping Summary

| Fessi Source | Graph-RAG Target | Status |
|--------------|------------------|--------|
| `services/vision_helper.py` | `src/services/vision_helper.py` | Port |
| `services/document_handler.py` | `src/services/document_renderer.py` | Partial port + extend |
| `services/cache/description_cache.py` | `src/services/description_cache.py` | Port + simplify |
| `services/file_retrieval_service.py` | `src/tools/workspace_tools.py` | Integrate into read_file |
| `services/audio_handler.py` | `src/services/audio_handler.py` | Future (optional) |
| `config.py` | `.env.example` + `config/defaults.yaml` | Adapt |

---

## Implementation Plan

### Phase 1: Agent Configuration for Multimodal

Add to `config/defaults.yaml`:

```yaml
model:
  # ... existing model config ...
  multimodal: false  # Set to true if primary model can process images
```

Add to `config/schema.json`:

```json
{
  "model": {
    "properties": {
      "multimodal": {
        "type": "boolean",
        "default": false,
        "description": "Whether the primary model can process images directly"
      }
    }
  }
}
```

Example agent configs:

```yaml
# config/text_only_agent.yaml - Uses OSS 120B text model
model:
  name: "your-120b-model"
  multimodal: false  # Will receive AI descriptions of visuals

# config/multimodal_agent.yaml - Uses GPT-4o or similar
model:
  name: "gpt-4o"
  multimodal: true  # Will receive actual screenshots
```

### Phase 2: Vision Helper Service

Create `src/services/vision_helper.py`:

```python
class VisionHelper:
    """
    Helper service for vision tasks using a dedicated multimodal model.

    Used when the primary model is text-only (multimodal: false) and we
    need to generate text descriptions of visual content.
    """

    def __init__(self):
        # Load from environment/config
        self.client = AsyncOpenAI(
            api_key=VISION_API_KEY,
            base_url=VISION_BASE_URL,
            timeout=VISION_TIMEOUT
        )
        self.model = VISION_MODEL

    async def describe_image(
        self,
        image_data: Union[bytes, str],
        mime_type: str = "image/png",
        query: Optional[str] = None
    ) -> str:
        """Generate a description of an image.

        Args:
            image_data: Image bytes or base64 string
            mime_type: Image MIME type
            query: Optional specific question about the image

        Returns:
            Text description of the image
        """
        ...

    async def describe_document_page(
        self,
        page_image: bytes,
        page_num: int,
        query: Optional[str] = None
    ) -> str:
        """Describe a rendered document page.

        Args:
            page_image: PNG bytes of rendered page
            page_num: Page number for context
            query: Optional specific question

        Returns:
            Text description of the page's visual content
        """
        ...
```

### Phase 3: Document Renderer

Create `src/services/document_renderer.py`:

```python
class DocumentRenderer:
    """
    Renders document pages as images for visual analysis.

    Supports PDF, PPTX, and DOCX formats.
    """

    def render_pdf_page(
        self,
        file_path: Path,
        page_num: int,
        dpi: int = 150
    ) -> bytes:
        """Render a PDF page as PNG.

        Uses pdf2image (requires poppler system package).
        """
        from pdf2image import convert_from_path

        images = convert_from_path(
            file_path,
            first_page=page_num,
            last_page=page_num,
            dpi=dpi
        )

        if not images:
            raise ValueError(f"Could not render page {page_num}")

        buffer = io.BytesIO()
        images[0].save(buffer, format="PNG")
        return buffer.getvalue()

    def render_pptx_slide(
        self,
        file_path: Path,
        slide_num: int,
        width: int = 1920,
        height: int = 1080
    ) -> bytes:
        """Render a PowerPoint slide as PNG.

        Uses python-pptx to extract slide and render.
        """
        from pptx import Presentation
        from pptx.util import Inches
        from PIL import Image

        # Note: python-pptx doesn't have native rendering
        # Options:
        # 1. Use LibreOffice headless: soffice --convert-to pdf
        # 2. Use unoconv
        # 3. Use comtypes on Windows
        # For cross-platform, convert to PDF first then render
        ...

    def render_docx_page(
        self,
        file_path: Path,
        page_num: int,
        dpi: int = 150
    ) -> bytes:
        """Render a Word document page as PNG.

        Converts to PDF first, then renders the page.
        Uses docx2pdf or LibreOffice headless.
        """
        # Convert DOCX to PDF (temp file)
        # Then use render_pdf_page()
        ...

    def get_page_count(self, file_path: Path) -> int:
        """Get total page/slide count for a document."""
        suffix = file_path.suffix.lower()

        if suffix == ".pdf":
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                return len(pdf.pages)
        elif suffix == ".pptx":
            from pptx import Presentation
            prs = Presentation(file_path)
            return len(prs.slides)
        elif suffix == ".docx":
            # Need to convert to PDF to get accurate page count
            # Or estimate based on content
            ...
```

### Phase 4: Description Cache

Create `src/services/description_cache.py`:

```python
class DescriptionCache:
    """
    Global cache for vision-generated descriptions.

    Uses file-based storage in workspace/.vision_cache/ (shared across all jobs).
    Cache key is SHA256(file_content_hash + page + query) for content-addressable lookups.
    """

    DEFAULT_CACHE_DIR = Path("workspace/.vision_cache")

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or self.DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _hash_file_content(self, file_path: Path) -> str:
        """Compute SHA256 hash of file contents."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _make_key(
        self,
        file_path: Path,
        page: Optional[int] = None,
        query: Optional[str] = None
    ) -> str:
        """Create content-addressable cache key."""
        content_hash = self._hash_file_content(file_path)
        key_data = {
            "content_hash": content_hash,
            "page": page,
            "query": query or ""
        }
        return hashlib.sha256(
            json.dumps(key_data, sort_keys=True).encode()
        ).hexdigest()

    def get(
        self,
        file_path: Path,
        page: Optional[int] = None,
        query: Optional[str] = None
    ) -> Optional[str]:
        """Get cached description if available."""
        key = self._make_key(file_path, page, query)
        cache_file = self.cache_dir / f"{key}.txt"
        if cache_file.exists():
            return cache_file.read_text()
        return None

    def set(
        self,
        file_path: Path,
        description: str,
        page: Optional[int] = None,
        query: Optional[str] = None
    ):
        """Store description in cache."""
        key = self._make_key(file_path, page, query)
        cache_file = self.cache_dir / f"{key}.txt"
        cache_file.write_text(description)
```

### Phase 5: Enhanced read_file Tool

Modify `src/tools/workspace_tools.py`:

```python
@tool
def read_file(
    path: str,
    page_start: Optional[int] = None,
    page_end: Optional[int] = None,
    describe: Optional[str] = None
) -> str:
    """Read content from a file in the workspace.

    Supports: PDF, PPTX, DOCX, and image files (PNG, JPG, etc.)

    For documents (PDF, PPTX, DOCX):
    - Text is always extracted
    - Visual content (charts, diagrams, figures) is automatically included:
      - Multimodal models: Receive rendered page screenshots
      - Text-only models: Receive AI-generated descriptions

    For image files:
    - Multimodal models: Receive the image directly
    - Text-only models: Receive AI-generated description

    Args:
        path: Relative path to the file
        page_start: First page/slide (1-indexed, documents only)
        page_end: Last page/slide (documents only)
        describe: Optional query for visual analysis
                  (e.g., "What values are shown in this chart?")

    Returns:
        For multimodal models: Text content + embedded images
        For text-only models: Text content + visual descriptions
    """
    ...
```

#### Implementation Logic

```python
def _read_file_impl(
    context: ToolContext,
    path: str,
    page_start: Optional[int] = None,
    page_end: Optional[int] = None,
    describe: Optional[str] = None
) -> Union[str, List[dict]]:
    """Internal implementation of read_file."""

    full_path = context.workspace.get_path(path)
    suffix = full_path.suffix.lower()
    is_multimodal = context.config.model.multimodal

    # Handle image files
    if suffix in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"]:
        return _handle_image_file(context, full_path, describe, is_multimodal)

    # Handle documents
    if suffix in [".pdf", ".pptx", ".docx"]:
        return _handle_document_file(
            context, full_path, suffix,
            page_start, page_end, describe, is_multimodal
        )

    # Other files: just read as text
    return full_path.read_text()


def _handle_image_file(
    context: ToolContext,
    file_path: Path,
    describe: Optional[str],
    is_multimodal: bool
) -> Union[str, dict]:
    """Handle standalone image files."""

    if is_multimodal:
        # Return image for multimodal model to see directly
        image_data = file_path.read_bytes()
        base64_image = base64.b64encode(image_data).decode()
        mime_type = _get_mime_type(file_path)

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64_image
            }
        }
    else:
        # Get AI description for text-only model
        cache = context.get_service("description_cache")
        vision = context.get_service("vision_helper")

        # Check cache
        cached = cache.get(file_path, query=describe)
        if cached:
            return f"[IMAGE: {file_path.name}]\n{cached}"

        # Generate description
        image_data = file_path.read_bytes()
        description = run_async(
            vision.describe_image(image_data, query=describe)
        )

        cache.set(file_path, description, query=describe)
        return f"[IMAGE: {file_path.name}]\n{description}"


def _handle_document_file(
    context: ToolContext,
    file_path: Path,
    suffix: str,
    page_start: Optional[int],
    page_end: Optional[int],
    describe: Optional[str],
    is_multimodal: bool
) -> Union[str, List]:
    """Handle PDF, PPTX, DOCX files."""

    renderer = context.get_service("document_renderer")

    # Get page range
    total_pages = renderer.get_page_count(file_path)
    start = page_start or 1
    end = min(page_end or total_pages, total_pages)

    results = []

    for page_num in range(start, end + 1):
        # Extract text
        text = _extract_page_text(file_path, suffix, page_num)

        # Render page as image
        page_image = renderer.render_page(file_path, suffix, page_num)

        if is_multimodal:
            # Return text + actual screenshot for multimodal model
            base64_image = base64.b64encode(page_image).decode()
            results.append({
                "page": page_num,
                "text": text,
                "image": {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64_image
                    }
                }
            })
        else:
            # Get AI description for text-only model
            cache = context.get_service("description_cache")
            vision = context.get_service("vision_helper")

            cached = cache.get(file_path, page=page_num, query=describe)
            if cached:
                description = cached
            else:
                description = run_async(
                    vision.describe_document_page(page_image, page_num, query=describe)
                )
                cache.set(file_path, description, page=page_num, query=describe)

            results.append({
                "page": page_num,
                "text": text,
                "visual_description": description
            })

    return _format_results(results, is_multimodal)
```

### Phase 6: Environment Configuration

Add to `.env.example`:

```bash
# Vision Model (for describing images when primary model is text-only)
# Only used when model.multimodal = false in agent config
VISION_API_KEY=your_openai_key
VISION_BASE_URL=https://api.openai.com/v1
VISION_MODEL=gpt-4o-mini
VISION_TIMEOUT=120

# Audio Transcription (optional)
WHISPER_ENABLED=false
WHISPER_API_KEY=your_openai_key
WHISPER_BASE_URL=https://api.openai.com/v1
WHISPER_MODEL=whisper-1
USE_LOCAL_WHISPER=false
LOCAL_WHISPER_MODEL=base
```

## Dependencies

### Python Packages

Add to `requirements.txt`:

```
pdf2image>=1.16.0       # PDF page rendering
python-pptx>=0.6.21     # PowerPoint handling
python-docx>=0.8.11     # Word document handling
docx2pdf>=0.1.8         # DOCX to PDF conversion (optional, for page rendering)
Pillow>=10.0.0          # Image handling
openai>=1.0.0           # Already present, for vision API
```

### System Dependencies

```bash
# Ubuntu/Debian
sudo apt-get install poppler-utils    # For pdf2image
sudo apt-get install libreoffice      # For PPTX/DOCX rendering (optional)

# macOS
brew install poppler
brew install --cask libreoffice       # Optional

# Fedora
sudo dnf install poppler-utils
sudo dnf install libreoffice          # Optional
```

## Usage Examples

### Example 1: Multimodal Model Reading PDF

```yaml
# config/my_agent.yaml
model:
  name: gpt-4o
  multimodal: true
```

```
Agent: I'll read page 7 of the compliance document.
Tool: read_file("documents/compliance.pdf", page_start=7, page_end=7)

Result: (Model receives)
- Extracted text from page 7
- Rendered PNG screenshot of page 7 (base64 embedded)
- Model can SEE the decision tree directly
```

### Example 2: Text-Only Model Reading PDF

```yaml
# config/my_agent.yaml
model:
  name: your-120b-model
  multimodal: false
```

```
Agent: I'll read page 7 of the compliance document.
Tool: read_file("documents/compliance.pdf", page_start=7, page_end=7)

Result:
[PAGE 7]
Figure 3.1: Data Retention Decision Tree
See figure above for the complete decision process.

The retention periods are determined by...

[PAGE 7 - VISUAL CONTENT]
The page shows a decision tree diagram with the following flow:
1. Start: "New Document Received"
2. Decision: "Contains Personal Data?"
   - Yes → "Apply GDPR retention (max 3 years)"
   - No → Continue
3. Decision: "Tax Relevant?"
   - Yes → "Apply GoBD retention (10 years)"
   - No → "Standard retention (5 years)"
```

### Example 3: Targeted Visual Query with `describe`

```
Agent: I need to understand the specific values in the chart.
Tool: read_file("documents/report.pdf", page_start=12, page_end=12,
                describe="What are the exact percentages shown in the pie chart?")

Result: (for text-only model)
[PAGE 12]
Q3 Revenue Distribution
See Figure 5.2 for breakdown by region.

[PAGE 12 - VISUAL CONTENT (Query: "What are the exact percentages...")]
The pie chart shows Q3 revenue distribution:
- North America: 42%
- Europe: 28%
- Asia Pacific: 19%
- Rest of World: 11%
```

### Example 4: Reading PowerPoint Slides

```
Agent: Let me review the architecture slides.
Tool: read_file("documents/architecture.pptx", page_start=3, page_end=5)

Result: (for multimodal model)
- Text from slides 3-5
- Rendered PNG of each slide
- Model sees diagrams, icons, layouts directly

Result: (for text-only model)
[SLIDE 3]
System Architecture Overview
• Microservices-based design
• Event-driven communication

[SLIDE 3 - VISUAL CONTENT]
The slide shows an architecture diagram with three main components...
```

### Example 5: Reading an Image File

```
Agent: The user uploaded a screenshot.
Tool: read_file("documents/error_screenshot.png",
                describe="What error message is shown?")

Result: (for multimodal model)
- The actual image (base64) - model can see it directly

Result: (for text-only model)
[IMAGE: error_screenshot.png]
The screenshot shows a Windows error dialog with the message:
"Access Denied: You do not have permission to access this resource.
Error code: 0x80070005"
The dialog has an "OK" button and a red X icon.
```

## Design Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| **Sync vs Async** | Sync signatures with async internals | Matches existing workspace tools pattern. Use `asyncio.run()` internally. |
| **Automatic vs Explicit** | Automatic visual content for all pages | Agent always gets full context. Use `describe` for targeted queries. |
| **Cost Control** | No limits (cache + trust agent) | Simplest implementation. Global cache prevents repeated API calls. |
| **Cache Location** | Global `workspace/.vision_cache/` | Shared across jobs. Content-addressable keys maximize cache hits. |
| **Format Support** | PDF, PPTX, DOCX + images | Full document support from the start. |
| **Multimodal Config** | `model.multimodal` in agent config | Per-agent setting allows mixing model types. |

## Migration Notes

### From Fessi

The Fessi implementation can be adapted with these changes:

1. **File paths**: Fessi uses `file_id` lookups; Graph-RAG uses workspace-relative paths
2. **Async**: Fessi is fully async; Graph-RAG uses sync wrappers with async internals
3. **Caching**: Fessi uses PostgreSQL; Graph-RAG uses global file-based cache
4. **Tool registration**: Fessi uses `@tool` directly; Graph-RAG uses `ToolContext` pattern
5. **Dual output**: Graph-RAG supports both multimodal (raw images) and text-only (descriptions)
6. **Document types**: Graph-RAG adds PPTX/DOCX support beyond Fessi's PDF/image handling

## Related Files

- Fessi source: `Advanced-LLM-Chat/backend/services/vision_helper.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/file_retrieval_service.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/cache/description_cache.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/audio_handler.py`
