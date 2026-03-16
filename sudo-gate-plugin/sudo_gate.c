/*
 * sudo_gate.so — Sudo Approval Plugin (type 4)
 *
 * Intercepts every sudo invocation and gates it through an external approval
 * daemon (sudo-gated) via a Unix domain socket. The daemon forwards the
 * request to the orchestrator over NATS; a human operator approves or denies
 * via the cockpit UI.
 *
 * Lifecycle (managed by sudo):
 *   1. sudo loads this .so and calls sudo_gate_open()
 *   2. After the policy plugin (sudoers) approves, sudo calls sudo_gate_check()
 *   3. check() connects to the daemon socket, sends the command details,
 *      and blocks (via poll()) until a response arrives or timeout fires
 *   4. sudo calls sudo_gate_close() after the command finishes or is rejected
 *
 * Plugin options (set in /etc/sudo.conf):
 *   socket_path=/run/sudo-gated/sudo-gated.sock  — daemon socket
 *   timeout=305                                    — poll() timeout in seconds
 *   fail_mode=deny                                 — behavior on error: "deny" or "open"
 *
 * CRITICAL: A broken plugin prevents ALL sudo usage. Always test with a
 * root shell open in another terminal. Use fail_mode=open during development.
 *
 * Build:
 *   gcc -fPIC -shared -Wall -Wextra -O2 -I./include -o sudo_gate.so \
 *       sudo_gate.c json_util.c
 *
 * Install:
 *   sudo install -o root -g root -m 0644 sudo_gate.so /usr/libexec/sudo/
 *   echo 'Plugin sudo_gate_approval sudo_gate.so' | sudo tee -a /etc/sudo.conf
 */

/* No _GNU_SOURCE needed — all APIs used are POSIX. */

#include "include/sudo_plugin.h"
#include "json_util.h"

#include <arpa/inet.h>   /* htonl, ntohl */
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <syslog.h>
#include <unistd.h>

/* --------------------------------------------------------------------------
 * Configuration (set from plugin_options[] in open())
 * -------------------------------------------------------------------------- */

static const char *cfg_socket_path = "/run/sudo-gated/sudo-gated.sock";
static int         cfg_timeout_sec = 305;
static bool        cfg_fail_open   = false;  /* false = deny on error (production) */

/* --------------------------------------------------------------------------
 * State captured during open() for use in check()
 * -------------------------------------------------------------------------- */

static sudo_printf_t plugin_printf = NULL;

/* User info from open(). */
static char *state_user     = NULL;
static char *state_host     = NULL;
static char *state_tty      = NULL;
static char *state_cwd      = NULL;

/* Submit command from open(). */
static int   state_argc     = 0;
static char **state_argv    = NULL;

/* --------------------------------------------------------------------------
 * Helpers
 * -------------------------------------------------------------------------- */

#define LOG_TAG "sudo_gate"

static void log_info(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsyslog(LOG_AUTH | LOG_INFO, fmt, ap);
    va_end(ap);
}

static void log_err(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsyslog(LOG_AUTH | LOG_ERR, fmt, ap);
    va_end(ap);
}

/*
 * Look up a key in a NULL-terminated key=value array (settings[], user_info[]).
 * Returns the value (pointer into the array entry), or NULL if not found.
 */
static const char *lookup(char * const arr[], const char *key)
{
    if (!arr)
        return NULL;

    size_t klen = strlen(key);
    for (int i = 0; arr[i] != NULL; i++) {
        if (strncmp(arr[i], key, klen) == 0 && arr[i][klen] == '=')
            return arr[i] + klen + 1;
    }
    return NULL;
}

/*
 * Duplicate a string, returning "" for NULL input.
 */
static char *safe_strdup(const char *s)
{
    return strdup(s ? s : "");
}

/*
 * Free the state captured during open().
 */
static void free_state(void)
{
    free(state_user);  state_user = NULL;
    free(state_host);  state_host = NULL;
    free(state_tty);   state_tty  = NULL;
    free(state_cwd);   state_cwd  = NULL;

    if (state_argv) {
        for (int i = 0; i < state_argc; i++)
            free(state_argv[i]);
        free(state_argv);
        state_argv = NULL;
    }
    state_argc = 0;
}

/* --------------------------------------------------------------------------
 * Socket communication
 * -------------------------------------------------------------------------- */

/*
 * Connect to the daemon's Unix domain socket.
 * Returns the socket fd, or -1 on error.
 */
static int connect_to_daemon(void)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        log_err(LOG_TAG ": socket(): %s", strerror(errno));
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, cfg_socket_path, sizeof(addr.sun_path) - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        log_err(LOG_TAG ": connect(%s): %s", cfg_socket_path, strerror(errno));
        close(fd);
        return -1;
    }

    return fd;
}

/*
 * Send a length-prefixed JSON message over the socket.
 *
 * Wire format: 4-byte big-endian uint32 (payload length) + JSON bytes.
 */
static int send_request(int fd, const char *json, size_t json_len)
{
    uint32_t net_len = htonl((uint32_t)json_len);

    /* Send length prefix. */
    if (write(fd, &net_len, 4) != 4) {
        log_err(LOG_TAG ": write length prefix: %s", strerror(errno));
        return -1;
    }

    /* Send JSON payload. */
    size_t sent = 0;
    while (sent < json_len) {
        ssize_t n = write(fd, json + sent, json_len - sent);
        if (n <= 0) {
            log_err(LOG_TAG ": write payload: %s", strerror(errno));
            return -1;
        }
        sent += (size_t)n;
    }

    return 0;
}

/*
 * Read exactly `count` bytes from fd, using poll() for timeout.
 * Returns 0 on success, -1 on error or timeout.
 */
static int read_exact(int fd, void *buf, size_t count, int timeout_ms)
{
    size_t total = 0;
    while (total < count) {
        struct pollfd pfd = { .fd = fd, .events = POLLIN };
        int ret = poll(&pfd, 1, timeout_ms);

        if (ret < 0) {
            if (errno == EINTR)
                continue;
            log_err(LOG_TAG ": poll(): %s", strerror(errno));
            return -1;
        }
        if (ret == 0) {
            log_err(LOG_TAG ": poll() timeout after %ds", timeout_ms / 1000);
            return -1;
        }
        if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
            log_err(LOG_TAG ": poll() error event: 0x%x", pfd.revents);
            return -1;
        }

        ssize_t n = read(fd, (char *)buf + total, count - total);
        if (n <= 0) {
            log_err(LOG_TAG ": read(): %s",
                    n == 0 ? "connection closed" : strerror(errno));
            return -1;
        }
        total += (size_t)n;
    }
    return 0;
}

/*
 * Receive a length-prefixed JSON response from the daemon.
 * Uses poll() with the configured timeout.
 *
 * Returns a malloc'd string (caller must free), or NULL on error/timeout.
 * Sets *out_len to the response length.
 */
static char *recv_response(int fd, size_t *out_len)
{
    int timeout_ms = cfg_timeout_sec * 1000;

    /* Read 4-byte length prefix. */
    uint32_t net_len;
    if (read_exact(fd, &net_len, 4, timeout_ms) < 0)
        return NULL;

    uint32_t payload_len = ntohl(net_len);

    /* Sanity check — same 64 KiB limit as the daemon. */
    if (payload_len > 65536) {
        log_err(LOG_TAG ": response too large: %u bytes", payload_len);
        return NULL;
    }

    /* Read JSON payload. */
    char *buf = malloc(payload_len + 1);
    if (!buf)
        return NULL;

    if (read_exact(fd, buf, payload_len, timeout_ms) < 0) {
        free(buf);
        return NULL;
    }

    buf[payload_len] = '\0';
    *out_len = payload_len;
    return buf;
}

/* --------------------------------------------------------------------------
 * Plugin callbacks
 * -------------------------------------------------------------------------- */

/*
 * open() — called once when sudo loads the plugin.
 *
 * We capture user information and plugin options here, since check()
 * doesn't receive user_info[].
 */
static int sudo_gate_open(
    unsigned int version,
    sudo_conv_t conversation __attribute__((unused)),
    sudo_printf_t sudo_printf,
    char * const settings[] __attribute__((unused)),
    char * const user_info[],
    int submit_optind __attribute__((unused)),
    char * const submit_argv[],
    char * const submit_envp[] __attribute__((unused)),
    char * const plugin_options[],
    const char **errstr __attribute__((unused)))
{
    openlog(LOG_TAG, LOG_PID, LOG_AUTH);
    plugin_printf = sudo_printf;

    /* Check API version compatibility. */
    if (SUDO_API_VERSION_GET_MAJOR(version) != SUDO_API_VERSION_MAJOR) {
        log_err(LOG_TAG ": incompatible API version %d.%d (expected %d.x)",
                SUDO_API_VERSION_GET_MAJOR(version),
                SUDO_API_VERSION_GET_MINOR(version),
                SUDO_API_VERSION_MAJOR);
        return SUDO_RC_ERROR;
    }

    /* Parse plugin options. */
    if (plugin_options) {
        const char *v;
        if ((v = lookup(plugin_options, "socket_path")) != NULL)
            cfg_socket_path = v;  /* Points into sudo's memory, valid for plugin lifetime */
        if ((v = lookup(plugin_options, "timeout")) != NULL) {
            char *endp;
            long t = strtol(v, &endp, 10);
            if (endp != v && t > 0 && t <= 3600)
                cfg_timeout_sec = (int)t;
            else
                log_err(LOG_TAG ": invalid timeout '%s', using default %d", v, cfg_timeout_sec);
        }
        if ((v = lookup(plugin_options, "fail_mode")) != NULL)
            cfg_fail_open = (strcmp(v, "open") == 0);
    }

    /* Capture user info for check(). */
    state_user = safe_strdup(lookup(user_info, "user"));
    state_host = safe_strdup(lookup(user_info, "host"));
    state_tty  = safe_strdup(lookup(user_info, "tty"));
    state_cwd  = safe_strdup(lookup(user_info, "cwd"));

    /* Capture submit_argv (the full command as the user typed it). */
    if (submit_argv) {
        int argc = 0;
        while (submit_argv[argc] != NULL)
            argc++;
        state_argc = argc;
        state_argv = calloc((size_t)(argc + 1), sizeof(char *));
        if (state_argv) {
            for (int i = 0; i < argc; i++)
                state_argv[i] = strdup(submit_argv[i]);
        }
    }

    log_info(LOG_TAG ": loaded (socket=%s, timeout=%ds, fail_mode=%s)",
             cfg_socket_path, cfg_timeout_sec, cfg_fail_open ? "open" : "deny");

    return SUDO_RC_OK;
}

/*
 * close() — called when sudo is done with the plugin.
 */
static void sudo_gate_close(void)
{
    free_state();
    closelog();
}

/*
 * check() — called after the policy plugin approves, before command execution.
 *
 * This is where the approval gate lives. We:
 *   1. Extract command details from command_info[] and run_argv[]
 *   2. Connect to the daemon's Unix socket
 *   3. Send a length-prefixed JSON request
 *   4. Block on poll() waiting for the response (up to cfg_timeout_sec)
 *   5. Return 1 (approve) or 0 (deny)
 */
static int sudo_gate_check(
    char * const command_info[],
    char * const run_argv[],
    char * const run_envp[] __attribute__((unused)),
    const char **errstr)
{
#ifdef STUB_MODE
    /* Stub mode: log the command and always approve. For testing plugin loading. */
    const char *cmd = lookup(command_info, "command");
    log_info(LOG_TAG ": STUB APPROVE: %s", cmd ? cmd : "(null)");
    (void)run_argv; (void)errstr;
    return SUDO_RC_OK;
#endif

    int result = cfg_fail_open ? SUDO_RC_OK : SUDO_RC_ERROR;

    /* Extract command path from command_info[]. */
    const char *command = lookup(command_info, "command");
    const char *runas_user = lookup(command_info, "runas_user");

    if (!command) {
        log_err(LOG_TAG ": no command in command_info");
        if (errstr)
            *errstr = "sudo_gate: no command found";
        return result;
    }

    /* Count run_argv entries. */
    int run_argc = 0;
    if (run_argv) {
        while (run_argv[run_argc] != NULL)
            run_argc++;
    }

    /* Build the JSON request. */
    struct approval_request req = {
        .command    = command,
        .runas_user = runas_user ? runas_user : "root",
        .user       = state_user ? state_user : "unknown",
        .host       = state_host ? state_host : "unknown",
        .tty        = state_tty  ? state_tty  : "",
        .cwd        = state_cwd  ? state_cwd  : "",
        .argc       = run_argc,
        .argv       = run_argv,
    };

    char *json = json_build_request(&req);
    if (!json) {
        log_err(LOG_TAG ": failed to build JSON request");
        if (errstr)
            *errstr = "sudo_gate: JSON serialization failed";
        return result;
    }

    /* Connect to daemon. */
    int fd = connect_to_daemon();
    if (fd < 0) {
        log_err(LOG_TAG ": cannot connect to daemon at %s", cfg_socket_path);
        if (plugin_printf)
            plugin_printf(SUDO_CONV_ERROR_MSG,
                          "sudo_gate: approval daemon unavailable\n");
        if (errstr)
            *errstr = "sudo_gate: daemon unavailable";
        free(json);
        return result;
    }

    /* Send the request. */
    if (send_request(fd, json, strlen(json)) < 0) {
        log_err(LOG_TAG ": failed to send request to daemon");
        if (errstr)
            *errstr = "sudo_gate: send failed";
        free(json);
        close(fd);
        return result;
    }
    free(json);

    /* Wait for the response. */
    size_t resp_len = 0;
    char *resp_json = recv_response(fd, &resp_len);
    close(fd);

    if (!resp_json) {
        log_err(LOG_TAG ": no response from daemon (timeout or error)");
        if (plugin_printf)
            plugin_printf(SUDO_CONV_ERROR_MSG,
                          "sudo_gate: approval timed out or daemon error\n");
        if (errstr)
            *errstr = "sudo_gate: no response from daemon";
        return result;
    }

    /* Parse the response. */
    struct approval_response resp;
    if (json_parse_response(resp_json, resp_len, &resp) < 0) {
        log_err(LOG_TAG ": malformed response from daemon: %.*s",
                (int)(resp_len > 200 ? 200 : resp_len), resp_json);
        if (errstr)
            *errstr = "sudo_gate: malformed daemon response";
        free(resp_json);
        return result;
    }
    free(resp_json);

    if (resp.approved) {
        log_info(LOG_TAG ": APPROVED: %s (user=%s, runas=%s)",
                 command, req.user, req.runas_user);
        return SUDO_RC_OK;
    }

    /* Denied. */
    log_info(LOG_TAG ": DENIED: %s (user=%s, runas=%s, reason=%s)",
             command, req.user, req.runas_user, resp.reason);

    if (plugin_printf) {
        if (resp.reason[0] != '\0')
            plugin_printf(SUDO_CONV_ERROR_MSG,
                          "sudo request denied by operator: %s\n", resp.reason);
        else
            plugin_printf(SUDO_CONV_ERROR_MSG,
                          "sudo request denied by operator\n");
    }

    if (errstr)
        *errstr = "sudo_gate: request denied by operator";

    return SUDO_RC_REJECT;
}

/*
 * show_version() — called by `sudo -V`.
 */
static int sudo_gate_show_version(int verbose)
{
    if (plugin_printf) {
        plugin_printf(SUDO_CONV_INFO_MSG, "sudo_gate approval plugin version 1.0.0\n");
        if (verbose) {
            plugin_printf(SUDO_CONV_INFO_MSG, "  socket: %s\n", cfg_socket_path);
            plugin_printf(SUDO_CONV_INFO_MSG, "  timeout: %ds\n", cfg_timeout_sec);
            plugin_printf(SUDO_CONV_INFO_MSG, "  fail_mode: %s\n",
                          cfg_fail_open ? "open" : "deny");
        }
    }
    return SUDO_RC_OK;
}

/* --------------------------------------------------------------------------
 * Plugin registration
 * --------------------------------------------------------------------------
 *
 * sudo looks for a symbol whose name matches the second field in
 * /etc/sudo.conf's Plugin line:
 *
 *   Plugin sudo_gate_approval sudo_gate.so socket_path=...
 *
 * The symbol must be a struct approval_plugin with type = SUDO_APPROVAL_PLUGIN.
 */

__attribute__((visibility("default")))
struct approval_plugin sudo_gate_approval = {
    .type         = SUDO_APPROVAL_PLUGIN,
    .version      = SUDO_API_VERSION,
    .open         = sudo_gate_open,
    .close        = sudo_gate_close,
    .check        = sudo_gate_check,
    .show_version = sudo_gate_show_version,
};
