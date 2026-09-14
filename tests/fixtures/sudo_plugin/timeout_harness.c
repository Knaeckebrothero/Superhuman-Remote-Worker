/* Run the real plugin option parser and framed response reader with a simulated
 * poll clock, so a 30-minute wait can be checked without sleeping. */
#include <poll.h>
static int simulated_wait_ms;
static int simulated_poll(struct pollfd *fds, nfds_t count, int timeout_ms)
{
    (void)count;
    if (timeout_ms < simulated_wait_ms)
        return 0;
    fds[0].revents = POLLIN;
    return 1;
}
#define poll simulated_poll
#include "sudo_gate.c"
#undef poll

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    simulated_wait_ms = atoi(argv[1]);
    if (sudo_gate_open(SUDO_API_VERSION, NULL, NULL, NULL, NULL, 0,
                       NULL, NULL, argv + 2, NULL) != SUDO_RC_OK) return 3;
    int fds[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fds)) return 4;
    const char response[] = "{\"approved\":true}";
    uint32_t length = htonl(sizeof(response) - 1);
    if (write(fds[0], &length, sizeof(length)) != sizeof(length)) return 5;
    if (write(fds[0], response, sizeof(response) - 1) != sizeof(response) - 1) return 6;
    size_t received_len = 0;
    char *received = recv_response(fds[1], &received_len);
    int result = received && received_len == sizeof(response) - 1 &&
        strcmp(received, response) == 0 ? 0 : 1;
    free(received);
    close(fds[0]);
    close(fds[1]);
    sudo_gate_close();
    return result;
}
