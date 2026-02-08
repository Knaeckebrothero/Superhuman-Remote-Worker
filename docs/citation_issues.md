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

### Root Cause: The "Bookmark Effect"

The disconnect is not a prompting bug — it is a fundamental architectural symptom of "Generation-Time" (G-Cite) workflows. When the agent calls `cite_document()` and receives a token ID (e.g., `[1]`), this ID competes for attention with the semantic content of the answer. By the time the agent writes the relevant claim — often several paragraphs later — the attention weight of the specific ID has decayed or been overwritten by the need to maintain narrative coherence. The agent "knows" the source exists but fails to surface the pointer in the output stream.

### The Cost of Verification Loops

The current system uses synchronous verification where every citation is LLM-checked in real-time. For a 10-paragraph report citing 20 sources, this means 20 verification pauses during generation. When the agent fails to embed a citation, it enters a "correction loop" — re-reading its output and attempting to insert missing `[N]` markers. These loops are notoriously inefficient: the agent often hallucinates *new* citations or places the original ID in the wrong location.

---

## Industry Research (January 2026)

### Two Main Paradigms

A [NeurIPS 2025 paper](https://arxiv.org/html/2509.21557) identifies two fundamental approaches:

| Aspect | G-Cite (Generation-Time) | P-Cite (Post-hoc) |
|--------|--------------------------|-------------------|
| How it works | Citations generated alongside text | Citations added after drafting |
| Cognitive load | **High** — model tracks prose flow + source IDs simultaneously | **Low** — model focuses on synthesis; auditing is separate |
| Coverage | 37-65% | **74-99%** |
| Precision | Variable (12-94%), prone to "hallucinated citations" | High baseline (26-75%), up to 90% with advanced tools |
| Hallucination rate | 41% avg | 37% avg |
| Latency | Low (single pass) | Higher (verification pass), but faster than "correction loops" |
| Best for | Chatbots requiring instant response | **Compliance reports, audit trails, deep research** |

**Key finding**: P-Cite achieves roughly **twice the source coverage** while maintaining reasonable precision.

**Conclusion for our system**: For compliance report workflows where accuracy is non-negotiable and latency is secondary, **P-Cite is the only viable architecture**. The latency cost of the post-hoc step is negligible compared to the value of auditability and the risk of hallucination.

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

### 3. Perplexity AI - The "Answer Engine" Architecture

**Source**: [Perplexity Architecture Analysis](https://www.frugaltesting.com/blog/behind-perplexitys-architecture-how-ai-search-handles-real-time-web-data), [AI-First Search API](https://research.perplexity.ai/articles/architecting-and-evaluating-an-ai-first-search-api)

Perplexity represents the industry gold standard for citation-backed generation. Its architecture directly solves the "bookmark problem":

1. **Query Decomposition**: Breaks user prompt into multiple sub-queries executed in parallel
2. **Snippet Extraction & Indexing**: Does *not* ingest full documents. Extracts atomic **snippets** (specific paragraphs or sentences) and indexes them temporarily for the session
3. **Constraint-Based Generation**: LLM is instructed via system prompt: "Do not say anything that you cannot ground in the retrieved snippets" — creates a hard boundary preventing reliance on pre-trained knowledge
4. **Sentence-Level Attribution**: Post-processing step aligns generated sentences with retrieved snippets using semantic similarity, ensuring `[N]` is physically attached to the claim it supports

**Key differentiator**: ChatGPT is generation-centric with retrieval bolted on. Perplexity is search-native with generation serving retrieval.

**Solving the Bookmark Problem**: By breaking documents into atomic snippets *before* the LLM sees them, the "bookmark" is not a pointer to a 50-page PDF but to a 3-sentence snippet. This reduces ambiguity and makes the linkage (citation) statistically easier for the model to reconstruct.

---

### 4. Google AGREE - Tuning for Self-Grounding

**Source**: [Effective Large Language Model Adaptation for Improved Grounding](https://research.google/blog/effective-large-language-model-adaptation-for-improved-grounding/) (Google Research)

AGREE (Adaptation for GRounding EnhancEment) demonstrates that "bolting on" citations to a standard instruction-tuned model is often insufficient — standard models are trained to be *helpful*, not strictly *factual*. AGREE fine-tunes the LLM to "self-ground" claims.

- **Test-Time Adaptation (TTA)**: If the model generates a claim that lacks support during the drafting phase, it can iteratively retrieve passages to support that specific claim *before* finalizing the output.

**Implication for our system**: Simply prompting an agent to "cite your sources" may fail if the underlying model hasn't been fine-tuned for attribution. A post-hoc tool that acts as the "AGREE" layer — verifying and retrieving support for ungrounded claims — is a necessary architectural addition to emulate this capability without training a custom model.

---

### 5. PaperQA2 & SciRAG - Recall-Summarize-Generate

**Source**: [PaperQA2](https://github.com/Future-House/paper-qa) (FutureHouse), [SciRAG](https://arxiv.org/abs/2511.14362) (Yale NLP)

For dense technical/compliance documents, PaperQA2 introduces **Ranking and Contextual Summarization (RCS)**:

1. **Retrieve**: Fetch top-k chunks based on the query
2. **Summarize**: A separate LLM call summarizes *each* chunk specifically in the context of the user's question, including citation metadata explicitly
3. **Generate**: The writer agent receives these *summaries* (rich in signal, low in noise) rather than raw text. Citations are baked into the summaries (e.g., "According to [Author, 2024], X is true..."), making it trivial for the writer to carry them to the final report

This "summarize-first" approach is particularly effective for compliance reports where raw legal text is convoluted. The summary step acts as a translation layer, converting legalese into "citable facts" that the agent can easily handle.

**SciRAG** adds **Citation Graph Expansion** — if Document A is cited, it also checks documents *cited by* A. Powerful for legal/compliance workflows where regulations cross-reference each other.

---

### 6. Self-RAG - Reflection Tokens

**Concept**: Trains LLM to generate special "reflection" tokens that trigger on-demand retrieval and self-critique.

**Results**: 7B and 13B Self-RAG models outperformed ChatGPT and RAG-augmented Llama-2 on:
- Open-domain QA
- Reasoning tasks
- Fact-verification

Yielded higher factual accuracy and citation precision.

---

### 7. Generate-then-Refine (Hybrid)

**Source**: [On the Capacity of Citation Generation by Large Language Models](https://arxiv.org/html/2410.11217v1)

Integrates pre-hoc and post-hoc methods:
- Generate with attribution capabilities
- Refine citations without altering response text

**Key finding**: Post-hoc methods only help models that lack attribution capabilities. For models with good attribution, post-hoc actually performs worse.

---

## Related Research (2025-2026)

From the [HITsz-TMG survey](https://github.com/HITsz-TMG/awesome-llm-attributions):

- **SAFE**: "Improving LLM Systems using Sentence-Level In-generation Attribution" (Batista et al., May 2025)
- **Source Attribution in RAG**: Nematov et al. (July 2025)
- **Generation-Time vs. Post-hoc Citation**: Saxena et al. (September 2025) - the NeurIPS paper referenced above
- **CaRR**: Citation-Aware Rubric Rewards — RL framework using fine-grained rubrics (Comprehensiveness: did it cite all relevant docs? Connectivity: do citations logically support the reasoning chain?) instead of binary rewards. Even without training, these rubrics can be used as an **Evaluator Node** (LLM-as-a-Judge) to score drafts before showing them to users. ([arXiv](https://arxiv.org/abs/2601.06021))

---

## The Mechanics of Post-Hoc Grounding

The transformation of a raw draft into a cited report happens in the **Grounding Pipeline**: Segmentation → Retrieval/Matching → Verification.

### Step 1: Claim Segmentation

The atomic unit of a citation is not a paragraph or sentence, but a **Claim** — a minimal, declarative statement that can be independently verified.

**Why sentence splitting is insufficient**: Complex sentences mix verifiable facts with reasoning, opinion, or conditional logic. Example: "Although the board, which met in July, approved the merger, the regulatory filing was delayed." — this contains three potential claims: the meeting date, the approval, and the delay.

**Proposition-Level Segmentation** (e.g., the [PropSegmEnt](https://www.semanticscholar.org/paper/PropSegmEnt) corpus) decomposes prose into atomic claims:

- *Tooling*: Lightweight, instruction-tuned LLMs (flan-t5 or Llama-3-8B) rewrite complex sentences into lists of simple statements
- *Prompt Pattern*: "Extract all verifiable factual claims from this text as a list of independent sentences. Ignore opinions and transitions."

**Recommendation**: Implement a dedicated `segment_claims` step. Use a fast, small LLM to "explode" the draft into a list of propositions. This list becomes the checklist for the Auditor.

### Step 2: Matching Algorithms (Connecting Claims to Sources)

**Dense X Retrieval**: Standard RAG retrieves chunks of 200-500 tokens, but the retrieval unit should match the granularity of the query (the claim). A 10-word claim is difficult to match against a 500-word paragraph using vector similarity because the paragraph's vector is dominated by noise.

- **Solution**: Index individual **propositions** (atomic facts) extracted from source documents. When the agent generates a specific claim, the retriever matches it against a database of specific facts. Use a library like LlamaIndex's DenseXRetrievalPack or propositional-retrieval in LangChain.

**Hybrid Retrieval** is non-negotiable for compliance reports:
- **Dense Retrieval (Embeddings)**: Essential for semantic matching (e.g., "Revenue increased" → "Sales went up"). Use models like bge-en-large or Contriever.
- **Sparse Retrieval (BM25)**: Essential for keyword precision (e.g., matching specific regulatory codes like "Section 404(b)" or distinct entity names).
- **Strategy**: Perform both retrievals, fuse using **Reciprocal Rank Fusion (RRF)**, pass top candidates to verification.

### Step 3: Verification (The Judge)

The most critical step — verifying the selected source *actually supports* the claim. This guards against "hallucinated citations" where the model cites a real document that is irrelevant to the claim.

**MiniCheck** ([GitHub](https://github.com/Liyan06/MiniCheck)) is a breakthrough in efficient fact-checking:
- A specialized small model (MiniCheck-FT5, 770M parameters) trained on the **LLM-AggreFact** dataset
- Outputs a binary Support / Not-Support label for a (Claim, Document) pair
- Achieves **GPT-4 level accuracy** but is **400x cheaper**
- Cost efficiency makes it feasible to check every single claim in a long report

```python
from minicheck import MiniCheck
scorer = MiniCheck(model_name='Bespoke-MiniCheck-7B', enable_prefix_caching=True)

pred_label, raw_prob, _, _ = scorer.score(docs=[retrieved_chunk], claims=[agent_claim])

if pred_label == 1:
    insert_citation(claim_id, chunk_id)
else:
    mark_as_unverified(claim_id)
```

**AlignScore** ([GitHub](https://github.com/yuh-zha/AlignScore)) provides a continuous score (0-1) representing factual alignment between two texts. Particularly useful for detecting subtle hallucinations where the agent slightly twists meaning (e.g., "The regulation *recommends* X" vs. "The regulation *requires* X"). A high threshold on AlignScore can filter out these nuanced errors.

**Reference-Blind Verification**: Crucially, do **not** provide the NLI model with the agent's *reasoning*. It must verify the link purely on evidence. If the NLI model says "No Entailment," the citation is flagged as a hallucination, regardless of how confident the agent was.

### Handling Multi-Source Synthesis

Agents often synthesize from multiple sources (e.g., "While US regulations require X, EU regulations mandate Y"). The system must:
- Identify that a single sentence needs **multiple source IDs**
- Not stop at the first match — the segmenter splits it into two propositions ("US requires X" and "EU requires Y")
- Find a source for each
- Re-assemble: "While US regulations require X [1], EU regulations mandate Y [2]."

---

## Agentic Workflow & Tooling Patterns

### Pattern 1: The "Placeholder Resolution" Pattern

The most practical pattern for solving our specific problem. It decouples the *intent* to cite from the *action* of citing.

1. **Drafting Node**: Agent writes text freely, optionally inserting **Placeholders** (e.g., `{cite: revenue_2023}` or `{check: "compliance requirement X"}`). The *implicit variant*: agent just writes prose, and the segmentation step treats every sentence as a potential placeholder.
2. **Resolution Node**: A deterministic tool or "Resolver Agent" scans the draft, parses placeholders/segments, generates retrieval queries, and calls the Retrieval Tool in parallel (batch processing).
3. **Grounding Node**: MiniCheck verifies retrieved content against claims.
4. **Formatting Node**: Replaces placeholders/claims with official `[N]` markers and appends bibliography.

**REWOO Pattern** (Reasoning WithOut Observation): The agent generates a full *plan* with placeholders (Step 1: Get revenue. Step 2: Get headcount. Step 3: Calculate ratio). Tools run in parallel to fill these variables. The final LLM call sees the filled variables and writes the report. The agent cannot "forget" the citation because the variable *is* the citation.

### Pattern 2: The "Audit Tool"

Instead of `cite_document()`, provide the agent with an **Audit Tool**:

- **Function**: `audit_draft(draft_text: str)`
- **Behavior**:
  1. Agent writes a paragraph and passes it to `audit_draft`
  2. The tool (running logic, not just an LLM) segments the text, retrieves sources, and verifies claims
  3. **Return Value**: The *annotated text* (with `[N]` inserted) AND a report of "Unverified Claims."
  4. **Agent Reaction**: The agent sees "Claim X was unverified" and either rewrites the sentence to be less specific or deletes it.

### Pattern 3: The "Claim-to-Source" Tool

- **Function**: `find_evidence(claim: str)` → `(source_id, quote, confidence_score)`
- **Usage**: Used by the **Resolver Node** (not the writer). The writer creates the claim; the Resolver finds the backing.

### LangGraph Implementation Strategy

Proposed graph structure for the grounding pipeline:

1. **Writer Node**: Generates initial draft using retrieved context
2. **Segmenter Node**: Breaks draft into atomic claims
3. **Verifier Node**: For each claim: Retrieve → MiniCheck → Assign Status (Verified/Hallucinated)
4. **Editor Node** (Conditional):
   - If all verified: Proceed to Formatting
   - If hallucinations found: Pass back to Writer with specific feedback: "The claim 'X' could not be verified. Please rewrite or remove."

This **Generate → Verify → Refine** loop ensures 100% precision. The "Editor" acts as a guardrail, preventing the system from outputting uncited text.

---

## Agent Prompt Engineering for Citations

### "Citation-Aware" Prompting

Adopt an **Academic Persona** in the system prompt:

> "You are a rigorous compliance auditor. You must write in an academic style. Every factual statement is a hypothesis until verified. Write clearly and precisely to facilitate verification."

This primes the model to avoid flowery, untestable language and focus on concrete propositions.

### Chain-of-Thought (CoT) with Citations

Use CoT to plan citations *before* writing the sentence. Even better, use **Structured Output** (JSON):

```json
{
  "thought": "Identifying revenue figures...",
  "claim": "The revenue was $5M",
  "potential_source_keywords": ["2023 financial report", "revenue"]
}
```

This allows the Resolver Node to use the `potential_source_keywords` for targeted retrieval.

### Reasoning Models (o1, DeepSeek R1)

Reasoning models are less prone to "forgetting" citations because their thinking process allows them to iterate on the structure before committing to output tokens.

- **Workflow**: Use DeepSeek R1/o1 as the **Planner Node** — ask it to outline the report and identify *what evidence is needed* for each section. Use a cheaper, faster model (GPT-4o, Sonnet 3.5) to execute the writing based on the plan and retrieved evidence.
- **Benefit**: Reasoning models are "citation-aware" in their training. They can deduce which claims are controversial and require stronger evidence (e.g., multiple sources) versus simple facts.

---

## Verification and Quality Assurance

### ALCE Benchmark Standards

The [ALCE](https://github.com/princeton-nlp/ALCE) (Automatic LLMs' Citation Evaluation) benchmark defines the industry standard metrics:

| Metric | Definition | Implementation Goal |
|--------|-----------|-------------------|
| Citation Precision | % of citations that *actually support* the associated statement | **Target > 95%**. Use MiniCheck to measure continuously. |
| Citation Recall | % of verifiable statements in the answer that *have* a citation | **Target > 90%**. Use an Auditor agent to scan for uncited claims. |
| Citation F1 | Harmonic mean of Precision and Recall | The single "North Star" metric for the system. |

### Measuring Hallucination

Implement **Reference-Blind Verification**:

1. Take the agent's claim
2. Take the cited document text
3. Ask an NLI model (MiniCheck): "Does the text entail the claim?"
4. *Crucially*: Do **not** provide the NLI model with the agent's *reasoning*. It must verify the link purely on evidence. If the NLI model says "No Entailment," the citation is flagged as a hallucination, regardless of how confident the agent was.

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
audit_and_ground(
    draft_text="The regulation requires X. This ensures compliance with Y.",
    context_scope=["source_1_id", "source_2_id"]  # optional
)
# Returns: CitationAuditResponse with:
#   grounded_text: "The regulation requires X [1]. This ensures compliance with Y [2]."
#   citations: [VerifiedCitation(...), VerifiedCitation(...)]
#   unverified_claims: ["..."]
```

**Agent instruction change**: "Write the report naturally. Do not worry about citation IDs. After writing a section, call `audit_and_ground` to verify and cite your work."

**Pros**:
- Matches natural writing flow
- Training-free, can be added as plugin to existing engine
- Achieves 74-99% coverage vs 37-65%
- Eliminates "verification theater" and correction loops
- Decouples writing quality from citation accuracy

**Cons**:
- Additional latency from verification pass (but faster than correction loops)
- Requires building claim segmentation + retrieval infrastructure (significant new code)
- Agent loses control over citation placement — the grounding pipeline decides where `[N]` goes
- Writing quality may suffer because the agent doesn't engage with source material while writing

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

### Option D: Deferred Resolution (Placeholder Pattern)

Agent writes with placeholders, resolved in post-processing:

```markdown
The regulation requires X {cite:source_1:p5}. This ensures Y {cite:source_2:s3}.
```

Post-processor resolves to:

```markdown
The regulation requires X [1]. This ensures Y [2].
```

**Pros**: Completely natural writing flow, agent cannot "forget" because the placeholder *is* the citation intent
**Cons**: Requires post-processing step, placeholder syntax education

---

### Option E: Academic Workflow — Plan with Citations, Refresh Before Writing — RECOMMENDED

This approach mirrors how experienced human academics write papers. Rather than creating citations as an afterthought (post-hoc) or juggling `[N]` IDs while writing (generation-time), the agent follows the same structured process a researcher would:

#### The Workflow

**Phase 1 — Read & Cite During Planning (Strategic)**

The agent reads sources and creates citations while building a source-backed outline. Each bullet point in the outline is already grounded in evidence:

```markdown
## 3. Compliance Architecture

- GoBD requires immutable audit trails for all tax-relevant data [1]
  - 10-year retention for bookkeeping records, 6-year for business letters [2]
- GDPR Art. 17 creates a tension: right to erasure vs. retention obligations [3]
  - Resolution: pseudonymization allows retention without personal data exposure [4]
- Technical implementation must separate audit logs from operational data [1]
```

At this point, `[1]`, `[2]`, `[3]`, `[4]` are real citation IDs returned by `cite_document()` during the planning phase. The agent doesn't need to hold them in memory long-term — they're written into the outline file.

**Phase 2 — Refresh & Write (Tactical)**

When converting the outline to prose, the agent does NOT try to remember what `[1]` means. Instead, before writing each section, it calls `get_citation()` for every citation it plans to use in that section:

```python
# Before writing Section 3, the agent calls:
get_citation(1)  # → Returns: source name, verbatim quote, page, context, notes
get_citation(2)  # → Returns: source name, verbatim quote, page, context, notes
get_citation(3)  # → ...
get_citation(4)  # → ...
```

With the full source context fresh in its context window, the agent writes the prose section. The citation IDs don't decay because the agent just re-read them. The writing is informed by the actual source text, not by a vague memory of what `[1]` was about.

**Phase 3 — Verify (Strategic)**

After writing, the agent performs a reverse outline check: for each claim in the final text, confirm a citation is present and that the cited source actually supports the claim.

#### Why This Works Better Than Post-Hoc

The post-hoc approach (Option B) decouples writing from citing entirely. While this solves the bookmark effect, it introduces new problems:

- The agent writes without engaging with source material, so prose quality and accuracy of paraphrasing may suffer
- The grounding pipeline decides where citations go, not the agent — the agent loses authorial control over attribution
- It requires building entirely new infrastructure (claim segmentation, retrieval, NLI verification)

The academic workflow keeps the agent in control of its citations while solving the bookmark effect through **context refreshing** — the same thing a human does when they flip back to a source before writing about it.

#### Current Blocker: `get_citation()` Doesn't Return Enough Information

The workflow above requires `get_citation()` to return the full citation record. Currently it does not:

| Field | In Database | Returned by `get_citation()` |
|-------|------------|------------------------------|
| Source name | Yes | Yes |
| Claim text | Yes | Yes (truncated to 300 chars!) |
| **Verbatim quote** | Yes | **No** |
| **Quote context** (surrounding paragraph) | Yes | **No** |
| **Locator** (page, section) | Yes | **No** |
| **Relevance reasoning** | Yes | **No** |
| Verification status | Yes | Yes |
| Confidence | Yes | Yes |
| Similarity score | Yes | Yes |

The fix is straightforward — the data is already stored in the database, the tool just needs to include it in the formatted output. The agent needs to see something like:

```
Citation [1]

Source: [3] GoBD-Grundsätze.pdf
Claim: "GoBD mandates immutable audit trails for all tax-relevant digital records"
Quote: "Jede Änderung oder Löschung von steuerrelevanten Daten muss protokolliert und
        nachvollziehbar sein. Die Unveränderbarkeit der Aufzeichnungen ist sicherzustellen."
Context: Section 3.2, paragraph discussing Unveränderbarkeit requirements
Location: Page 24, Section 3.2
Language: de
Reasoning: Direct statement of the immutability requirement for audit trails

Status: VERIFIED | Confidence: high | Similarity: 0.95
```

With this information, the agent can write: "The GoBD mandates that all changes to tax-relevant data must be logged and traceable, requiring immutable record-keeping (GoBD-Grundsätze, Section 3.2) [1]."

#### Harvard Style Consideration

Switching to Harvard-style inline citations (e.g., `(Kautz, 2023)`) would further reduce the bookmark effect. Unlike opaque `[N]` markers, Harvard citations are **self-documenting** — the agent (and the reader) can see *who said what* without looking up a reference list. The citation engine already supports Harvard formatting via `format_citation(id, style='harvard')`, but this is currently only exposed as a bibliography export, not as an inline format during writing.

Harvard style would turn:

> The regulation requires immutable audit trails [1].

Into:

> The regulation requires immutable audit trails (GoBD-Grundsätze, 2024, S. 24).

This makes citations meaningful in context and eliminates the problem of the agent "forgetting" what `[1]` refers to — the citation itself carries the attribution.

#### Comparison with Other Options

| Criterion | Option B (Post-hoc) | Option E (Academic Workflow) |
|-----------|--------------------|-----------------------------|
| Solves bookmark effect | Yes (bypasses it entirely) | Yes (context refresh before writing) |
| Writing quality | Risk: agent writes without source context | Better: agent has source text fresh in context |
| Agent control over citations | Low — pipeline decides placement | High — agent places citations intentionally |
| Infrastructure needed | Large — segmentation, retrieval, NLI | Small — fix `get_citation()` output, update prompts |
| Time to implement | Weeks (new pipeline) | Days (tool output fix + prompt update) |
| Works with existing tools | No — needs new `audit_and_ground()` | Yes — uses existing `cite_document()` + `get_citation()` |
| Citation accuracy | High (NLI verification) | Depends on agent discipline (but already has verification) |
| Auditability | High (full audit trail) | Same — citations already stored with verification |

**Pros**:
- Mirrors how humans actually write academic papers
- Minimal code changes — fix `get_citation()` output + update agent prompts
- Agent stays in control of citation placement and paraphrasing
- Writing quality benefits from having source text in context during writing
- Works with the existing citation tool infrastructure
- No new pipeline or infrastructure needed

**Cons**:
- Relies on agent discipline (must call `get_citation()` before writing each section)
- Adds tool calls (one `get_citation()` per citation per section), increasing token usage
- Citation recall depends on outline quality — if a claim isn't in the outline, it won't get cited
- No automatic detection of uncited claims (unlike post-hoc scanning)

---

## Recommendation & Migration Roadmap

**Option E (Academic Workflow)** is the recommended primary approach. It solves the core problem with minimal changes to existing infrastructure and mirrors proven human writing methodology. Option B (Post-hoc Grounding) remains valuable as a future **safety net** — an optional verification pass after writing — but is not the primary citation mechanism.

### Phase 1: Fix `get_citation()` and Update Prompts (Immediate)

- **Objective**: Unblock the academic workflow by making citation lookup useful.
- **Action 1**: Update `get_citation()` in `src/tools/citation/sources.py` to return the full citation record: verbatim quote, quote context, locator (page/section), relevance reasoning, and untruncated claim text.
- **Action 2**: Update agent writing instructions to follow the plan-refresh-write cycle:
  1. During planning: read sources and create citations with `cite_document()`, build outline with `[N]` markers
  2. Before writing each section: call `get_citation()` for every citation used in that section to refresh context
  3. Write prose with source text fresh in context window
  4. After writing: verify each claim has its citation embedded
- **Action 3**: Consider switching inline citation format to Harvard style for self-documenting references.

### Phase 2: Post-hoc Verification Safety Net (Intermediate)

- **Objective**: Catch uncited claims that slipped through the academic workflow.
- **Action**: Build a lightweight `audit_draft(text)` tool that scans written text for claims without citations and flags them. This is simpler than full post-hoc grounding — it only *detects* missing citations, it doesn't *insert* them. The agent then decides how to fix gaps.
- **Logic**:
  1. Segment text into claims (small LLM)
  2. For each claim, check if a `[N]` marker is present
  3. For claims with citations, optionally verify the citation actually supports the claim (NLI check)
  4. Return a report: "3 claims without citations found. 1 citation may not support its claim."

### Phase 3: Full Pipeline Hardening (Advanced)

- **Objective**: Enterprise-grade reliability and auditability.
- **Action**: Implement **Dense X Retrieval** and **MiniCheck** as the verification backend.
- **Logic**:
  1. Re-index PostgreSQL documents using propositional indexing (atomic facts) for higher-precision retrieval
  2. Replace the LLM-based verification step with MiniCheck-7B for 400x cost reduction and higher accuracy
  3. Implement **ALCE metrics** in CI/CD pipeline to measure citation precision/recall on a gold-standard dataset of past reports
  4. Optionally add full `audit_and_ground()` as a fallback for agents/models that struggle with the academic workflow

---

## Tool Signatures (Recommended)

```python
from pydantic import BaseModel, Field
from typing import List, Optional

class CitationAuditRequest(BaseModel):
    """Request to audit and ground a generated draft."""
    draft_text: str = Field(..., description="The prose text generated by the agent.")
    context_scope: List[str] = Field(default=[], description="Optional list of doc IDs to restrict search to.")

class VerifiedCitation(BaseModel):
    claim_text: str
    source_id: str
    quote: str
    confidence: float
    is_hallucination: bool

class CitationAuditResponse(BaseModel):
    grounded_text: str = Field(..., description="Text with [N] citations inserted.")
    citations: List[VerifiedCitation]
    unverified_claims: List[str] = Field(..., description="Claims that could not be grounded.")
```

### Audit Trail Schema

```sql
CREATE TABLE citation_audit_trail (
    citation_id UUID PRIMARY KEY,
    report_id UUID,
    claim_text TEXT NOT NULL,          -- The agent's generated text
    source_id UUID,                    -- Link to the original document
    source_verbatim_text TEXT,         -- The specific snippet used for grounding
    retrieval_score FLOAT,             -- Dense/Sparse retrieval score
    verification_model VARCHAR(50),    -- e.g., 'MiniCheck-FT5'
    verification_confidence FLOAT,     -- Probability score from MiniCheck
    timestamp TIMESTAMP DEFAULT NOW()
);
```

---

## Recommended Libraries

| Category | Library | Notes |
|----------|---------|-------|
| **Verification** | [MiniCheck](https://github.com/Liyan06/MiniCheck) | GPT-4 accuracy, 400x cheaper, 770M params |
| **Retrieval** | [llama-index](https://github.com/edumunozsala/llamaindex-RAG-techniques) (DenseXRetrieval) or [PaperQA2](https://github.com/Future-House/paper-qa) | Propositional retrieval |
| **Alignment** | [AlignScore](https://github.com/yuh-zha/AlignScore) | Continuous factual alignment score |
| **Orchestration** | [LangGraph](https://docs.langchain.com/oss/python/langgraph/workflows-agents) | Cyclic verification loops |
| **Evaluation** | [ALCE](https://github.com/princeton-nlp/ALCE) or [ragas](https://docs.ragas.io/) | Citation precision/recall/F1 benchmarks |

---

## Database Configuration

The citation tool supports two database modes:

| Mode | Database | Use Case |
|------|----------|----------|
| `basic` | SQLite | Single-agent, local development |
| `multi-agent` | PostgreSQL | Production, shared database |

**Shared Database with Orchestrator**: The citation tool can use the same PostgreSQL database as the orchestrator by setting `CITATION_DB_URL` to the same connection string:

```bash
# .env
DATABASE_URL=postgresql://graphrag:graphrag_password@localhost:5432/graphrag
CITATION_DB_URL=postgresql://graphrag:graphrag_password@localhost:5432/graphrag
```

**How it works**:
- The orchestrator schema (`orchestrator/database/schema.sql`) already creates the `sources`, `citations`, and `schema_migrations` tables
- The citation tool's `CREATE TABLE IF NOT EXISTS` statements become no-ops
- Job isolation is automatic: `job_id` flows through `ToolContext` → `CitationContext` → INSERT
- Cascading deletes: when a job is deleted, all its citations/sources are cleaned up

**Schema differences** (orchestrator is stricter):
- Orchestrator: `job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE`
- Citation tool standalone: `job_id UUID` (nullable, no FK)

This means citations are properly isolated per job and can be queried:
```sql
SELECT * FROM citations WHERE job_id = 'your-job-uuid';
```

---

## References

1. [Generation-Time vs. Post-hoc Citation: A Holistic Evaluation of LLM Attribution](https://arxiv.org/html/2509.21557) - NeurIPS 2025
2. [Ground Every Sentence (ReClaim)](https://arxiv.org/abs/2407.01796) - Interleaved generation
3. [Citation-Enhanced Generation for LLM-based Chatbots](https://arxiv.org/html/2402.16063v3) - ACL 2024
4. [On the Capacity of Citation Generation by LLMs](https://arxiv.org/html/2410.11217v1) - Generate-then-Refine
5. [Perplexity Architecture](https://www.frugaltesting.com/blog/behind-perplexitys-architecture-how-ai-search-handles-real-time-web-data)
6. [Architecting and Evaluating an AI-First Search API](https://research.perplexity.ai/articles/architecting-and-evaluating-an-ai-first-search-api) - Perplexity Research
7. [HITsz-TMG LLM Attributions Survey](https://github.com/HITsz-TMG/awesome-llm-attributions)
8. [How OpenAI, Gemini, and Claude Use Agents for Deep Research](https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use)
9. [Effective Large Language Model Adaptation for Improved Grounding](https://research.google/blog/effective-large-language-model-adaptation-for-improved-grounding/) - Google AGREE
10. [MiniCheck: Efficient Fact-Checking of LLMs on Grounding Documents](https://aclanthology.org/2024.emnlp-main.499.pdf) - ACL Anthology
11. [MiniCheck](https://github.com/Liyan06/MiniCheck) - GitHub
12. [PaperQA2](https://github.com/Future-House/paper-qa) - FutureHouse
13. [SciRAG: Adaptive, Citation-Aware Retrieval and Synthesis](https://arxiv.org/abs/2511.14362) - arXiv
14. [PropSegmEnt: Proposition-Level Segmentation](https://www.semanticscholar.org/paper/PropSegmEnt%3A-A-Large-Scale-Corpus-for-Segmentation-Chen-Buthpitiya/72da4a646a31d72bcae90b916e120cd7df5f9dae) - Semantic Scholar
15. [Dense X Retrieval: Enhancing Retrieval in QA Systems](https://arxiv.org/html/2410.03754v1) - arXiv
16. [AlignScore: Evaluating Factual Consistency](https://github.com/yuh-zha/AlignScore) - GitHub
17. [ALCE: Automatic LLMs' Citation Evaluation](https://github.com/princeton-nlp/ALCE) - Princeton NLP
18. [REWOO Agent Pattern](https://agent-patterns.readthedocs.io/en/stable/patterns/rewoo.html) - Agent Patterns
19. [Chaining the Evidence: Citation-Aware Rubric Rewards (CaRR)](https://arxiv.org/abs/2601.06021) - arXiv
20. [AISAC: Integrated Multi-Agent System for Retrieval-Grounded Scientific Assistance](https://arxiv.org/html/2511.14043v1) - arXiv
21. [Audit-Trail Fabrication in Tool-Using LLM Agents (OLIF)](https://www.researchgate.net/publication/400395675) - ResearchGate
