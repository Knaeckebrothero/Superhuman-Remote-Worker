/*
 * Minimal sudo plugin API header -- extracted from sudo 1.9.15p5 source.
 *
 * Only the types and defines needed for an approval plugin (type 4) are
 * included here.  For the full header, obtain sudo source:
 *   apt-get source sudo
 *
 * Original copyright:
 *   (c) 2009-2023 Todd C. Miller
 *   SPDX-License-Identifier: ISC
 */

#ifndef SUDO_PLUGIN_H
#define SUDO_PLUGIN_H

/* Plugin API version -- sudo 1.9.x uses major 1. */
#define SUDO_API_VERSION_MAJOR  1
#define SUDO_API_VERSION_MINOR  21
#define SUDO_API_MKVERSION(x, y) (((x) << 16) | (y))
#define SUDO_API_VERSION SUDO_API_MKVERSION(SUDO_API_VERSION_MAJOR, \
                                            SUDO_API_VERSION_MINOR)
#define SUDO_API_VERSION_GET_MAJOR(v) ((v) >> 16)
#define SUDO_API_VERSION_GET_MINOR(v) ((v) & 0xffff)

/* Plugin types. */
#define SUDO_POLICY_PLUGIN      1
#define SUDO_IO_PLUGIN          2
#define SUDO_AUDIT_PLUGIN       3
#define SUDO_APPROVAL_PLUGIN    4

/* Return values from plugin functions. */
#define SUDO_RC_OK               1    /* Approve / success */
#define SUDO_RC_REJECT           0    /* Deny */
#define SUDO_RC_ERROR           -1    /* Error (treated as deny) */
#define SUDO_RC_USAGE_ERROR     -2    /* Usage error (treated as deny) */

/* Printf message types. */
#define SUDO_CONV_INFO_MSG      0x0001
#define SUDO_CONV_ERROR_MSG     0x0002

/*
 * Forward declarations for conversation callback types.
 * The approval plugin does not use conversations, but the open()
 * signature requires these types.
 */
struct sudo_conv_callback;

struct sudo_conv_message {
    int msg_type;
    int timeout;
    const char *msg;
};

struct sudo_conv_reply {
    char *reply;
};

typedef int (*sudo_conv_t)(int num_msgs,
    const struct sudo_conv_message msgs[],
    struct sudo_conv_reply replies[],
    struct sudo_conv_callback *callback);

/* Printf-like function for plugin output. */
typedef int (*sudo_printf_t)(int msg_type, const char *fmt, ...);

/*
 * Approval plugin structure (type 4).
 *
 * Lifecycle:
 *   1. sudo loads the plugin and calls open()
 *   2. After the policy plugin approves, sudo calls check()
 *   3. check() blocks until the approval decision arrives
 *   4. sudo calls close() after the command finishes (or is rejected)
 *
 * check() return values:
 *   1  -- approve (SUDO_RC_OK)
 *   0  -- deny (SUDO_RC_REJECT)
 *  -1  -- error, treated as deny (SUDO_RC_ERROR)
 *  -2  -- usage error, treated as deny (SUDO_RC_USAGE_ERROR)
 */
struct approval_plugin {
    unsigned int type;      /* Always SUDO_APPROVAL_PLUGIN (4) */
    unsigned int version;   /* SUDO_API_VERSION */

    int (*open)(
        unsigned int version,
        sudo_conv_t conversation,
        sudo_printf_t sudo_printf,
        char * const settings[],
        char * const user_info[],
        int submit_optind,
        char * const submit_argv[],
        char * const submit_envp[],
        char * const plugin_options[],
        const char **errstr
    );

    void (*close)(void);

    int (*check)(
        char * const command_info[],
        char * const run_argv[],
        char * const run_envp[],
        const char **errstr
    );

    int (*show_version)(int verbose);
};

#endif /* SUDO_PLUGIN_H */
