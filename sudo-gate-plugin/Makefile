CC       := gcc
CFLAGS   := -fPIC -shared -Wall -Wextra -Werror -O2 -I./include
LDFLAGS  :=

TARGET   := sudo_gate.so
SOURCES  := sudo_gate.c json_util.c

.PHONY: all clean install uninstall test-stub

all: $(TARGET)

$(TARGET): $(SOURCES) json_util.h include/sudo_plugin.h
	$(CC) $(CFLAGS) -o $@ $(SOURCES) $(LDFLAGS)

clean:
	rm -f $(TARGET)

# Install to the standard sudo plugin directory.
# MUST be run as root.
install: $(TARGET)
	install -o root -g root -m 0644 $(TARGET) /usr/libexec/sudo/
	@echo "Installed to /usr/libexec/sudo/$(TARGET)"
	@echo "Add to /etc/sudo.conf:"
	@echo "  Plugin sudo_gate_approval sudo_gate.so socket_path=/run/sudo-gated/sudo-gated.sock timeout=305 fail_mode=deny"

uninstall:
	rm -f /usr/libexec/sudo/$(TARGET)
	@echo "Removed /usr/libexec/sudo/$(TARGET)"
	@echo "Don't forget to remove the Plugin line from /etc/sudo.conf"

# Build a stub version that always approves (for initial testing).
# This avoids needing the daemon running to verify plugin loading works.
test-stub:
	$(CC) -fPIC -shared -Wall -Wextra -O2 -I./include \
		-DSTUB_MODE \
		-o sudo_gate_stub.so sudo_gate.c json_util.c
	@echo "Built sudo_gate_stub.so (always approves, logs to syslog)"
