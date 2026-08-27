import {MarkedExtension} from 'marked';

/**
 * Marked extension that turns the agent's inline citation markers into
 * superscript reference elements the chat component resolves into a source
 * popover.
 *
 * The citation tools (`cite_web` / `cite_document`) return `[<id>]` and the
 * agent is instructed to place that marker on the claim it supports, so the
 * marker IS the citation id:
 *
 *   [21]  ->  <sup class="citation-ref" data-cid="21">21</sup>
 *
 * This stage is stateless — it only marks candidates. The chat component then
 * post-processes each `.citation-ref` against the citations it fetched for the
 * thread (keyed by `data-cid`): real ids get renumbered 1..k in order of
 * appearance, made clickable, and given a source popover; ids with no matching
 * citation are restored to their literal `[N]` text, so a stray `[5]` in normal
 * prose is never hijacked.
 *
 * A `[N]` immediately followed by `(`, `[`, or `:` is left untouched so real
 * markdown links and reference definitions (`[1](url)`, `[1][ref]`, `[1]: url`)
 * still parse normally.
 */
export function citationExtension(): MarkedExtension {
    return {
        extensions: [
            {
                name: 'citationRef',
                level: 'inline',
                start(src: string) {
                    const i = src.indexOf('[');
                    return i < 0 ? undefined : i;
                },
                tokenizer(src: string) {
                    const match = src.match(/^\[(\d+)\]/);
                    if (!match) {
                        return undefined;
                    }
                    const after = src.charAt(match[0].length);
                    if (after === '(' || after === '[' || after === ':') {
                        // Real markdown link / reference definition — not a citation.
                        return undefined;
                    }
                    return {
                        type: 'citationRef',
                        raw: match[0],
                        cid: match[1],
                    };
                },
                renderer(token: any) {
                    // The id rides a class (`cid-N`) as well as data-cid: class
                    // survives the markdown HTML sanitizer for sure, so the
                    // component can always recover the id even after it renumbers
                    // the visible text.
                    return `<sup class="citation-ref cid-${token.cid}" data-cid="${token.cid}">${token.cid}</sup>`;
                },
            },
        ],
    };
}
