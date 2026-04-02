import {MarkedExtension} from 'marked';

/**
 * Marked extension that renders agent citation markup as clickable links.
 *
 * Patterns:
 *   【cite_web(Title, URL)】  ->  <a href="URL" target="_blank">Title</a>
 *   【cite_document(Title, Path)】  ->  <span title="Path">Title</span>
 */
export function citationExtension(): MarkedExtension {
    return {
        extensions: [
            {
                name: 'citation',
                level: 'inline',
                start(src: string) {
                    return src.indexOf('\u3010');
                },
                tokenizer(src: string) {
                    const match = src.match(
                        /^\u3010cite_(web|document)\(([^,]+),\s*(.+?)\)\u3011/
                    );
                    if (match) {
                        return {
                            type: 'citation',
                            raw: match[0],
                            citationType: match[1],
                            title: match[2].trim(),
                            ref: match[3].trim(),
                        };
                    }
                    return undefined;
                },
                renderer(token: any) {
                    if (token.citationType === 'web') {
                        const safeUrl = encodeURI(token.ref);
                        return `<a class="citation citation-web" href="${safeUrl}" target="_blank" rel="noopener" title="${token.ref}">${token.title}</a>`;
                    }
                    return `<span class="citation citation-doc" title="${token.ref}">${token.title}</span>`;
                },
            },
        ],
    };
}
