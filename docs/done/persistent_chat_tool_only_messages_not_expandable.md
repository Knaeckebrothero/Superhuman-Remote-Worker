# Persistent chat — tool-only assistant messages aren't expandable after streaming finishes

## Symptom (observed 2026-05-11)

User uploaded a PDF (no text), agent ran a series of tool calls. While the
turn was streaming, each tool call's args and result were inspectable by
expanding the per-tool-card `<details>`. After the message finalized, the
same calls collapsed to a single non-expandable line:

> 🔧  Inspecting document  ●
> 📄  Exploring files uploads/  ●
> 📄  Reading Vertraulichkeitsvereinbarung.pdf  ●

Clicking these does nothing. The tool args, result text, and approval
decision are no longer reachable from the UI.

This matters in practice — when the agent fails or behaves oddly, the
post-mortem trail is gone. In the test session, the first `get_document_info`
returned "file not found" and only `list_files` then `read_file` worked,
but with no way to expand the failed call there's no UI evidence of the
error.

## Root cause

`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:374-382`
takes a special-cased branch for finalized assistant messages that have
**no content and at least one tool call**:

```ts
} @else if (msg.role === 'assistant' && !msg.content && msg.toolCalls?.length) {
  <!-- Tool-only message: compact inline indicator -->
  <div class="tool-only-row">
    <app-icon size="sm" class="tool-only-icon">{{ toolIcon(msg.toolCalls![0].tool) }}</app-icon>
    <span class="tool-only-label">{{ toolSummaryLabel(msg.toolCalls!) }}</span>
    <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
  </div>
}
```

This is a flat row with no `<details>` wrapper — by design a "compact
inline indicator", but it loses access to the `tool-detail-list` markup
that the regular path (line ~449) renders. The streaming view at line
~589 has the full expandable `<details class="tool-summary">` tree, which
is why expansion works mid-turn and breaks the moment `finalizeStreaming()`
moves the call into the messages array.

The data itself is preserved correctly. `PersistentChatService.finalizeStreaming()`
(`persistent-chat.service.ts:937`) copies `currentToolCalls()` verbatim
into the message — args, results, decisions all survive. It's purely a
rendering omission.

## Impact

- Users can't inspect tool args/results after a turn ends.
- Failed tool calls are invisible (the dot only encodes status — error
  text and call args are hidden).
- Reduces debugging value of the chat transcript significantly. The
  natural workflow — "scroll back, expand the failed tool, see what
  arguments produced the error" — doesn't work.
- Particularly harmful for tool-only turns (e.g. agent makes 3 tool
  calls before producing any prose), which is the common pattern when
  the agent works through a problem.

## Fix sketch

Replace the flat `tool-only-row` branch (line 374-382) with the same
expandable structure used at line 449 — wrap in `<details class="tool-summary">`
with the `tool-detail-list` body. The compact look can be preserved by
making the `<summary>` styled to match `tool-only-row` and only showing
the chevron on hover, so unexpanded state still looks tight.

Roughly:

```ts
} @else if (msg.role === 'assistant' && !msg.content && msg.toolCalls?.length) {
  <details class="tool-summary tool-only-summary"
           [attr.open]="hasDeniedTools(msg.toolCalls!) || chat.narrationMode() === 'verbose' ? '' : null">
    <summary class="tool-only-row">
      <app-icon ...>{{ toolIcon(msg.toolCalls![0].tool) }}</app-icon>
      <span class="tool-only-label">{{ toolSummaryLabel(msg.toolCalls!) }}</span>
      <span class="tool-summary-dot" [class]="toolSummaryStatus(msg.toolCalls!)"></span>
    </summary>
    <div class="tool-detail-list">
      @for (tc of msg.toolCalls; track tc.id) {
        <details class="tool-card" ...> ...same as line 461-485... </details>
      }
    </div>
  </details>
}
```

The detail-list rendering is duplicated three places already (streaming
~607, finalized-with-content ~460, this proposed branch). A small shared
template (`<ng-template #toolDetailList let-tools>`) would dedupe them
and prevent future drift, but isn't required to fix the bug.

## Related code

- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:374-382` — the broken branch
- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:449-485` — the working expandable rendering for messages with content
- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:589-633` — the working streaming-view rendering
- `cockpit/src/app/core/services/persistent-chat.service.ts:937-958` — `finalizeStreaming()` (data preserved correctly)

## Decision pending

Not fixed yet — filed at user request after the 2026-05-11 PDF test session.
