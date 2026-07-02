# **Architecting Cross-Model Subagent Delegation Interfaces**

The orchestration of multi-tier artificial intelligence agent systems increasingly relies on decoupled, model-agnostic architectures. Within these systems, a parent agent must seamlessly delegate well-scoped, ephemeral subtasks to disposable subagents. This "fire-and-collect" paradigm—where subagents execute bounded ReAct loops, utilize a subset of inherited tools, and return finalized text results without requiring human review or long-lived lifecycles—demands a highly robust interaction protocol. When the orchestration layer must operate across a heterogeneous fleet of frontier models, the interface design of the delegation tool becomes the single most critical point of failure or success.  
The primary challenge lies in establishing a universal tool schema and invocation pattern exposed interchangeably to models with vastly different underlying architectures, parsing engines, and training distributions. The target fleet includes MiniMax M3, Zhipu GLM-5.2, OpenAI GPT-5.5, Anthropic Claude Opus, Google Gemini, Moonshot Kimi K2.5, Qwen, and DeepSeek. The delegation interface must reliably coerce accurate tool selection, precise argument formatting, and optimal parallel concurrency from all of these model families simultaneously. An interface optimized solely for OpenAI's function-calling implementation will inevitably degrade when exposed to the XML-translation layers of MiniMax or the strict role-alternation requirements of DeepSeek.  
This report provides an exhaustive analysis of subagent delegation interfaces, synthesizing data from a comprehensive survey of production-grade agent harnesses. It delivers actionable, cross-model recommendations for invocation patterns, semantic tool naming, argument schema design, and behavioral steering mechanisms. The analysis culminates in a concrete, cross-model compatible tool definition designed to maximize resilience across the modern frontier model ecosystem.

## **Comparative Survey of Frontier Agent Harnesses**

To establish a baseline for architectural best practices, it is necessary to analyze how leading enterprise agent frameworks and model-native harnesses implement subagent delegation. The design philosophies across the ecosystem diverge significantly, particularly regarding whether delegation is treated as a conversational multi-turn handoff, a programmatic map-reduce operation, or a strict functional tool call.

| Harness Implementation | Delegation Primitive / Tool Name | Invocation Pattern | Argument Schema Strategy | Result Formatting and Integration | Notable Design Rationale |
| :---- | :---- | :---- | :---- | :---- | :---- |
| **Anthropic Claude Code** | Agent (formerly Task) | Iterative parallel tool calls. | Target subagent type/ID and an isolated string prompt. | Verbatim string output returned via standard tool response. | Prioritizes complete context isolation. The parent receives only the final summary to prevent transcript pollution, a pattern evaluated as highly effective for parallel codebase exploration1. |
| **OpenAI Agents SDK** | Agent.as\_tool() and handoff() | Iterative function calling. | Highly customizable, Pydantic-driven structured inputs. | Structured or text output synthesized back to the parent context. | Separates "handoffs" (transfer of conversational control) from "agents as tools" (manager retains full control). Strongly types inputs to route to specialized downstream agents4. |
| **Google ADK** | AgentTool / ParallelAgent | Iterative invocation within graph-based execution. | Subagent exposed transparently as a standard function tool. | Synchronous text or JSON return payload. | Wraps an entire specialized agent (LLM, prompt, and tools) as a single callable function. Control strictly returns to the parent LLM, avoiding subagent-to-user conversational drift7. |
| **LangGraph** | Send API | Batch (Map-Reduce fan-out). | Arbitrary state object pushed to target nodes. | Aggregated state object returned to the parent graph. | Implements true graph-based fan-out and fan-in execution. Designed for deterministic parallelization defined by the developer, rather than autonomous, LLM-driven parallelization12. |
| **CrewAI** | Delegate work to coworker | Iterative, but frequently defaults to sequential execution. | Accepts task (str), context (str), and coworker (str). | Natural language string response from the delegated coworker. | Relies heavily on anthropomorphic LLM semantics. Frequently suffers from infinite delegation loops or failure to recognize valid coworkers due to strict string matching and hallucinations15. |
| **LlamaIndex** | AgentWorkflow | Iterative orchestrator pattern. | Subagents exposed via run methods mapped directly to tool calls. | Shared state context updates modified asynchronously. | Favors the orchestrator pattern where subagents act as standard tools, allowing the top-level parent agent to decide the sequence and parallelization dynamically based on the event stream18. |
| **Moonshot Kimi K2.5/K2.6** | *Agent Swarm* | Model-native parallel orchestration. | Dynamic task decomposition mapped autonomously to temporary subagents. | Synthesized shared state presented as a final unified output. | Inverts the traditional paradigm: the model intrinsically decomposes a prompt into parallel research tracks and spawns up to 300 subagents autonomously, shifting orchestration from the framework to the model itself21. |

The survey reveals a clear dichotomy between frameworks that attempt to enforce human-like hierarchical management (such as CrewAI and AutoGen) and frameworks that treat subagents as stateless, functional subroutines (such as Google ADK and Claude Code). For a fire-and-collect architecture operating across multiple model families, treating subagents strictly as functional subroutines yields significantly higher reliability. Conversational handoffs require the model to understand complex role-playing dynamics, which open-weight models often struggle to maintain over long horizons.

## **Core Decision 1: Invocation Pattern (Batch vs. Iterative)**

The most consequential architectural decision in designing a delegation interface is determining how the model should trigger multiple subagents simultaneously. The system can either expose a tool that accepts a batch of subtasks in a single array payload (the batch pattern), or it can expose a tool that accepts a single subtask, relying on the model to emit multiple tool calls simultaneously in a single response turn (the iterative, parallel tool-calling pattern).

### **The Fallacy of Batch Invocation**

A batch invocation pattern requires an argument schema structured as an array of objects. For example, a single tool named delegate\_batch might require an argument such as subtasks: \[{description: "...", role: "..."}, ...\]. While this approach superficially appears token-efficient by minimizing tool-call envelope overhead, it introduces severe cross-model reliability failures.  
The primary vulnerability of the batch pattern is JSON formatting degradation. Weaker models, deeply quantized local models, and even frontier models under high context pressure struggle significantly with deeply nested JSON structures. Research into tool-calling reliability indicates that forcing language models to generate lists of complex dictionaries exponentially increases the likelihood of syntax errors, improper character escaping, and schema violations25. When an LLM generates a long array of objects, the token prediction probabilities for structural characters (like brackets and commas) degrade as the sequence length increases. If a model forgets a single closing brace in a batch of five subtasks, the entire JSON payload fails to parse, destroying the entire orchestration turn and requiring an expensive retry27.  
Furthermore, passing a large array of complex tasks can easily exceed a model's maximum output token limit for a single structured block. Frameworks interacting with Qwen models frequently report that tool calls containing highly verbose arrays fail or truncate silently, leading to catastrophic workflow interruptions28. Finally, batch invocation complicates error recovery. If one out of five subtasks in a batch is semantically malformed or targets an invalid role, the orchestrating infrastructure must either reject the entire batch—punishing the model for a partial success—or implement complex, custom partial-success logic that returns a fragmented result string, which confuses the parent model on the subsequent turn.

### **The Superiority of Iterative Parallel Tool Calls**

The recommended approach is to design a tool that accepts only a single subtask per invocation, relying entirely on the parent model's native parallel\_tool\_calls capability to achieve fan-out. If the parent agent determines that five subagents are required, it simply emits five separate, flat tool calls to the delegate\_subtask tool within a single generation phase.  
The industry has rapidly converged on native parallel tool calling as the standard for multi-action generation. OpenAI GPT-5.5, Anthropic Claude Opus, Google Gemini, Zhipu GLM-5.2, and DeepSeek V4 all natively support emitting multiple distinct tool calls in a single turn29. DeepSeek V4 explicitly advertises support for up to 128 parallel tool calls natively, highlighting the efficiency of this approach for massive fan-out operations31.  
By migrating the array structure out of the JSON schema and into the inference engine's standard tool-call envelope, the argument schema remains entirely flat. Flat JSON schemas achieve near-perfect format validity across all models, including open-weight models like Qwen and MiniMax, because they eliminate the cognitive overhead of tracking nested bracket depths during token generation32.  
Iterative parallel calls also isolate failure domains perfectly. If the parent model emits five parallel tool calls and one call hallucinates an invalid parameter, the infrastructure can cleanly reject the single invalid call with a targeted error message, while allowing the other four subagents to execute normally. The parent model receives four successful results and one error message, and can easily be prompted to correct and re-issue the single failed task in the next turn34. Additionally, this pattern simplifies traceability and attribution. Emitting separate tool calls assigns a unique tool\_call\_id to each subtask at the inference level. When the subagents complete their work, the infrastructure simply maps each result to its specific tool\_call\_id. This aligns perfectly with the standard OpenAI and Anthropic message formats, preventing attribution drift and ensuring the model knows exactly which result corresponds to which request36.

### **Mitigating Cross-Model Quirks in Parallel Calls**

While iterative parallel tool calling is the superior architectural design, specific model quirks must be accommodated at the infrastructure router level to ensure seamless compatibility.  
The DeepSeek family (including V3, V4, and R1 variants) enforces strict role alternation. If a model generates parallel tool calls, the API router must append them within a single assistant message containing an array of tool\_calls. Storing them as consecutive assistant messages, each with a single tool call, violently breaks DeepSeek's API constraints, resulting in immediate HTTP 400 errors38. Furthermore, older open-source parsers utilized by local models sometimes employ greedy regular expressions that inadvertently drop all but the final tool call when a model emits multiples39. The orchestration infrastructure must ensure its parsing layer is strictly compliant with non-greedy multi-tool extraction.

## **Core Decision 2: Naming, Framing, and Semantic Steering**

The semantic naming of a tool dramatically influences how reliably a language model selects it and how it behaves during execution. Language models rely heavily on their attention mechanisms to map abstract user intent to the literal string tokens of tool names and descriptions. A poorly named tool creates semantic friction, requiring excessive system prompt engineering to overcome.

### **Analysis of Existing Naming Conventions**

Frameworks that rely on anthropomorphic role-play, such as CrewAI's Delegate work to coworker tool, often inadvertently trigger conversational filler. When a model uses this tool, it tends to generate task descriptions like "Hello coworker, could you please look into this file and tell me what you think?" rather than strict, programmatic task definitions16. This conversational drift degrades the performance of the receiving subagent, which benefits from concise, direct instructions.  
Anthropic's Claude Code utilizes the name Agent (having transitioned from Task). While mathematically clean, the term Agent is heavily overloaded in the context of modern LLM prompts. If a parent model's system prompt instructs it to "act as a highly capable coding agent," the semantic overlap with an Agent tool can cause the model to confuse its own identity with the tool, leading to unexpected invocations or recursive hallucinations1. Similarly, the spawn\_agent and wait\_agent paradigm utilized by Codex requires the model to manage state asynchronously across multiple conversation turns. The model must track which agent IDs are currently running in the background and explicitly decide when to yield execution by calling a wait function34. For a fire-and-collect architecture where subagents are purely disposable subroutines, this introduces unnecessary token drift and cognitive load over long context horizons.

### **The Recommended Designation: delegate\_subtask**

The delegation tool should be universally named delegate\_subtask. This specific combination of verb and noun is highly optimized for cross-model comprehension.  
The verb "delegate" strongly signals to the model that it is handing off computational responsibility to an external entity. This immediately triggers appropriate decomposition behaviors, discouraging the parent model from attempting to solve the problem directly within the tool arguments. The noun "subtask" explicitly grounds the action in a broader, parent-child workflow. It signals that the requested action is merely a piece of a larger puzzle, naturally encouraging the parent model to maintain high-level orchestration control rather than abdicating total responsibility to the tool.  
Beyond the name, the tool's framing—specifically its description field within the JSON schema—must explicitly permit and encourage parallelization. The description must aggressively neutralize the "serial collapse" problem, where models stubbornly execute one task per turn despite having parallel capabilities22. The description should state: *"Executes a highly focused subtask in an isolated, parallel agent environment. To perform multiple independent tasks simultaneously, invoke this tool multiple times in the same response."*

## **Core Decision 3: Argument Schema Architecture**

The argument schema must balance the need for precise subagent steering against the formatting limitations of the weakest supported model in the fleet. Deep nesting must be entirely avoided, as it exponentially increases the likelihood of JSON parsing errors and hallucinated keys25.

### **The Minimal Ergonomic Set**

A robust cross-model schema requires only three flat, highly descriptive fields to function optimally.  
The most critical field is the task\_description (String, Required). This field contains the exact prompt that will initialize the subagent's ReAct loop. Because the subagent operates in a completely isolated environment without access to the parent's memory or context window, the schema description for this field must force the parent model to be exhaustive. It must instruct the parent to include all necessary context, specific file paths, and strict constraints1. If the parent simply writes "analyze the database," the subagent will fail due to lack of context.  
The second field is the specialist\_role (String, Optional). This field acts as an enum or string dictionary that dictates the system persona of the subagent (e.g., researcher, code\_analyst, data\_extractor). Allowing the parent model to define the role improves the subagent's downstream performance by activating relevant latent knowledge and shaping its internal reasoning vectors18. It also allows the backend infrastructure to route the request to a specific, smaller model fine-tuned for that exact role, saving compute costs.  
The third field is the expected\_return\_format (String, Optional). This is a natural language description of how the subagent should structure its final output string back to the parent. The parent might request a Markdown table, a comma-separated list, or a brief executive summary.

### **Exclusions and Anti-Patterns**

It is vital to exclude explicit JSON return schemas from the subagent's input arguments. Forcing the parent LLM to write a nested JSON schema as a stringified argument introduces severe escaping vulnerabilities and formatting errors. If structured output is required from the subagent, the parent should request it in natural language via the expected\_return\_format field, allowing the subagent to output fenced JSON or structured Markdown natively27.  
Furthermore, do not include model overrides, iteration caps, or timeout parameters in the model-facing schema. Parameters such as max\_turns, temperature, or specific model weights should be handled entirely by the backend infrastructure based on the provided specialist\_role or the user's billing tier. Exposing these technical parameters to the parent LLM creates unnecessary cognitive load and encourages hallucinations, such as the parent arbitrarily requesting non-existent models (e.g., gpt-7-turbo) or infinite timeout values.

## **Core Decision 4: Result Formatting and Parent Comprehension**

Because the architecture demands a fire-and-collect pattern, the parent model must receive the subagent outputs in a manner that maximizes comprehension and minimizes context pollution. When five to ten parallel subagents return their results simultaneously, the parent model's context window is flooded with disparate information.

### **Result Packaging**

The output of a delegate\_subtask tool call should be returned to the parent as a verbatim string of the subagent's final output, rather than a complex structured object. Returning deeply nested JSON objects from the tool result forces the parent model to parse layers of quotes and brackets, which degrades its attention on the actual semantic content of the results25.  
When the orchestration infrastructure resolves the parallel tool calls, it must inject the results back into the parent's context as standard tool\_response messages, mapping each response directly to its originating tool\_call\_id36.  
To aid the parent model in synthesizing these parallel results, the infrastructure should prepend a system-injected metadata header to the text content of the tool response. An optimal format is:

## **\[Subtask Completed\] Task: {Original task\_description} Role: {specialist\_role}**

{Subagent's final text output}  
This explicit re-statement of the original task grounds the parent model. It immediately reminds the parent *why* this specific block of text is entering the context window, neutralizing the confusion that frequently arises when massive amounts of parallel results hit the model simultaneously10.

## **Core Decision 5: Limits, Conventions, and Error Handling**

To maintain stability across varying model contexts and inference budgets, hard limits must be established at the infrastructure level and communicated directly to the model via system prompts and runtime errors.

### **Concurrency and Nesting**

The system prompt must explicitly cap parallelization to prevent runaway generation. Research indicates that beyond five to seven concurrent subagents, the parent model begins to struggle with synthesizing the resulting influx of data, leading to severe "lost in the middle" phenomena where critical details from the middle subagents are ignored2. The system prompt must state explicitly: *"Do not spawn more than 5 parallel subtasks per turn."*  
Similarly, nesting depth must be strictly capped. Subagents must operate at Depth 1; they should not be permitted to call delegate\_subtask themselves. In systems like Claude Code, deeply nested subagents are restricted from spawning further agents to prevent exponential token exhaustion and runaway recursive loops1. The backend orchestration must intercept any nested calls and return a hard error string to the subagent: *"Error: Subagent nesting limit reached. You must complete this task directly."*

### **Robust Error Feedback**

If a subagent fails, encounters a timeout, or crashes during its ReAct loop, the tool result must not be a silent failure or an empty string. The infrastructure must return a clear, localized error string directly to the parent model. For example: *"Error: Subagent timed out after 120 seconds. Proceed with available information or attempt a different strategy."* Models like Zhipu GLM-5.2 and OpenAI GPT-5.5 are highly capable of reading error strings, understanding the failure mode, and pivoting their execution plans accordingly33. Supplying a descriptive error string prevents the parent model from hallucinating that the task was completed successfully.

## **Core Decision 6: Steering and Prompting Strategies**

A persistent challenge in multi-agent orchestration is "serial collapse"—the tendency of a language model to execute tasks sequentially, one turn at a time, even when parallel tools are available and clearly defined in the schema22. Conversely, models sometimes suffer from over-delegation, spawning expensive subagents for trivial string-matching tasks that could easily be answered natively, thereby wasting setup overhead and compute tokens2.

### **Combating Serial Collapse**

To force parallelization, the instructions provided to the parent model must be highly explicit. The system prompt cannot rely on the word "parallel" alone; it must dictate the precise mechanical mechanism of parallelization required by the API. The prompt should dictate: *"When analyzing multiple files, researching distinct entities, or exploring orthogonal concepts, you MUST delegate the work. To execute in parallel, you must invoke the delegate\_subtask tool multiple times within a single response turn. Do not wait for one subtask to finish before delegating the next."*34. Models that are specifically optimized for agentic workflows, such as Kimi K2.5's Agent Swarm and Zhipu GLM-5.2, natively recognize this semantic pattern and will fan out aggressively, dramatically reducing end-to-end latency22.

### **Combating Over-Delegation**

Subagents carry an inherent initialization cost, including context setup, system prompt processing, and time-to-first-token (TTFT) latency. Spawning a subagent for a simple regular expression search or basic arithmetic is a severe anti-pattern2. The system prompt must establish clear boundaries for delegation: *"Do not delegate trivial tasks such as simple formatting, basic calculations, or answering questions based on context you already possess. Only use delegate\_subtask for tasks requiring deep, isolated research, multi-step tool execution, or exploration of unseen files."*

## **Cross-Model Quirks and Compatibility Assessment**

An interface designed exclusively for OpenAI will inevitably fail on Chinese open-weights or specialized models due to distinct inference behaviors, tokenizer differences, and parser implementations. To ensure true cross-model compatibility, the architecture must account for the specific quirks of the target fleet.

| Model Family | Documented Quirk / Parsing Behavior | Required Infrastructure Mitigation |
| :---- | :---- | :---- |
| **MiniMax M3** | Emits native XML (\<minimax:tool\_call\>) under the hood, which is translated to JSON via regex parsers (e.g., vLLM/SGLang)32. | Complex, nested JSON schemas cause the regex parser to drop parameters. Keeping the schema strictly flat guarantees the XML-to-JSON bridge operates flawlessly. |
| **Zhipu GLM-5.2** | Utilizes advanced "thinking modes" between tool calls to plan parallel fan-outs49. | Highly reliable with flat schemas. However, it requires explicit authorization in the system prompt to use parallel tools, otherwise it defaults to sequential reasoning46. |
| **DeepSeek (V3/V4)** | Enforces strict role alternation at the API level. Supports 128 parallel calls31. | The orchestration backend *must* merge parallel tool calls into a single assistant message array. Storing them consecutively triggers HTTP 400 errors38. Furthermore, older open-source parsers must be patched for non-greedy regex to prevent dropping multiple calls39. |
| **Google Gemini** | Exceptionally strict regarding schema adherence. Prone to entering infinite retry loops if tool calls return unexpected errors20. | Error strings returned from the subagent must explicitly instruct Gemini to "stop retrying and synthesize," preventing token exhaustion on failed subagent configurations. |
| **Qwen (3.6/3.7)** | Known to truncate tool arguments if the generated JSON payload exceeds standard generation limits28. | Prevent the parent from passing large text blocks or raw file contents into the task\_description. Force the subagent to fetch necessary data itself to keep the argument footprint minimal. |

## **Proposed Tool Definition and Implementation**

To achieve maximum reliability across GPT-5.5, Claude Opus, Gemini, MiniMax M3, GLM-5.2, DeepSeek V4, and Qwen, the system must utilize an **iterative parallel invocation pattern** mapped to a **flat argument schema** named delegate\_subtask.

### **The JSON Schema Definition**

JSON  
{  
  "type": "function",  
  "function": {  
    "name": "delegate\_subtask",  
    "description": "Delegates a focused subtask to an isolated, ephemeral subagent. The subagent has no access to your memory, so you must provide complete instructions and context. To run multiple tasks in parallel, call this tool multiple times in the same response.",  
    "parameters": {  
      "type": "object",  
      "properties": {  
        "task\_description": {  
          "type": "string",  
          "description": "Comprehensive instructions for the subagent. Include all necessary context, file paths, or specific constraints required to complete the task."  
        },  
        "specialist\_role": {  
          "type": "string",  
          "description": "The persona the subagent should adopt to optimize its execution.",  
          "enum": \["researcher", "code\_analyst", "data\_extractor", "general\_assistant"\]  
        },  
        "expected\_return\_format": {  
          "type": "string",  
          "description": "Instructions on how the subagent should format its final text response back to you (e.g., 'A bulleted list of findings', 'A brief summary')."  
        }  
      },  
      "required": \["task\_description", "specialist\_role", "expected\_return\_format"\],  
      "additionalProperties": false  
    }  
  }  
}

### **Standard System Prompt Injection**

To guarantee correct utilization across the model fleet and neutralize serial collapse, inject the following strict directive into the parent agent's system prompt:  
**Subagent Delegation Policy:**  
You have access to the delegate\_subtask tool to offload complex, isolated work.

1. **Parallelization:** When a task requires exploring multiple files, researching distinct topics, or analyzing multiple components, you MUST execute these in parallel. Do this by emitting multiple independent delegate\_subtask tool calls in a single response. Do not wait for one to finish before starting another.  
2. **Concurrency Limit:** Do not spawn more than 5 subagents in a single turn.  
3. **Context Independence:** Subagents start with a blank context window. You must include all necessary reference material, parameters, and specific file paths directly in the task\_description.

### **Example Invocation (Parallel Fan-out of 5\)**

When a user prompts the parent model with: *"Analyze the authentication, database, routing, utility, and middleware layers of this codebase and summarize the security patterns."*  
The parent model generates a single assistant message containing an array of five flat tool calls, invoking maximum concurrency. (Note: The JSON below represents the internal API payload generated by the model's inference engine, successfully avoiding list-of-dicts parsing vulnerabilities).

JSON  
{  
  "role": "assistant",  
  "content": null,  
  "tool\_calls": \[  
    {  
      "id": "call\_abc1",  
      "type": "function",  
      "function": {  
        "name": "delegate\_subtask",  
        "arguments": "{\\"task\_description\\": \\"Analyze the /auth directory. Identify authentication patterns, token handling, and potential security vulnerabilities.\\", \\"specialist\_role\\": \\"code\_analyst\\", \\"expected\_return\_format\\": \\"A bulleted list of security findings\\"}"  
      }  
    },  
    {  
      "id": "call\_abc2",  
      "type": "function",  
      "function": {  
        "name": "delegate\_subtask",  
        "arguments": "{\\"task\_description\\": \\"Analyze the /db directory. Identify ORM usage, query sanitization, and injection risks.\\", \\"specialist\_role\\": \\"code\_analyst\\", \\"expected\_return\_format\\": \\"A bulleted list of security findings\\"}"  
      }  
    },  
    {  
      "id": "call\_abc3",  
      "type": "function",  
      "function": {  
        "name": "delegate\_subtask",  
        "arguments": "{\\"task\_description\\": \\"Analyze the /routes directory. Check for proper authorization middleware application.\\", \\"specialist\_role\\": \\"code\_analyst\\", \\"expected\_return\_format\\": \\"A bulleted list of security findings\\"}"  
      }  
    },  
    {  
      "id": "call\_abc4",  
      "type": "function",  
      "function": {  
        "name": "delegate\_subtask",  
        "arguments": "{\\"task\_description\\": \\"Analyze the /utils directory. Look for unsafe cryptographic functions or exposed secrets.\\", \\"specialist\_role\\": \\"code\_analyst\\", \\"expected\_return\_format\\": \\"A bulleted list of security findings\\"}"  
      }  
    },  
    {  
      "id": "call\_abc5",  
      "type": "function",  
      "function": {  
        "name": "delegate\_subtask",  
        "arguments": "{\\"task\_description\\": \\"Analyze the /middleware directory. Review CORS settings, rate limiting, and header injection protections.\\", \\"specialist\_role\\": \\"code\_analyst\\", \\"expected\_return\_format\\": \\"A bulleted list of security findings\\"}"  
      }  
    }  
  \]  
}

This architecture deliberately leverages the iterative pattern to ensure absolute stability on open-weights and deeply quantized models. By maintaining the arguments as highly compressed, flat scalar strings rather than deeply nested JSON objects, the design preempts the parsing limits of MiniMax M3's XML translation layer44 and bypasses Qwen's truncation flaws28. To satisfy DeepSeek V4's strict alternation requirements38, the orchestrating backend must append all five tool\_response messages consecutively before prompting the model for its final synthesis turn. Under this exact design paradigm, virtually any frontier model can reliably act as a scalable, highly concurrent orchestration engine without requiring provider-specific hardcoding.

#### **Works cited**

1. Subagents in the SDK \- Claude Code Docs, [https://code.claude.com/docs/en/agent-sdk/subagents](https://code.claude.com/docs/en/agent-sdk/subagents)  
2. Claude Code Sub-Agents: 3x Output with Parallel Tasks \- AI Builder Club, [https://www.aibuilderclub.com/blog/claude-code-sub-agents-guide](https://www.aibuilderclub.com/blog/claude-code-sub-agents-guide)  
3. Claude Code agents: understand subagents and autonomous workflows, [https://claude-codex.fr/en/agents/what-are-agents/](https://claude-codex.fr/en/agents/what-are-agents/)  
4. Handoffs \- OpenAI Agents SDK, [https://openai.github.io/openai-agents-python/handoffs/](https://openai.github.io/openai-agents-python/handoffs/)  
5. Observability for OpenAI Agents SDK \- Laminar documentation, [https://laminar.sh/docs/tracing/integrations/openai-agents-sdk](https://laminar.sh/docs/tracing/integrations/openai-agents-sdk)  
6. Agent orchestration \- OpenAI Agents SDK, [https://openai.github.io/openai-agents-python/multi\_agent/](https://openai.github.io/openai-agents-python/multi_agent/)  
7. Parallel workflow \- Agent Development Kit (ADK), [https://adk.dev/agents/workflow-agents/parallel-agents/](https://adk.dev/agents/workflow-agents/parallel-agents/)  
8. One Small Change That Makes OpenAI Work Perfectly in ADK Parallel Pipelines | by Ashmi Banerjee | Google Developer Experts | Medium, [https://medium.com/google-developer-experts/one-small-change-that-makes-openai-work-perfectly-in-adk-parallel-pipelines-c37cd2858396](https://medium.com/google-developer-experts/one-small-change-that-makes-openai-work-perfectly-in-adk-parallel-pipelines-c37cd2858396)  
9. Building AI Agents with ADK: Empowering with Tools \- Codelabs, [https://codelabs.developers.google.com/devsite/codelabs/build-agents-with-adk-empowering-with-tools](https://codelabs.developers.google.com/devsite/codelabs/build-agents-with-adk-empowering-with-tools)  
10. Google ADK \- OpenAPI tools, agents-as-tools, authentication, and long-running operations, [https://ravichaganti.com/blog/google-adk-openapi-tools-agents-as-tools-authentication-and-long-running-operations/](https://ravichaganti.com/blog/google-adk-openapi-tools-agents-as-tools-authentication-and-long-running-operations/)  
11. Why Google ADK's AgentTool Eliminates a Common Multi-Agent Development Friction | by sarojkumar rout | Medium, [https://medium.com/@sarojkumar.rout/why-google-adks-agenttool-eliminates-a-common-multi-agent-development-friction-b0cc6e5e6099](https://medium.com/@sarojkumar.rout/why-google-adks-agenttool-eliminates-a-common-multi-agent-development-friction-b0cc6e5e6099)  
12. Send | langgraph \- LangChain Reference, [https://reference.langchain.com/python/langgraph/types/Send](https://reference.langchain.com/python/langgraph/types/Send)  
13. Map-Reduce with the Send() API in LangGraph | by Damilola Oyedunmade \- Medium, [https://medium.com/ai-engineering-bootcamp/map-reduce-with-the-send-api-in-langgraph-29b92078b47d](https://medium.com/ai-engineering-bootcamp/map-reduce-with-the-send-api-in-langgraph-29b92078b47d)  
14. Use the graph API \- Docs by LangChain, [https://docs.langchain.com/oss/python/langgraph/use-graph-api](https://docs.langchain.com/oss/python/langgraph/use-graph-api)  
15. Collaboration \- CrewAI Documentation, [https://docs.crewai.com/v1.15.1/en/concepts/collaboration](https://docs.crewai.com/v1.15.1/en/concepts/collaboration)  
16. The issue is that the manager agent isn't properly delegating to the crew member agents (like company\_Scheduling\_Agent or Retriever\_Agent). Instead, it's attempting to use a coworker designation that only recognizes the manager itself. This means that whe \- CrewAI Community Support, [https://community.crewai.com/t/the-issue-is-that-the-manager-agent-isn-t-properly-delegating-to-the-crew-member-agents-like-company-scheduling-agent-or-retriever-agent-instead-it-s-attempting-to-use-a-coworker-designation-that-only-recognizes-the-manager-itself-this-means-that-whe/4966](https://community.crewai.com/t/the-issue-is-that-the-manager-agent-isn-t-properly-delegating-to-the-crew-member-agents-like-company-scheduling-agent-or-retriever-agent-instead-it-s-attempting-to-use-a-coworker-designation-that-only-recognizes-the-manager-itself-this-means-that-whe/4966)  
17. Why CrewAI's Manager-Worker Architecture Fails — and How to Fix It, [https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/](https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/)  
18. Multi-agent patterns in LlamaIndex | Developer Documentation \- LlamaParse, [https://developers.llamaindex.ai/python/framework/understanding/agent/multi\_agent/](https://developers.llamaindex.ai/python/framework/understanding/agent/multi_agent/)  
19. AgentWorkflow Guide: Build AI Agent Systems | LlamaIndex, [https://www.llamaindex.ai/blog/introducing-agentworkflow-a-powerful-system-for-building-ai-agent-systems](https://www.llamaindex.ai/blog/introducing-agentworkflow-a-powerful-system-for-building-ai-agent-systems)  
20. Diving into LlamaIndex AgentWorkflow: A Nearly Perfect Multi-Agent Orchestration Solution, [https://www.dataleadsfuture.com/diving-into-llamaindex-agentworkflow-a-nearly-perfect-multi-agent-orchestration-solution/](https://www.dataleadsfuture.com/diving-into-llamaindex-agentworkflow-a-nearly-perfect-multi-agent-orchestration-solution/)  
21. \[2602.02276\] Kimi K2.5: Visual Agentic Intelligence \- arXiv, [https://arxiv.org/abs/2602.02276](https://arxiv.org/abs/2602.02276)  
22. Kimi K2.5 and Agent Swarm: A Guide With Four Practical Examples | DataCamp, [https://www.datacamp.com/tutorial/kimi-k2-agent-swarm-guide](https://www.datacamp.com/tutorial/kimi-k2-agent-swarm-guide)  
23. Kimi K2.6 Agent Swarm: 300 Sub-Agents and 4,000 Steps Explained \- Verdent Guides, [https://www.verdent.ai/guides/kimi-k2-6-agent-swarm](https://www.verdent.ai/guides/kimi-k2-6-agent-swarm)  
24. Kimi Agent Swarm: 100 Sub-Agents at Scale, [https://www.kimi.com/blog/agent-swarm](https://www.kimi.com/blog/agent-swarm)  
25. Gecko: A Simulation Environment with Stateful Feedback for Refining Agent Tool Calls, [https://arxiv.org/html/2602.19218v1](https://arxiv.org/html/2602.19218v1)  
26. Most capable function calling open source models? : r/LocalLLaMA \- Reddit, [https://www.reddit.com/r/LocalLLaMA/comments/1ackxxt/most\_capable\_function\_calling\_open\_source\_models/](https://www.reddit.com/r/LocalLLaMA/comments/1ackxxt/most_capable_function_calling_open_source_models/)  
27. Enforcing JSON Outputs in Commercial LLMs | by Daniel Kharitonov | TDS Archive | Medium, [https://medium.com/data-science/enforcing-json-outputs-in-commercial-llms-3db590b9b3c8](https://medium.com/data-science/enforcing-json-outputs-in-commercial-llms-3db590b9b3c8)  
28. Qwen 3/3.5/3.6 tool calling is broken (even worse with 3.6). : r/Vllm \- Reddit, [https://www.reddit.com/r/Vllm/comments/1suasv2/qwen\_33536\_tool\_calling\_is\_broken\_even\_worse\_with/](https://www.reddit.com/r/Vllm/comments/1suasv2/qwen_33536_tool_calling_is_broken_even_worse_with/)  
29. Glm 4.5 \- AI/ML API Documentation, [https://docs.aimlapi.com/api-references/text-models-llm/zhipu/glm-4.5](https://docs.aimlapi.com/api-references/text-models-llm/zhipu/glm-4.5)  
30. glm-5.2 (Zhipu AI) \- Cloudflare Docs, [https://developers.cloudflare.com/ai/models/%40cf/zai-org/glm-5.2/](https://developers.cloudflare.com/ai/models/%40cf/zai-org/glm-5.2/)  
31. DeepSeek V4 AI Agents: Function Calling, MCP & Agentic Guide \- LushBinary, [https://lushbinary.com/blog/deepseek-v4-ai-agents-function-calling-mcp-guide/](https://lushbinary.com/blog/deepseek-v4-ai-agents-function-calling-mcp-guide/)  
32. docs/function\_call\_guide.md · MiniMaxAI/MiniMax-M1-40k at 82ba8ac91c9d5731f8400ab387ed59ee6440d33b \- Hugging Face, [https://huggingface.co/MiniMaxAI/MiniMax-M1-40k/blame/82ba8ac91c9d5731f8400ab387ed59ee6440d33b/docs/function\_call\_guide.md](https://huggingface.co/MiniMaxAI/MiniMax-M1-40k/blame/82ba8ac91c9d5731f8400ab387ed59ee6440d33b/docs/function_call_guide.md)  
33. How to use function calling | Scaleway Documentation, [https://www.scaleway.com/en/docs/generative-apis/how-to/use-function-calling/](https://www.scaleway.com/en/docs/generative-apis/how-to/use-function-calling/)  
34. Stop spawning subagents. Here are the 4 Subagent Patterns : r/ClaudeCode \- Reddit, [https://www.reddit.com/r/ClaudeCode/comments/1t5j65c/stop\_spawning\_subagents\_here\_are\_the\_4\_subagent/](https://www.reddit.com/r/ClaudeCode/comments/1t5j65c/stop_spawning_subagents_here_are_the_4_subagent/)  
35. Inside the Agent Harness: How Codex and Claude Code Actually Work | by Jonathan Fulton, [https://medium.com/jonathans-musings/inside-the-agent-harness-how-codex-and-claude-code-actually-work-63593e26c176](https://medium.com/jonathans-musings/inside-the-agent-harness-how-codex-and-claude-code-actually-work-63593e26c176)  
36. DeepSeek Tool Calls Guide \- Chat-Deep.ai, [https://chat-deep.ai/docs/deepseek-tool-calls/](https://chat-deep.ai/docs/deepseek-tool-calls/)  
37. How to use function calling to invoke tools \- Alibaba Cloud Model Studio, [https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling)  
38. Parallel tool calls stored as separate assistant messages break DeepSeek session replay · Issue \#29148 · NousResearch/hermes-agent \- GitHub, [https://github.com/NousResearch/hermes-agent/issues/29148](https://github.com/NousResearch/hermes-agent/issues/29148)  
39. DeepSeek V3 parser drops tool calls when model returns multiple · Issue \#989 · NousResearch/hermes-agent \- GitHub, [https://github.com/NousResearch/hermes-agent/issues/989](https://github.com/NousResearch/hermes-agent/issues/989)  
40. Tools reference \- Claude Code Docs, [https://code.claude.com/docs/en/tools-reference](https://code.claude.com/docs/en/tools-reference)  
41. Claude Code Task Management: Distribute Work Across Agents, [https://claudefa.st/blog/guide/agents/task-distribution](https://claudefa.st/blog/guide/agents/task-distribution)  
42. How to Use Sub-Agents in Claude Code to Manage Context and Speed Up Research, [https://www.mindstudio.ai/blog/sub-agents-claude-code-context-management](https://www.mindstudio.ai/blog/sub-agents-claude-code-context-management)  
43. pi-agents-pool · Packages, [https://pi.dev/packages/pi-agents-pool?page=35](https://pi.dev/packages/pi-agents-pool?page=35)  
44. The Ultimate Guide to Agentic Tool Calling: From Native Tool Calling to Best Custom Approach \- Blog, [https://blog.sylph.ai/posts/ultimate-guide-agentic-tool-calling](https://blog.sylph.ai/posts/ultimate-guide-agentic-tool-calling)  
45. Claude Code subagents: the 2026 production playbook \- Totalum Blog, [https://www.totalum.app/blog/claude-code-subagents-totalum](https://www.totalum.app/blog/claude-code-subagents-totalum)  
46. GLM-5: from Vibe Coding to Agentic Engineering \- arXiv, [https://arxiv.org/html/2602.15763v1](https://arxiv.org/html/2602.15763v1)  
47. How to Test Your MCP Server with Z.AI GLM Models (2026 Guide), [https://mcpplaygroundonline.com/blog/test-mcp-server-with-glm-models](https://mcpplaygroundonline.com/blog/test-mcp-server-with-glm-models)  
48. Chasing 100% Accuracy: A Deep Dive into Debugging Kimi K2's Tool-Calling on vLLM, [https://vllm.ai/blog/2025-10-28-kimi-k2-accuracy](https://vllm.ai/blog/2025-10-28-kimi-k2-accuracy)  
49. GLM-5.2 quickstart \- Together AI docs, [https://docs.together.ai/docs/glm-5.2-quickstart](https://docs.together.ai/docs/glm-5.2-quickstart)  
50. GLM-5.2: Zhipu AI's 1M-Token Open-Weight Coding Model \- Eigent AI, [https://www.eigent.ai/blog/glm-5-2](https://www.eigent.ai/blog/glm-5-2)