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
| `backend/services/tools/file_tools.py` | `get_file_content` tool exposed to agent |

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
