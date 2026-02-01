# Finetuning Guide for Agent Improvement

This document outlines strategies for finetuning open-source LLMs (e.g., 120B parameter models) to improve agent behavior, tool use, and task completion reliability.

## Overview

Finetuning can address several agent-specific challenges:
- Todo completion discipline (forgetting to mark todos complete)
- File editing patterns (string replacement like Anthropic's Claude)
- Workspace file compactness
- Phase-appropriate behavior (strategic vs tactical)
- Custom tool schemas and usage patterns
- Obsidian-style workflows

## Hardware Requirements

### Full Finetuning (Not Recommended)

Full finetuning requires ~16GB VRAM per 1B parameters:

| Model Size | VRAM Required | Hardware |
|------------|---------------|----------|
| 7B | ~112 GB | 2x A100 80GB |
| 70B | ~1.1 TB | 14+ A100 80GB |
| 120B | ~1.9 TB | 24+ H100 80GB |

### QLoRA (Recommended)

QLoRA quantizes the frozen base model to 4-bit, training only low-rank adapter matrices. This reduces requirements by 50-70% while achieving 95-99% of full finetune quality.

| Model Size | VRAM (QLoRA) | Hardware |
|------------|--------------|----------|
| 7B | ~6 GB | Consumer GPU (RTX 3090) |
| 13B | ~16-24 GB | RTX 4090 / A10 |
| 70B | ~48 GB | Single A100 80GB |
| 120B | ~80-160 GB | 2-4x H100 80GB |

**Key insight**: Only 0.5-5% of parameters are trained with LoRA/QLoRA, making large model finetuning practical.

## Training Data Requirements

### Minimum Examples by Task Type

| Task Type | Examples Needed | Notes |
|-----------|-----------------|-------|
| Output formatting, tone | 100-500 | Straightforward pattern matching |
| Classification, extraction | 500-1,000 | Requires understanding categories |
| Tool calling patterns | 500-1,000 | JSON/XML structure critical |
| Complex multi-step behaviors | 1,000-5,000+ | Agent workflows, planning |
| Domain expertise | 2,000-10,000+ | Specialized knowledge |

### Quality vs Quantity

- High-quality data is critical: errors in training data cause **quadratic** increases in model errors
- Linear improvements observed with each doubling of dataset size
- Manual review of a subset before training is essential
- 200 rows minimum to see any benefits

## Tool Use Finetuning

Tool calling is a well-studied finetuning target with proven results.

### Key Findings

1. **Small models can achieve high accuracy**: 1B model improved from 10% → 79% tool-calling success in 15 minutes on a MacBook
2. **Specialized models outperform generalists**: The future of agents is specialized models for specialized tasks
3. **ToolACE (ICLR 2025)**: Multi-agent dialog generation for synthetic tool-use data with decomposed verification

### Training Objectives (Increasing Complexity)

1. Single-turn forced function call
2. Parallel function calling
3. Nested function calls
4. Multi-turn chat with optional function calls
5. Full agentic workflows with planning

### Data Format for Tool Calls

```jsonl
{"messages": [
  {"role": "system", "content": "You have access to the following tools: ..."},
  {"role": "user", "content": "Read the file config.yaml"},
  {"role": "assistant", "content": null, "tool_calls": [{"name": "read_file", "arguments": {"path": "config.yaml"}}]},
  {"role": "tool", "content": "file contents here..."},
  {"role": "assistant", "content": "The config file contains..."}
]}
```

## Synthetic Data Generation

Using a powerful model (Claude Opus, GPT-4) as a "teacher" to generate training data for a smaller "student" model.

### Best Practices

1. **Decouple generation steps**: Separate question generation from response generation
2. **Use agentic workflows**: Reflection and iteration improve data quality
3. **Hybrid approach**: Human-curated examples establish correctness, LLM scales the dataset
4. **Validation pipeline**:
   - Statistical verification (format, completeness)
   - Human-in-the-loop assessment for edge cases
5. **Evolve queries**: Generate initial queries, then "evolve" them multiple times to increase complexity and realism

### Teacher-Student Pipeline

```
1. Define target behaviors with human-written examples (50-100)
2. Use Claude Opus to generate variations (10x-100x expansion)
3. Filter and validate generated examples
4. Manual review of random subset (10-20%)
5. Format as JSONL for training
```

### Data Sources for This Project

| Behavior | Data Source | Generation Method |
|----------|-------------|-------------------|
| Todo completion | MongoDB audit trail | Extract successful job completions |
| File editing | Git diffs + context | Generate edit pairs with explanation |
| Workspace compactness | Before/after examples | Manual curation + LLM expansion |
| Phase transitions | Job logs | Extract strategic→tactical handoffs |
| Tool selection | Audit trail | Successful tool sequences |

## Agent-Specific Finetuning Targets

### 1. Todo Management Discipline

**Problem**: Agent forgets to mark todos complete after finishing work.

**Training data structure**:
```jsonl
{"messages": [
  {"role": "user", "content": "Complete the task: Write unit tests for auth module"},
  {"role": "assistant", "content": "I'll write the tests now.", "tool_calls": [...]},
  {"role": "tool", "content": "Tests written successfully"},
  {"role": "assistant", "content": null, "tool_calls": [{"name": "todo_complete", "arguments": {"notes": "Added 5 unit tests covering login, logout, and token refresh"}}]}
]}
```

**Negative examples** (what NOT to do):
```jsonl
{"messages": [...task completion without todo_complete call...], "label": "rejected"}
```

### 2. String Replacement / File Editing

**Problem**: Model generates full file rewrites instead of targeted edits.

**Training approach**:
- Show before/after file states
- Train on minimal edit patterns
- Include context window constraints

### 3. Phase-Appropriate Behavior

**Strategic phase training**:
- Planning and decomposition
- Workspace.md updates
- `next_phase_todos` usage
- `job_complete` only in strategic

**Tactical phase training**:
- Direct execution
- `todo_complete` after each task
- No `job_complete` calls
- Focused tool usage

### 4. Compact File Writing

**Problem**: Agent writes verbose files, filling context window.

**Training approach**:
- Examples of concise vs verbose for same content
- Preference data for DPO training
- Token budget constraints in prompts

## Architecture Options

### Option 1: Single Finetuned Model

Simplest approach - one model handles all behaviors.

**Pros**: Simple deployment, single checkpoint
**Cons**: May have conflicting objectives, larger training set needed

### Option 2: Phase-Specific Models

Separate models for strategic and tactical phases.

```
Strategic Model (Planning)     Tactical Model (Execution)
├── Workspace management       ├── Tool execution
├── Todo creation              ├── Todo completion
├── Goal decomposition         ├── File operations
└── Job completion             └── Direct task work
```

**Pros**: Specialized behavior, smaller datasets per model
**Cons**: Complex deployment, model switching overhead

### Option 3: Toolshim Architecture

Keep base model frozen, finetune a small "shim" model for tool formatting.

```
User Query → Base Model (frozen) → Toolshim (finetuned) → Structured Tool Calls
```

**Pros**: Preserves base model capabilities, minimal finetuning
**Cons**: Additional inference step, potential latency

### Option 4: Mixture of Experts (Advanced)

Route to specialized expert models based on task type.

**Pros**: Best of all worlds
**Cons**: Complex, requires routing logic

## Recommended Tools & Stack

### Training Frameworks

| Tool | Description | Best For |
|------|-------------|----------|
| [Unsloth](https://github.com/unslothai/unsloth) | 2x faster, 80% less memory | Quick experiments, single GPU |
| [LlamaFactory](https://github.com/hiyouga/LLaMA-Factory) | 100+ models, config-based | Production training |
| [Axolotl](https://github.com/OpenAccess-AI-Collective/axolotl) | Flexible, many techniques | Advanced users |
| [TRL](https://github.com/huggingface/trl) | Hugging Face official | Integration with HF ecosystem |

### Key Libraries

```bash
pip install peft bitsandbytes transformers datasets accelerate
pip install unsloth  # For optimized training
```

### Training Configuration (QLoRA)

```yaml
# Example QLoRA config for 70B model
model:
  base_model: "meta-llama/Llama-3.1-70B"
  quantization: "4bit"  # QLoRA

lora:
  r: 64                 # Rank (higher = more params, better quality)
  lora_alpha: 128       # Scaling factor
  lora_dropout: 0.05
  target_modules:       # Which layers to adapt
    - q_proj
    - k_proj
    - v_proj
    - o_proj
    - gate_proj
    - up_proj
    - down_proj

training:
  learning_rate: 2e-4   # Higher than full finetune
  batch_size: 4
  gradient_accumulation_steps: 8
  epochs: 3
  warmup_ratio: 0.03

optimization:
  gradient_checkpointing: true
  flash_attention: true
```

## Cost Estimates

### Cloud GPU Pricing (Approximate, 2025-2026)

| Provider | GPU | $/hour | 70B QLoRA (10h) | 120B QLoRA (20h) |
|----------|-----|--------|-----------------|------------------|
| RunPod | A100 80GB | $1.99 | ~$20 | ~$80 (2 GPUs) |
| Lambda | H100 80GB | $2.49 | ~$25 | ~$100 (2 GPUs) |
| AWS | p5.48xlarge | $98.32 | ~$1000 | ~$2000 |
| Vast.ai | A100 80GB | $1.50 | ~$15 | ~$60 (2 GPUs) |

**Note**: Actual costs vary based on dataset size, epochs, and optimization.

### Data Generation Costs

Using Claude Opus for synthetic data generation:
- ~$15/M input tokens, ~$75/M output tokens
- 1,000 training examples ≈ $5-20 depending on complexity
- 10,000 training examples ≈ $50-200

## Implementation Roadmap

### Phase 1: Data Collection (Week 1-2)

1. [ ] Export successful job traces from MongoDB audit trail
2. [ ] Identify patterns in todo completion failures
3. [ ] Collect file editing examples from git history
4. [ ] Create human-curated seed examples (50-100 per behavior)

### Phase 2: Synthetic Data Generation (Week 2-3)

1. [ ] Design prompts for Claude Opus data generation
2. [ ] Generate 10x expansion of seed examples
3. [ ] Implement validation pipeline
4. [ ] Manual review and filtering

### Phase 3: Training Experiments (Week 3-4)

1. [ ] Set up training environment (RunPod/Lambda)
2. [ ] Start with smaller model (7B-13B) for quick iteration
3. [ ] Evaluate on held-out test set
4. [ ] Scale to target model size (70B-120B)

### Phase 4: Integration & Evaluation (Week 4-5)

1. [ ] Integrate finetuned model with agent framework
2. [ ] A/B testing against base model
3. [ ] Measure: todo completion rate, edit quality, phase adherence
4. [ ] Iterate based on results

## Evaluation Metrics

| Metric | Description | Target |
|--------|-------------|--------|
| Todo completion rate | % of todos marked complete after task done | >95% |
| Edit precision | Minimal diff vs unnecessary changes | <10% extra tokens |
| Phase adherence | Correct tool usage per phase | >98% |
| Task success rate | Jobs completing successfully | >90% |
| Token efficiency | Average tokens per task | -20% vs baseline |

## References

### Research Papers

- [ToolACE: Winning the Points of LLM Function Calling](https://proceedings.iclr.cc/paper_files/paper/2025/file/663865ea167425c6c562cb0b6bcf76c7-Paper-Conference.pdf) (ICLR 2025)
- [How Much Data is Enough Data? Fine-Tuning LLMs](https://arxiv.org/abs/2409.03454)
- [Parameter-efficient fine-tuning in large language models: a survey](https://link.springer.com/article/10.1007/s10462-025-11236-4)

### Practical Guides

- [Fine-Tune LLM Agent for Tool Use with Hugging Face](https://kyrylai.com/2025/04/01/fine-tune-llm-agent-tool-use-huggingface/)
- [Fine-tuning LLMs for function-calling](https://wandb.ai/wandb/function-calling-finetuning/reports/Fine-tuning-LLMs-for-function-calling--VmlldzoxMjgxMTgxMg)
- [QLoRA - How to Fine-Tune an LLM on a Single GPU](https://towardsdatascience.com/qlora-how-to-fine-tune-an-llm-on-a-single-gpu-4e44d6b5be32/)
- [Synthetic Data Generation Strategies for Fine-Tuning LLMs](https://scale.com/blog/synthetic-data-fine-tuning-llms)
- [Finetuning Toolshim Models for Tool Calling](https://block.github.io/goose/blog/2025/04/11/finetuning-toolshim/)

### Tools & Frameworks

- [Unsloth GitHub](https://github.com/unslothai/unsloth)
- [LlamaFactory GitHub](https://github.com/hiyouga/LLaMA-Factory)
- [Hugging Face PEFT](https://github.com/huggingface/peft)
- [Tool Use & Function Calling - RLHF Book](https://rlhfbook.com/c/14.5-tools)
