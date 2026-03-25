/*
 * Minimal JSON utilities for sudo_gate plugin.
 *
 * Purpose-built for the plugin's specific needs:
 *   - Build a fixed-structure request JSON object
 *   - Parse a simple {"approved": bool, "reason": "..."} response
 *
 * This avoids vendoring a full JSON library (cJSON) for ~20 lines of
 * actual JSON logic. Smaller code = smaller attack surface in a SUID context.
 */

#ifndef JSON_UTIL_H
#define JSON_UTIL_H

#include <stdbool.h>
#include <stddef.h>

/*
 * Approval request fields — populated from sudo's command_info/user_info
 * arrays and passed to json_build_request().
 */
struct approval_request {
    const char *command;      /* Full path to command */
    const char *runas_user;   /* Target user (usually "root") */
    const char *user;         /* Requesting user */
    const char *host;         /* Hostname */
    const char *tty;          /* TTY or "" */
    const char *cwd;          /* Working directory */
    int argc;                 /* Number of arguments */
    char * const *argv;       /* Command arguments */
};

/*
 * Approval response fields — populated by json_parse_response().
 */
struct approval_response {
    bool approved;
    char reason[512];         /* Denial reason (empty string if approved) */
};

/*
 * Build a JSON request string from the approval_request struct.
 *
 * Returns a malloc'd string that the caller must free, or NULL on error.
 * The string is a complete JSON object matching the wire protocol spec.
 */
char *json_build_request(const struct approval_request *req);

/*
 * Parse a JSON response string into the approval_response struct.
 *
 * Returns 0 on success, -1 on parse error.
 */
int json_parse_response(const char *json, size_t len, struct approval_response *resp);

#endif /* JSON_UTIL_H */
