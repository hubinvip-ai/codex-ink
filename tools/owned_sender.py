#!/usr/bin/env python3
"""Retain inherited flock ownership until the actual BLE subprocess exits.

The parent starts this supervisor in its own process group and passes the
sender-lock descriptor. If the parent is killed, this process still owns the
same open-file-description lock. Never unlock or close inherited descriptors
while a transport child can still write. Children stay in this process group.
"""

import os
import errno
import select
import signal
import subprocess
import sys
import time


def main():
    if len(sys.argv) != 5:
        return 2
    # Refuse direct invocations that might kill somebody else's process group.
    if os.getpgrp() != os.getpid():
        return 2
    binary, frame, device, descriptor = sys.argv[1:]
    owner_fd = int(descriptor)
    if owner_fd >= 0:
        os.fstat(owner_fd)
    command = [binary, '--input', frame, '--device', device]
    # /usr/bin/script creates a new child session, escaping an outer killpg.
    # Supply a PTY ourselves without setsid in the leaf process instead.
    master, slave = os.openpty()
    try:
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                   pass_fds=() if owner_fd < 0 else (owner_fd,))
        os.close(slave)
        deadline = time.monotonic() + 125
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 125)
            ready, _, _ = select.select([master], [], [], min(remaining, .1))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b''
                if not chunk:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                    break
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise ValueError('excessive sender output')
    except BaseException:
        # Includes timeout with a dead parent: stop all members of OUR group.
        os.killpg(os.getpgrp(), signal.SIGKILL)
        return 1
    finally:
        os.close(master)
    sys.stdout.buffer.write(output)
    return process.returncode


if __name__ == '__main__':
    raise SystemExit(main())
