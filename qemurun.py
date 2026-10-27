#!/usr/bin/env python3
"""Boot xv6 in QEMU, run shell commands, save the console, quit.

    python3 qemurun.py out.log 'schedctl sjf 10' 'tracedump workload 42'
    python3 qemurun.py --timeout 300 out.log 'usertests -q'

Each command is typed at the xv6 shell once the previous one has
returned to the prompt. The whole console session is written to out.log
(the format analyze.py reads). Exits non-zero on a timeout or a panic.
Builds nothing: run `make fs.img` first if user programs changed.

QEMU runs on a scratch copy of fs.img next to the log, so every run
starts from a clean file system and an interactive `make qemu` session
can stay open at the same time.
"""

import argparse
import os
import pty
import select
import shutil
import signal
import sys
import time

QEMU = ["qemu-system-riscv64", "-machine", "virt", "-bios", "none",
        "-kernel", "kernel/kernel", "-m", "128M", "-nographic",
        "-global", "virtio-mmio.force-legacy=false"]

PROMPT = b"\n$ "


def read_until(fd, cond, timeout, log):
    """Read console bytes into log until cond(buf) holds or timeout."""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        log.write(data)
        log.flush()
        buf += data
        if b"panic" in data:
            return buf, "panic"
        if cond(buf):
            return buf, "ok"
    return buf, "timeout"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log")
    ap.add_argument("cmds", nargs="+")
    ap.add_argument("--timeout", type=float, default=180,
                    help="seconds allowed per command (default 180)")
    ap.add_argument("--cpus", default="1")
    args = ap.parse_args()

    img = os.path.splitext(args.log)[0] + ".fs.img"
    shutil.copyfile("fs.img", img)
    argv = QEMU + ["-smp", args.cpus,
                   "-drive", f"file={img},if=none,format=raw,id=x0",
                   "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0"]

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)

    status = 0
    with open(args.log, "wb") as log:
        try:
            _, how = read_until(fd, lambda b: b.endswith(b"$ ") and
                                b"init: starting sh" in b, 60, log)
            if how != "ok":
                print(f"qemurun: boot {how}", file=sys.stderr)
                status = 1
            else:
                for cmd in args.cmds:
                    if cmd == "^P":  # xv6 process dump, then wait briefly
                        os.write(fd, b"\x10")
                        read_until(fd, lambda b: False, 2, log)
                        continue
                    os.write(fd, cmd.encode() + b"\n")
                    _, how = read_until(fd, lambda b: b.endswith(PROMPT),
                                        args.timeout, log)
                    if how != "ok":
                        print(f"qemurun: '{cmd}' {how}", file=sys.stderr)
                        status = 1
                        break
        finally:
            try:
                os.write(fd, b"\x01x")  # ctrl-a x: quit QEMU
                time.sleep(0.5)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.unlink(img)
    sys.exit(status)


if __name__ == "__main__":
    main()
