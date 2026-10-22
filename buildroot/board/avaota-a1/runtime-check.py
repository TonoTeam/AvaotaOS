#!/usr/bin/env python3
"""Fail closed if the ordinary-user kiosk cannot create Chromium namespaces.

Run inside the same RootDirectory mount namespace and user as Tono Canvas.
The short-lived child changes its own namespace; the parent is unaffected.
"""
import ctypes
import os
import sys


def main():
    if os.getuid() == 0:
        print("tono-sandbox: refusing to run the kiosk as root", file=sys.stderr)
        return 1
    for name in ("QTWEBENGINE_DISABLE_SANDBOX", "QTWEBENGINE_CHROMIUM_FLAGS"):
        value = os.environ.get(name, "")
        forbidden = ("--no-sandbox", "--disable-namespace-sandbox", "--disable-seccomp-filter-sandbox")
        if (name == "QTWEBENGINE_DISABLE_SANDBOX" and value) or any(flag in value for flag in forbidden):
            print("tono-sandbox: sandbox-disabling environment is forbidden", file=sys.stderr)
            return 1
    child = os.fork()
    if child == 0:
        library = ctypes.CDLL(None, use_errno=True)
        library.unshare.argtypes = [ctypes.c_int]
        library.unshare.restype = ctypes.c_int
        # A user namespace is precisely what plain chroot prevents. Test PID
        # namespace creation as well, because Chromium uses both facilities.
        if library.unshare(0x10000000 | 0x20000000) != 0:  # CLONE_NEWUSER | CLONE_NEWPID
            code = ctypes.get_errno()
            print("tono-sandbox: namespace preflight failed: " + os.strerror(code), file=sys.stderr, flush=True)
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(child, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        return 1
    print("tono-sandbox: ordinary-user USER/PID namespace preflight passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
