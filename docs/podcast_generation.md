# Podcast Generation from Documents

**Status**: Idea / Research phase

## Motivation

Gemini's NotebookLM can create "audio overviews" from documents, but it compresses 50 pages into ~5-15 minutes. The goal is to create longer-form podcasts (e.g., 30 minutes) that preserve more detail from lengthy academic papers or technical documents.

## Existing Tools & Research

### Google NotebookLM
- Free, up to 50 sources, 500k words per source
- Generates ~15 minute "deep dive" discussions with two AI hosts
- Limitations: English only, can't edit after generation, heavy compression
- Recent updates (2025): Interactive audio overviews, customization options
- [NotebookLM Audio Overviews Blog](https://blog.google/innovation-and-ai/products/notebooklm-audio-overviews/)

### Commercial Alternatives
- **[Wondercraft](https://www.wondercraft.ai/tools/ai-podcast-generator)** - 1000+ voices, clone your own, multi-host support
- **[NoteGPT](https://notegpt.io/ai-podcast-generator)** - Multi-voice conversations, supports PDFs/URLs/YouTube
- **PODLM** (iOS) - PDF to podcast with multiple AI voices
- **[Speechify](https://speechify.com/blog/how-to-turn-essays-and-pdfs-into-podcasts-with-speechify/)** - TTS focused, good for research papers

### Open Source / DIY Pipelines
- **[NotebookLlama](https://www.analyticsvidhya.com/blog/2025/12/build-your-own-notebookllama/)** - Open source recreation using Llama models
- **[Together.ai Tutorial](https://docs.together.ai/docs/open-notebooklm-pdf-to-podcast)** - Detailed guide for building PDF-to-podcast
- **[Featherless.ai Pipeline](https://featherless.ai/blog/building-a-pdf-to-podcast-pipeline-with-open-source-ai-from-text-extraction-to-voice-synthesis)** - PyMuPDF + LLM + Kokoro TTS
- **[n8n Workflow](https://n8n.io/workflows/4883-convert-pdf-documents-to-ai-podcasts-with-google-gemini-and-text-to-speech/)** - Gemini + TTS workflow template

## Architecture Pattern (from open source implementations)

```
PDF Document
    │
    ▼
┌─────────────────────────┐
│  Text Extraction        │  PyMuPDF, pdfplumber, etc.
│  (preserve structure)   │
└─────────────────────────┘
    │
    ▼
┌─────────────────────────┐
│  Script Generation      │  LLM with structured output (pydantic)
│  - Host/Guest dialogue  │  - DialogueItem(speaker, text)
│  - Chunked for length   │  - Control pacing and depth
└─────────────────────────┘
    │
    ▼
┌─────────────────────────┐
│  Text-to-Speech         │  Kokoro TTS (open source)
│  - Multiple voices      │  ElevenLabs, OpenAI TTS
│  - Natural prosody      │  Azure Speech, Google TTS
└─────────────────────────┘
    │
    ▼
Audio File (MP3/WAV)
```

## Advanced Pipeline Ideas

The key insight: Don't just convert text to speech. Use AI to **enrich and craft** the content, then **iteratively refine** with multimodal feedback.

### Multi-Pass Script Generation
Instead of one-shot conversion, have the agent spend multiple passes:
1. **Analysis pass** - Extract key concepts, structure, important quotes
2. **Outline pass** - Plan the narrative arc, decide pacing and depth per section
3. **Draft pass** - Generate dialogue with proper sourcing and citations
4. **Refinement pass** - Polish transitions, add natural conversation elements

### Modular Audio Assembly
Generate audio in sections, then puzzle them together:
```
Section 1: Intro & hook
Section 2: Background context
Section 3: Deep dive topic A
Section 4: Deep dive topic B
Section 5: Discussion/implications
Section 6: Summary & takeaways
```

### Multimodal QA Loop
Use a multimodal model to **listen** to generated audio and critique:
- Check transition smoothness between sections
- Evaluate pacing and flow
- Detect awkward phrasing or pronunciation issues
- Suggest re-generation of specific segments
- Could use Gemini 2.0 Flash or similar for audio understanding

### Configurable Style Parameters
- **Cast size**: Solo narrator, two hosts, panel of 3-4 experts
- **Tone**: Academic/precise, conversational/casual, debate-style
- **Focus**: Fact-dense vs high-level overview vs critical analysis
- **Audience level**: Expert, practitioner, general public
- **Pacing**: Dense information vs breathing room and examples

### Example Workflow
```
┌─────────────────────────────────────────────────────────────────┐
│  STRATEGIC PHASE: Planning                                       │
├─────────────────────────────────────────────────────────────────┤
│  1. Analyze source document structure and content               │
│  2. Define podcast parameters (length, tone, cast, focus)       │
│  3. Create section outline with time budgets                    │
│  4. Identify key quotes and concepts to include                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  TACTICAL PHASE: Script Generation (per section)                │
├─────────────────────────────────────────────────────────────────┤
│  For each section:                                              │
│    - Generate dialogue draft                                    │
│    - Self-review for accuracy and flow                          │
│    - Synthesize audio segment                                   │
│    - Store segment with metadata                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  QA PHASE: Multimodal Review                                    │
├─────────────────────────────────────────────────────────────────┤
│  1. Assemble full audio from sections                           │
│  2. Feed to multimodal model for listening review               │
│  3. Get feedback on transitions, pacing, clarity                │
│  4. Re-generate flagged sections if needed                      │
│  5. Final assembly and export                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Implementation Ideas for Graph-RAG

### Option A: Dedicated "Podcaster" Agent Config
- New agent config: `config/podcaster.yaml`
- Domain tools: `generate_script`, `synthesize_audio`, `chunk_document`, `review_audio`
- Prompt template focused on conversational dialogue generation
- Could leverage existing document processing pipeline
- Fits naturally into strategic/tactical phase model

### Option B: TTS Tools for Any Agent
- Add TTS tools to tool registry (domain tools)
- `tts_synthesize` - convert text to speech
- `podcast_script` - convert document/analysis to dialogue format
- `audio_review` - multimodal review of generated audio
- Reusable across different agent configs

### Key Design Decisions (TODO)
- [ ] TTS provider: OpenAI TTS vs ElevenLabs vs Kokoro (open source) vs Azure
- [ ] Dialogue format: Two hosts vs narrator + expert vs interview style
- [ ] Length control: How to specify target duration and control compression
- [ ] Chunking strategy: How to split long documents for coherent podcast segments
- [ ] Voice selection: Predefined voices vs user-configurable
- [ ] Multimodal QA: Which model for audio review (Gemini 2.0 Flash?)
- [ ] Section assembly: How to handle crossfades/transitions between segments

## Related Links
- [Mercity Step-by-Step Guide](https://www.mercity.ai/blog-post/step-by-step-guide-to-generate-podcasts-using-tts-and-llms-ai-elevenlabs)
- [Descript NotebookLM Review](https://www.descript.com/blog/article/testing-notebook-for-podcasters)
