// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Minimal static initrd launcher: never changes the shared root disk.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void checked_mount(const char *source, const char *target,
                          const char *type, unsigned long flags, const char *data) {
    if (mount(source, target, type, flags, data) != 0) {
        perror(target);
        exit(1);
    }
}

int main(void) {
    if (mkdir("/dev", 0755) != 0 && errno != EEXIST) return 1;
    checked_mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    int console = open("/dev/console", O_RDWR);
    if (console < 0) return 1;
    for (int fd = 0; fd < 3; ++fd)
        if (dup2(console, fd) < 0) return 1;
    if (console > 2) close(console);
    for (unsigned int i = 0; access("/dev/vda", F_OK) != 0 && i < 100; ++i)
        usleep(100000);
    if (mkdir("/newroot", 0755) != 0) return 1;
    checked_mount("/dev/vda", "/newroot", "ext4", MS_RDONLY | MS_NOATIME, NULL);
    checked_mount("proc", "/newroot/proc", "proc", 0, NULL);
    checked_mount("sysfs", "/newroot/sys", "sysfs", 0, NULL);
    checked_mount("devtmpfs", "/newroot/dev", "devtmpfs", 0, NULL);
    if (mkdir("/newroot/dev/shm", 0700) != 0 && errno != EEXIST) return 1;
    checked_mount("tmpfs", "/newroot/dev/shm", "tmpfs", 0, "size=512m");
    int script = open("/proof.py", O_RDONLY);
    if (script < 0 || dup2(script, 3) != 3) return 1;
    if (script != 3) close(script);
    if (chroot("/newroot") != 0 || chdir("/") != 0) return 1;
    execl("/usr/bin/python3", "python3", "-u", "/proc/self/fd/3", (char *)NULL);
    perror("python3");
    return 1;
}
