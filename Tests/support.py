"""Portable executable fixtures for interpreters whose paths contain spaces."""
import shlex
import sys


def python_executable_header():
    # The shell execs the selected interpreter; Python reads this header as a string.
    return "#!/bin/sh\n'''exec' " + shlex.quote(sys.executable) + ' "$0" "$@"\n' + "' '''\n"
