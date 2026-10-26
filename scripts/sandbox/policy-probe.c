/* SPDX-License-Identifier: MIT
 * Independently authored, non-destructive policy probes. Rejection does not
 * identify its security layer; retain audit records and baseline comparisons.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/capability.h>
#include <linux/sched.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int failures;

static void result(const char *name, long value, int error, int expected) {
    int ok = value == -1 && error == expected;
    printf("{\"probe\":\"%s\",\"return\":%ld,\"errno\":%d,\"expected_errno\":%d,\"match\":%s}\n",
           name, value, error, expected, ok ? "true" : "false");
    if (!ok) failures++;
}

static void denied(const char *name, long value) {
    int error = errno;
    result(name, value, error, EPERM);
}

static void identity(void) {
    const char *paths[] = {"/proc/self/status", "/proc/self/attr/current"};
    for (unsigned i = 0; i < sizeof(paths)/sizeof(paths[0]); i++) {
        FILE *f = fopen(paths[i], "r");
        if (!f) { perror(paths[i]); failures++; continue; }
        char line[512];
        while (fgets(line, sizeof(line), f)) {
            if (i || !strncmp(line, "Uid:", 4) || !strncmp(line, "Gid:", 4) ||
                !strncmp(line, "Cap", 3) || !strncmp(line, "NoNewPrivs:", 11) ||
                !strncmp(line, "Seccomp", 7)) printf("evidence %s %s", paths[i], line);
        }
        fclose(f);
    }
}

static void clone_reserved(uint64_t bit, const char *name) {
    errno = 0;
    long value = syscall(SYS_clone, bit | SIGCHLD, 0, 0, 0, 0);
    int error = errno;
    if (value == 0) _exit(0);
    if (value > 0) {
        int status;
        if (waitpid((pid_t)value, &status, 0) < 0) perror("waitpid reserved clone");
    }
    result(name, value, error, EPERM);
}

static void canary(const char *path) {
    errno = 0;
    int fd = open(path, O_WRONLY | O_APPEND | O_CLOEXEC);
    int error = errno;
    if (fd >= 0) close(fd);
    /* O_WRONLY tests write access but deliberately writes no bytes. */
    result("outside-writable-canary-open", fd, error, EACCES);
}

static void scratch_exec(const char *self, const char *dir) {
    char path[256], buffer[32768];
    snprintf(path, sizeof(path), "%s/webcappi-probe-%ld", dir, (long)getpid());
    int in = open(self, O_RDONLY | O_CLOEXEC);
    int out = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0700);
    if (in < 0 || out < 0) {
        perror("prepare writable execution canary");
        if (in >= 0) close(in);
        if (out >= 0) { close(out); unlink(path); }
        failures++;
        return;
    }
    ssize_t n;
    int copied = 1;
    while ((n = read(in, buffer, sizeof(buffer))) > 0) {
        ssize_t pos = 0;
        while (pos < n) {
            ssize_t w = write(out, buffer + pos, (size_t)(n-pos));
            if (w <= 0) { copied = 0; break; }
            pos += w;
        }
        if (!copied) break;
    }
    if (n < 0) copied = 0;
    close(in);
    close(out);
    if (!copied) { perror("copy writable execution canary"); unlink(path); failures++; return; }
    pid_t child = fork();
    if (child == 0) {
        execl(path, path, "--execution-canary", (char *)NULL);
        _exit(errno == EACCES ? 0 : 125);
    }
    int status = 0;
    int ok = child > 0 && waitpid(child, &status, 0) == child &&
             WIFEXITED(status) && WEXITSTATUS(status) == 0;
    printf("{\"probe\":\"scratch-exec-%s\",\"match\":%s,\"wait_status\":%d}\n", dir, ok ? "true" : "false", status);
    if (!ok) failures++;
    if (unlink(path)) { perror("unlink execution canary"); failures++; }
}

static void ptrace_child(void) {
    pid_t child = fork();
    if (child == 0) {
        errno = 0;
        long value = syscall(SYS_ptrace, PTRACE_TRACEME, 0, 0, 0);
        int error = errno;
        /* No exec or stop follows, even if tracing was unexpectedly allowed. */
        _exit(value == -1 && error == EPERM ? 0 : 125);
    }
    int status = 0;
    int ok = child > 0 && waitpid(child, &status, 0) == child &&
             WIFEXITED(status) && WEXITSTATUS(status) == 0;
    printf("{\"probe\":\"ptrace-traceme-child\",\"match\":%s,\"wait_status\":%d}\n", ok ? "true" : "false", status);
    if (!ok) failures++;
}

static void initial_privilege_child(int capability) {
    pid_t child = fork();
    if (child == 0) {
        long value;
        errno = 0;
        if (capability) {
            struct __user_cap_header_struct h = {_LINUX_CAPABILITY_VERSION_3, 0};
            struct __user_cap_data_struct d[2] = {{0}, {0}};
            d[CAP_SYS_ADMIN / 32].permitted = 1U << (CAP_SYS_ADMIN % 32);
            d[CAP_SYS_ADMIN / 32].effective = 1U << (CAP_SYS_ADMIN % 32);
            value = syscall(SYS_capset, &h, d);
        } else value = syscall(SYS_setuid, 0);
        _exit(value == -1 && errno == EPERM ? 0 : 125);
    }
    int status = 0;
    int ok = child > 0 && waitpid(child, &status, 0) == child &&
             WIFEXITED(status) && WEXITSTATUS(status) == 0;
    printf("{\"probe\":\"initial-namespace-%s\",\"match\":%s,\"wait_status\":%d}\n",
           capability ? "capset-sys-admin" : "setuid-root", ok ? "true" : "false", status);
    if (!ok) failures++;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--execution-canary")) return 42;
    const char *canary_path = NULL;
    if (argc == 3 && !strcmp(argv[1], "--canary")) canary_path = argv[2];
    else if (argc != 1) { fprintf(stderr, "usage: %s [--canary PATH]\n", argv[0]); return 2; }
    setvbuf(stdout, NULL, _IONBF, 0);
    identity();
    initial_privilege_child(1);
    initial_privilege_child(0);
    clone_reserved(UINT64_C(1)<<63, "clone-reserved-bit63");
    clone_reserved(UINT64_C(1)<<32, "clone-unapproved-bit32");
    errno = 0;
    denied("unshare-high-bit63", syscall(SYS_unshare, UINT64_C(1)<<63));
    errno = 0;
    denied("unshare-high-bit32", syscall(SYS_unshare, UINT64_C(1)<<32));
    errno = 0;
    denied("unshare-unapproved-cgroup", syscall(SYS_unshare, CLONE_NEWCGROUP));
#ifdef SYS_clone3
    errno = 0;
    long clone3_value = syscall(SYS_clone3, NULL, 0);
    result("clone3-fallback", clone3_value, errno, ENOSYS);
#endif
    errno = 0;
    int raw = socket(AF_INET, SOCK_RAW | SOCK_CLOEXEC, IPPROTO_RAW);
    int raw_error = errno;
    if (raw >= 0) close(raw);
    result("inet-raw-socket", raw, raw_error, EPERM);
    errno = 0;
    int packet = socket(AF_PACKET, SOCK_RAW | SOCK_CLOEXEC, 0);
    int packet_error = errno;
    if (packet >= 0) close(packet);
    result("packet-raw-socket", packet, packet_error, EPERM);
    ptrace_child();
#ifdef SYS_bpf
    errno = 0;
    denied("bpf-invalid-command", syscall(SYS_bpf, -1, NULL, 0));
#endif
#ifdef SYS_keyctl
    errno = 0;
    denied("keyctl-invalid-command", syscall(SYS_keyctl, -1, 0, 0, 0, 0));
#endif
#ifdef SYS_process_vm_readv
    errno = 0;
    denied("process-vm-readv-empty-self", syscall(SYS_process_vm_readv, getpid(), NULL, 0, NULL, 0, 0));
#endif
#ifdef SYS_io_uring_setup
    errno = 0;
    denied("io-uring-setup-empty", syscall(SYS_io_uring_setup, 0, NULL));
#endif
    if (canary_path) canary(canary_path);
    else puts("SKIP outside-writable-canary: supply --canary PATH after validating DAC and mount writability without AppArmor");
    scratch_exec(argv[0], "/work");
    scratch_exec(argv[0], "/tmp");
    scratch_exec(argv[0], "/dev/shm");
    printf("{\"probe_summary_failures\":%d,\"attribution\":\"requires-baseline-and-audit\"}\n", failures);
    return failures ? 1 : 0;
}
