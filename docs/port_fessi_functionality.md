# Porting Fessi Vision/Audio Functionality to Graph-RAG Workspace

This document outlines the plan for adding vision and audio capabilities to the Graph-RAG workspace agent, based on the patterns implemented in the Advanced-LLM-Chat (Fessi) backend.

## Problem Statement

The Graph-RAG workspace agent uses a text-only OSS 120B model that cannot process images. This creates limitations when working with PDFs:

- **No image awareness**: The agent doesn't know if a PDF page contains charts, diagrams, or figures
- **No visual analysis**: Cannot describe or answer questions about visual content
- **Missing context**: Requirements documents often contain flowcharts, decision trees, and compliance diagrams that are invisible to the agent

## Current State

| Capability | Status | Implementation |
|------------|--------|----------------|
| PDF text extraction | ✅ | `pdfplumber.extract_text()` in `pdf_utils.py` |
| Page-based PDF reading | ✅ | `read_file(path, page_start, page_end)` |
| PDF metadata | ✅ | `get_document_info()` |
| Image file support | ❌ | None |
| PDF image detection | ❌ | None |
| Vision model integration | ❌ | None |
| Description caching | ❌ | None |
| Audio transcription | ❌ | None |

### Relevant Existing Files

- `src/tools/workspace_tools.py` - File operations including `read_file`
- `src/tools/pdf_utils.py` - PDF reading with `PDFReader` class
- `src/tools/document_tools.py` - Document extraction using langchain loaders
- `src/tools/context.py` - `ToolContext` for dependency injection

## Fessi Reference Implementation

The Fessi backend solves this problem with a clean architecture:

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent (text-only model)                  │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     get_file_content tool                       │
│  - file_id: str                                                 │
│  - query: Optional[str]  ← "What does this chart show?"         │
│  - pages: Optional[List[int]]                                   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FileRetrievalService                         │
│  Routes by file type:                                           │
│  - Images → VisionHelper.describe_image()                       │
│  - PDFs → text + VisionHelper.analyze_document_page()           │
│  - Audio → get_transcript()                                     │
└─────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
┌───────────────────────────┐   ┌───────────────────────────┐
│      VisionHelper         │   │    DescriptionCache       │
│  - Separate OpenAI client │   │  - DB-backed cache        │
│  - gpt-4o-mini model      │   │  - Key: SHA256(file+query)│
│  - describe_image()       │   │  - Avoids repeated calls  │
│  - analyze_document_page()│   │                           │
└───────────────────────────┘   └───────────────────────────┘
```

### Key Source Files (Fessi)

| File | Purpose |
|------|---------|
| `backend/services/vision_helper.py` | Vision model client with `describe_image()` and `analyze_document_page()` |
| `backend/services/file_retrieval_service.py` | Orchestrates content retrieval by file type |
| `backend/services/cache/description_cache.py` | Database-backed cache for vision descriptions |
| `backend/services/audio_handler.py` | Whisper transcription (API + local fallback) |
| `backend/services/tools/file_tools.py` | `get_file_content` tool exposed to agent |

### Configuration Pattern

Fessi uses separate configuration for the vision model:

```python
# Primary model (text-only, e.g., OSS 120B on vLLM)
OPENAI_BASE_URL = "http://your-vllm-server/v1"
OPENAI_MODEL = "your-120b-model"

# Vision model (multimodal, separate endpoint)
VISION_BASE_URL = "https://api.openai.com/v1"  # Or other provider
VISION_MODEL = "gpt-4o-mini"
VISION_API_KEY = "..."  # Can be different from primary
VISION_TIMEOUT = 120  # seconds
```

## Implementation Plan

### Phase 1: Vision Helper Service

Create `src/services/vision_helper.py`:

```python
class VisionHelper:
    """
    Helper service for vision tasks using a dedicated multimodal model.

    Used when the primary model is text-only and we need to analyze
    images or visual content from documents.
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
        mime_type: str = "image/jpeg",
        prompt: Optional[str] = None
    ) -> str:
        """Generate a description of an image."""
        ...

    async def analyze_document_page(
        self,
        page_image: Union[bytes, str],
        mime_type: str = "image/png",
        query: Optional[str] = None
    ) -> str:
        """Analyze a document page image (rendered PDF page)."""
        ...
```

### Phase 2: PDF Page Rendering

Enhance `src/tools/pdf_utils.py`:

```python
# New dependency: pdf2image (requires poppler)
from pdf2image import convert_from_path

class PDFReader:
    # ... existing methods ...

    def render_page_as_image(
        self,
        file_path: Path,
        page_num: int,
        dpi: int = 150
    ) -> bytes:
        """Render a PDF page as a PNG image.

        Args:
            file_path: Path to the PDF
            page_num: Page number (1-indexed)
            dpi: Resolution (150 is good balance of quality/size)

        Returns:
            PNG image as bytes
        """
        images = convert_from_path(
            file_path,
            first_page=page_num,
            last_page=page_num,
            dpi=dpi
        )

        if not images:
            raise ValueError(f"Could not render page {page_num}")

        # Convert PIL image to PNG bytes
        buffer = io.BytesIO()
        images[0].save(buffer, format="PNG")
        return buffer.getvalue()

    def has_images(self, file_path: Path, page_num: int) -> bool:
        """Check if a PDF page contains images."""
        with pdfplumber.open(file_path) as pdf:
            page = pdf.pages[page_num - 1]
            return len(page.images) > 0
```

### Phase 3: Description Cache

Create `src/services/description_cache.py`:

```python
class DescriptionCache:
    """
    Cache for vision-generated descriptions.

    Uses file-based storage in the workspace to avoid repeated API calls.
    Cache key is SHA256(file_path + query).
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _make_key(self, file_path: str, query: Optional[str] = None) -> str:
        key_data = {"file": file_path, "query": query or ""}
        return hashlib.sha256(
            json.dumps(key_data, sort_keys=True).encode()
        ).hexdigest()

    def get(self, file_path: str, query: Optional[str] = None) -> Optional[str]:
        """Get cached description if available."""
        ...

    def set(self, file_path: str, description: str, query: Optional[str] = None):
        """Store description in cache."""
        ...
```

### Phase 4: New Workspace Tools

Add to `src/tools/workspace_tools.py` or create `src/tools/vision_tools.py`:

#### Option A: New Dedicated Tools

```python
@tool
async def describe_image(
    path: str,
    query: Optional[str] = None
) -> str:
    """Describe an image file using vision AI.

    Use this when you need to understand visual content that you
    cannot see directly (charts, diagrams, photos, screenshots).

    Args:
        path: Path to image file (PNG, JPG, etc.)
        query: Optional specific question (e.g., "What values are shown in this chart?")

    Returns:
        Text description of the image
    """
    ...

@tool
async def analyze_pdf_visual(
    path: str,
    page: int,
    query: Optional[str] = None
) -> str:
    """Get visual analysis of a PDF page.

    Use this when a PDF page contains charts, diagrams, figures, or
    other visual elements that text extraction misses.

    Args:
        path: Path to PDF file
        page: Page number (1-indexed)
        query: Optional specific question about the visual content

    Returns:
        Text description of the page's visual content
    """
    ...
```

#### Option B: Enhanced read_file

```python
@tool
def read_file(
    path: str,
    page_start: Optional[int] = None,
    page_end: Optional[int] = None,
    include_visual: bool = False,  # NEW
    visual_query: Optional[str] = None  # NEW
) -> str:
    """Read content from a file in the workspace.

    For PDF files:
    - Text is always extracted
    - Set include_visual=True to also get AI descriptions of visual content
    - Use visual_query to ask specific questions about charts/diagrams

    Args:
        path: Relative path to the file
        page_start: For PDFs: first page (1-indexed)
        page_end: For PDFs: last page
        include_visual: Include AI-generated descriptions of images/charts
        visual_query: Specific question about visual content
    """
    ...
```

### Phase 5: Configuration

Add to `.env.example`:

```bash
# Vision Model (for image/PDF visual analysis)
# Used when primary model is text-only
VISION_ENABLED=true
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

Add to `src/config/defaults.json`:

```json
{
  "vision": {
    "enabled": true,
    "model": "gpt-4o-mini",
    "timeout": 120,
    "max_tokens": 1000,
    "cache_enabled": true
  }
}
```

## Dependencies

New packages to add to `requirements.txt`:

```
pdf2image>=1.16.0    # PDF page rendering (requires poppler system package)
Pillow>=10.0.0       # Image handling
openai>=1.0.0        # Already present, for vision API
```

System dependency (for pdf2image):
```bash
# Ubuntu/Debian
sudo apt-get install poppler-utils

# macOS
brew install poppler

# Fedora
sudo dnf install poppler-utils
```

## Usage Examples

### Example 1: Agent discovers a chart

```
Agent: I'll read page 7 of the compliance document.
Tool: read_file("documents/compliance.pdf", page_start=7, page_end=7)

Result:
[PAGE 7]
Figure 3.1: Data Retention Decision Tree
See figure above for the complete decision process.

The retention periods are determined by...

Agent: There's a figure on this page. Let me analyze it.
Tool: analyze_pdf_visual("documents/compliance.pdf", page=7,
                         query="Describe the decision tree and all its branches")

Result:
The decision tree shows the data retention workflow:
1. Start: "New Document Received"
2. Decision: "Contains Personal Data?"
   - Yes → "Apply GDPR retention (max 3 years)"
   - No → Continue
3. Decision: "Tax Relevant?"
   - Yes → "Apply GoBD retention (10 years)"
   - No → "Standard retention (5 years)"
...
```

### Example 2: Analyzing an image attachment

```
Agent: The user uploaded a screenshot. Let me analyze it.
Tool: describe_image("documents/screenshot.png",
                     query="What error message is shown?")

Result:
The screenshot shows a Windows error dialog with the message:
"Access Denied: You do not have permission to access this resource.
Error code: 0x80070005"
The dialog has an "OK" button and a red X icon.
```

## Migration Notes

### From Fessi

The Fessi implementation can be adapted with these changes:

1. **File paths**: Fessi uses `file_id` lookups; Graph-RAG uses workspace-relative paths
2. **Async**: Fessi is fully async; Graph-RAG tools may need sync wrappers
3. **Caching**: Fessi uses PostgreSQL; Graph-RAG can use file-based cache in workspace
4. **Tool registration**: Fessi uses `@tool` directly; Graph-RAG uses `ToolContext` pattern

### Tool Context Integration

```python
def create_vision_tools(context: ToolContext) -> List:
    """Create vision tools with injected context."""

    workspace = context.workspace_manager
    vision_helper = context.get_service("vision_helper")
    cache = context.get_service("description_cache")

    @tool
    async def describe_image(path: str, query: Optional[str] = None) -> str:
        # Check cache first
        cached = cache.get(path, query)
        if cached:
            return cached

        # Read image from workspace
        full_path = workspace.get_path(path)
        image_data = full_path.read_bytes()

        # Get description
        description = await vision_helper.describe_image(image_data, query=query)

        # Cache and return
        cache.set(path, description, query)
        return description

    return [describe_image, ...]
```

## Open Questions

1. **Sync vs Async**: The current workspace tools are synchronous. Should vision tools be async, or should we wrap them?

2. **Automatic vs Explicit**: Should `read_file` automatically include visual descriptions when images are detected, or require explicit `include_visual=True`?

3. **Cost Control**: Vision API calls cost money. Should there be a budget/limit system?

4. **Cache Location**: File-based in workspace, or separate global cache directory?

5. **Image Formats**: Support standalone images (PNG, JPG) or only PDF pages initially?

## Related Files

- Fessi source: `Advanced-LLM-Chat/backend/services/vision_helper.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/file_retrieval_service.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/cache/description_cache.py`
- Fessi source: `Advanced-LLM-Chat/backend/services/audio_handler.py`
