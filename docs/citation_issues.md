# Citation Workflow Issues & Research Findings

## The Problem

The current citation workflow requires the agent to create citations **before** writing the text that references them. This is backwards from natural writing where you:

1. Write "The regulation requires X"
2. Then add a citation to support it

Instead, the current flow is:

1. Agent reads document, finds useful passage
2. Calls `cite_document()` to create citation → gets `[1]`
3. Later, when writing, must remember to insert `[1]`

This leads to the agent treating citations as **bookmarks** for passages rather than **inline references** embedded in text.

---

## Industry Research (January 2026)

### Two Main Paradigms

A [NeurIPS 2025 paper](https://arxiv.org/html/2509.21557) identifies two fundamental approaches:

| Aspect | G-Cite (Generation-Time) | P-Cite (Post-hoc) |
|--------|--------------------------|-------------------|
| How it works | Citations generated alongside text | Citations added after drafting |
| Coverage | 37-65% | **74-99%** |
| Precision | Variable (12-94%) | Competitive (26-75%) |
| Hallucination rate | 41% avg | 37% avg |
| Best for | Precision-critical settings | High-stakes (legal, medical, research) |

**Key finding**: P-Cite achieves roughly **twice the source coverage** while maintaining reasonable precision.

**Recommendations from the paper**:
- Use **P-Cite first for high-stakes applications** (healthcare, law, research) - prioritizes comprehensive citation coverage for verifiability
- Reserve **G-Cite for precision-critical settings** requiring strict claim validation with minimal false attributions

---

## Approaches from Literature

### 1. ReClaim - Interleaved Reference-Claim Generation

**Source**: [Ground Every Sentence: Improving Retrieval-Augmented LLMs with Interleaved Reference-Claim Generation](https://arxiv.org/abs/2407.01796)

Instead of generating text then adding citations, ReClaim **alternates** between generating a reference and generating a claim at the sentence level:

```
[Reference to Source A, page 5] → "The regulation requires X [1]"
[Reference to Source B, section 3] → "This ensures compliance with Y [2]"
```

**Results**: Achieves **90% citation accuracy** at sentence level.

**Key insight**: Moving from coarse-grained (passage/paragraph level) to fine-grained (sentence level) citations dramatically improves verifiability.

---

### 2. CEG - Citation-Enhanced Generation (Post-hoc)

**Source**: [Citation-Enhanced Generation for LLM-based Chatbots](https://arxiv.org/html/2402.16063v3) (ACL 2024)

A training-free, plug-and-play post-hoc approach:

1. **Initial Generation**: LLM writes response freely
2. **Claim Segmentation**: Response split into individual claims
3. **Retrieval**: Dense retrieval (SimCSE BERT) finds supporting docs for each claim
4. **NLI Verification**: Each claim labeled as "Factual" or "Nonfactual"
5. **Regeneration Loop**: If nonfactual claims exist, regenerate with retrieved docs
6. **Iterate**: Repeat until all statements are supported or max iterations reached

**Advantages**:
- No training or fine-tuning required
- Works with any LLM
- Matches natural writing flow (write first, cite after)
- Reduces hallucinations through verification cycles

---

### 3. Perplexity - Search-Native Architecture

**Source**: [Perplexity Architecture Analysis](https://www.frugaltesting.com/blog/behind-perplexitys-architecture-how-ai-search-handles-real-time-web-data)

Perplexity takes a fundamentally different approach where retrieval, ranking, and citation are **tightly integrated**:

- Uses hybrid retrieval (lexical + semantic + vector embeddings)
- Retrieved content passes through LLM which synthesizes with inline citations
- Not "generate then cite" but "retrieve → synthesize with citations inline"
- Authority scoring, freshness signals, and cross-source validation

**Key differentiator**: ChatGPT is generation-centric with retrieval bolted on. Perplexity is search-native with generation serving retrieval.

---

### 4. Self-RAG - Reflection Tokens

**Concept**: Trains LLM to generate special "reflection" tokens that trigger on-demand retrieval and self-critique.

**Results**: 7B and 13B Self-RAG models outperformed ChatGPT and RAG-augmented Llama-2 on:
- Open-domain QA
- Reasoning tasks
- Fact-verification

Yielded higher factual accuracy and citation precision.

---

### 5. Generate-then-Refine (Hybrid)

**Source**: [On the Capacity of Citation Generation by Large Language Models](https://arxiv.org/html/2410.11217v1)

Integrates pre-hoc and post-hoc methods:
- Generate with attribution capabilities
- Refine citations without altering response text

**Key finding**: Post-hoc methods only help models that lack attribution capabilities. For models with good attribution, post-hoc actually performs worse.

---

## Related Research (2025)

From the [HITsz-TMG survey](https://github.com/HITsz-TMG/awesome-llm-attributions):

- **SAFE**: "Improving LLM Systems using Sentence-Level In-generation Attribution" (Batista et al., May 2025)
- **Source Attribution in RAG**: Nematov et al. (July 2025)
- **Generation-Time vs. Post-hoc Citation**: Saxena et al. (September 2025) - the NeurIPS paper referenced above

---

## Proposed Solutions for Our Engine

### Option A: Interleaved Mode (ReClaim-style)

Agent alternates between citing and writing:

```python
# Agent workflow
"From source X page Y..." → cite internally
"The regulation requires Z [1]" → write with citation
"Section 4.2 states..." → cite internally
"This implies W [2]" → write with citation
```

**Pros**: Natural flow, high accuracy
**Cons**: Requires changing agent prompting/workflow

---

### Option B: Post-hoc Grounding (CEG-style)

New tool that takes written text and adds citations:

```python
ground_text(
    text="The regulation requires X. This ensures compliance with Y.",
    sources=[source_1, source_2]
)
# Returns: "The regulation requires X [1]. This ensures compliance with Y [2]."
# Automatically creates citations, regenerates if claims unsupported
```

**Pros**:
- Matches natural writing flow
- Training-free
- Can be added as plugin to existing engine

**Cons**:
- Additional latency from verification loop
- May require regeneration

---

### Option C: Claim-First Citation Tool

Tool that returns claim with citation embedded:

```python
cite_claim(
    claim="The regulation requires X",
    source_id=1,
    page=5,
    quote_context="..."
)
# Returns: "The regulation requires X [1]"
```

Agent uses return value directly in output text.

**Pros**: Simple to implement
**Cons**: Still requires knowing what to cite before writing

---

### Option D: Deferred Resolution

Agent writes with placeholders, resolved in post-processing:

```markdown
The regulation requires X {cite:source_1:p5}. This ensures Y {cite:source_2:s3}.
```

Post-processor resolves to:

```markdown
The regulation requires X [1]. This ensures Y [2].
```

**Pros**: Completely natural writing flow
**Cons**: Requires post-processing step, placeholder syntax

---

## Recommendation

Based on the research, **Option B (Post-hoc Grounding)** appears most promising:

1. Matches the natural writing flow
2. Achieves higher coverage (74-99% vs 37-65%)
3. Training-free, can be added as plugin
4. Aligns with CEG approach that's been validated in literature

The key insight from the NeurIPS paper is that P-Cite methods achieve **twice the source coverage** while maintaining reasonable precision - exactly what's needed for high-stakes applications like compliance and requirement traceability.

---

## References

1. [Generation-Time vs. Post-hoc Citation: A Holistic Evaluation of LLM Attribution](https://arxiv.org/html/2509.21557) - NeurIPS 2025
2. [Ground Every Sentence (ReClaim)](https://arxiv.org/abs/2407.01796) - Interleaved generation
3. [Citation-Enhanced Generation for LLM-based Chatbots](https://arxiv.org/html/2402.16063v3) - ACL 2024
4. [On the Capacity of Citation Generation by LLMs](https://arxiv.org/html/2410.11217v1) - Generate-then-Refine
5. [Perplexity Architecture](https://www.frugaltesting.com/blog/behind-perplexitys-architecture-how-ai-search-handles-real-time-web-data)
6. [HITsz-TMG LLM Attributions Survey](https://github.com/HITsz-TMG/awesome-llm-attributions)
7. [How OpenAI, Gemini, and Claude Use Agents for Deep Research](https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use)
