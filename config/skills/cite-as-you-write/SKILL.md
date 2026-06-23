---
name: cite-as-you-write
description: Use whenever you state a fact, figure, quote, or technical claim that rests on a source — how to cite it with cite_web / cite_document and carry the returned [N] marker into your answer so the reader can see and check the source.
display_name: Cite As You Write
icon: format_quote
color: "#cba6f7"
tags:
  - citations
  - research
  - writing
---

# Cite As You Write

You can look things up and call `cite_web` / `cite_document` — but a citation the
reader never sees does nothing. The common failure is **cite-and-forget**: you
search, you record four sources, and then you write an answer with no markers in
it. The citations sit in the database, invisible on the page. This skill closes
that gap.

The rule: **every claim that rests on a source carries that source's marker, in
the text, where the claim is made.** Creating the citation and placing its marker
are one action, not two.

## The loop

For each factual, numeric, quoted, or technical claim you make from a source:

1. **Cite it.** Call `cite_web` (for a URL) or `cite_document` (for a workspace
   document), passing the claim and the supporting quote/locator. See the tool
   description for the exact arguments.
2. **Take the marker.** The tool returns a citation reference like `[12]`. That
   bracketed token is yours to use verbatim — don't renumber it, don't invent your
   own.
3. **Place it.** Put the marker at the end of the sentence (or clause) the source
   supports, before the period: `... roughly 40% of cases [12].` Write the
   sentence and attach the marker in the same breath.

If a sentence draws on two sources, give it both: `... within 24 hours [12][15].`
If you make several claims from one source, reuse its marker each time.

## When to cite

Cite when a reader could reasonably ask "says who?":

- A specific fact, statistic, date, name, or measurement.
- A direct or paraphrased quote.
- A technical claim, recommendation, or cause/effect you took from a source rather
  than derived yourself.

You don't need a marker for your own reasoning, summary, or connective prose —
only for the load-bearing claims that come from somewhere.

## In a conversation

When you're answering a user turn (not writing a file), the markers go **inline in
your reply** — the same `[N]` at the end of each supported sentence. These inline
markers are how the reader connects a specific claim to the specific source behind
it; an answer without them reads as unsourced even when you did the research.
Don't replace inline markers with a single "Sources:" list at the end — the
per-claim marker is the point.

## Don't

- **Don't cite and then write marker-free prose.** That is the failure this skill
  exists to prevent — the citation is wasted if it isn't in the text.
- **Don't fabricate or guess a marker.** Use only the `[N]` a cite tool actually
  returned this turn. If you didn't cite it, don't mark it.
- **Don't attach a marker to a claim you didn't draw from a source.** A marker
  promises "this came from that source" — keep it honest.
- **Don't renumber.** Emit the id the tool gave you; the interface handles display
  numbering.
