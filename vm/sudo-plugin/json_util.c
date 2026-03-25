/*
 * Minimal JSON utilities for sudo_gate plugin.
 * See json_util.h for API documentation.
 */

#include "json_util.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * Escape a string for JSON output. Handles the required escapes:
 *   " → \"    \ → \\    control chars → \uXXXX
 *
 * Writes to dst (which must have room for at least dst_size bytes).
 * Returns the number of bytes written (excluding NUL), or -1 if dst is too small.
 */
static int json_escape(char *dst, size_t dst_size, const char *src)
{
    size_t i = 0;
    const char *s = src ? src : "";

    for (; *s != '\0'; s++) {
        unsigned char c = (unsigned char)*s;

        if (c == '"' || c == '\\') {
            if (i + 2 >= dst_size) return -1;
            dst[i++] = '\\';
            dst[i++] = (char)c;
        } else if (c < 0x20) {
            /* Control character — use \uXXXX. */
            if (i + 6 >= dst_size) return -1;
            int n = snprintf(dst + i, dst_size - i, "\\u%04x", c);
            if (n < 0 || (size_t)n >= dst_size - i) return -1;
            i += (size_t)n;
        } else {
            if (i + 1 >= dst_size) return -1;
            dst[i++] = (char)c;
        }
    }

    dst[i] = '\0';
    return (int)i;
}

char *json_build_request(const struct approval_request *req)
{
    /*
     * Build JSON manually. Maximum realistic sizes:
     *   command path: 4096, user/host/tty/cwd: 256 each, argv: ~8192 total
     *   JSON overhead + escaping expansion: ~2x worst case
     * Allocate generously — this runs once per sudo invocation.
     */
    size_t buf_size = 65536;
    char *buf = malloc(buf_size);
    if (!buf)
        return NULL;

    /* Escaped string scratch buffer — large enough for PATH_MAX with escaping. */
    char esc[8192];
    size_t pos = 0;
    int n;

    /*
     * Helper macro: escape a string field, bail on truncation.
     * Checks both json_escape() and snprintf() return values.
     */
#define APPEND_FIELD(prefix, value)                                          \
    do {                                                                     \
        if (json_escape(esc, sizeof(esc), (value)) < 0) goto fail;          \
        n = snprintf(buf + pos, buf_size - pos, prefix "\"%s\"", esc);      \
        if (n < 0 || (size_t)n >= buf_size - pos) goto fail;                \
        pos += (size_t)n;                                                    \
    } while (0)

    /* Opening brace. */
    buf[pos++] = '{';

    APPEND_FIELD("\"command\":",    req->command);
    APPEND_FIELD(",\"runas_user\":", req->runas_user);
    APPEND_FIELD(",\"user\":",      req->user);
    APPEND_FIELD(",\"host\":",      req->host);
    APPEND_FIELD(",\"tty\":",       req->tty);
    APPEND_FIELD(",\"cwd\":",       req->cwd);

#undef APPEND_FIELD

    /* "argv": ["arg0", "arg1", ...] */
    n = snprintf(buf + pos, buf_size - pos, ",\"argv\":[");
    if (n < 0 || (size_t)n >= buf_size - pos) goto fail;
    pos += (size_t)n;

    for (int i = 0; i < req->argc && req->argv[i] != NULL; i++) {
        if (i > 0) {
            if (pos + 1 >= buf_size) goto fail;
            buf[pos++] = ',';
        }
        if (json_escape(esc, sizeof(esc), req->argv[i]) < 0) goto fail;
        n = snprintf(buf + pos, buf_size - pos, "\"%s\"", esc);
        if (n < 0 || (size_t)n >= buf_size - pos) goto fail;
        pos += (size_t)n;
    }

    n = snprintf(buf + pos, buf_size - pos, "]}");
    if (n < 0 || (size_t)n >= buf_size - pos) goto fail;
    pos += (size_t)n;

    return buf;

fail:
    free(buf);
    return NULL;
}

/*
 * Skip whitespace in a JSON string.
 */
static const char *skip_ws(const char *p, const char *end)
{
    while (p < end && isspace((unsigned char)*p))
        p++;
    return p;
}

/*
 * Try to match a literal string at position p.
 * Returns pointer past the literal on match, or NULL.
 */
static const char *match_literal(const char *p, const char *end, const char *lit)
{
    size_t len = strlen(lit);
    if ((size_t)(end - p) >= len && memcmp(p, lit, len) == 0)
        return p + len;
    return NULL;
}

/*
 * Find a JSON key at the top level of an object, skipping over string values.
 *
 * Unlike memmem(), this won't match keys that appear inside string values.
 * Only handles the flat JSON objects the daemon sends — no nesting.
 *
 * Returns pointer to the first char after the key's closing quote,
 * or NULL if the key is not found.
 */
static const char *find_key(const char *p, const char *end, const char *key)
{
    size_t klen = strlen(key);

    while (p < end) {
        p = skip_ws(p, end);
        if (p >= end)
            break;

        if (*p == '"') {
            /* Start of a quoted string — check if it matches our key. */
            p++; /* skip opening quote */
            const char *start = p;

            /* Find the closing quote, respecting escapes. */
            while (p < end && *p != '"') {
                if (*p == '\\' && p + 1 < end)
                    p++; /* skip escaped char */
                p++;
            }
            if (p >= end)
                break;

            size_t slen = (size_t)(p - start);
            p++; /* skip closing quote */

            /* Check if this string matches the key we're looking for. */
            if (slen == klen && memcmp(start, key, klen) == 0) {
                /* Verify it's followed by ':' (it's a key, not a value). */
                const char *after = skip_ws(p, end);
                if (after < end && *after == ':')
                    return after + 1; /* return pointer past the colon */
            }

            /* If this was a string value (after ':'), skip it. */
            continue;
        }

        /* Skip any non-quote character (braces, commas, booleans, numbers). */
        p++;
    }

    return NULL;
}

int json_parse_response(const char *json, size_t len, struct approval_response *resp)
{
    /*
     * We parse a very specific shape:
     *   {"approved": true}
     *   {"approved": false, "reason": "some text"}
     *
     * This is NOT a general-purpose JSON parser. It handles exactly the
     * response format the daemon sends, nothing more. However, it properly
     * skips string values to avoid matching keys inside quoted strings.
     */
    memset(resp, 0, sizeof(*resp));
    resp->approved = false;
    resp->reason[0] = '\0';

    const char *end = json + len;

    /* Find "approved" key (returns pointer past the ':'). */
    const char *val = find_key(json, end, "approved");
    if (!val)
        return -1;

    val = skip_ws(val, end);

    /* Parse boolean value. */
    const char *after;
    if ((after = match_literal(val, end, "true")) != NULL) {
        resp->approved = true;
    } else if ((after = match_literal(val, end, "false")) != NULL) {
        resp->approved = false;
    } else {
        return -1;
    }

    /* Optionally parse "reason" key. */
    val = find_key(json, end, "reason");
    if (val) {
        val = skip_ws(val, end);
        if (val < end && *val == '"') {
            val++; /* skip opening quote */
            size_t ri = 0;
            while (val < end && *val != '"' && ri < sizeof(resp->reason) - 1) {
                if (*val == '\\' && val + 1 < end) {
                    val++; /* skip backslash, take next char */
                }
                resp->reason[ri++] = *val++;
            }
            resp->reason[ri] = '\0';
        }
    }

    return 0;
}
