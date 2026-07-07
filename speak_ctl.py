#!/usr/bin/env python3
"""
speak_ctl: send a one-shot command to the running speak_daemon over its socket.

Bind to keyboard shortcuts, e.g.:
    .venv/bin/python speak_ctl.py stop      # silence rest of current message
    .venv/bin/python speak_ctl.py toggle    # pause / resume
    .venv/bin/python speak_ctl.py repeat     # re-read last paragraph
    .venv/bin/python speak_ctl.py skip       # jump to next chunk
    .venv/bin/python speak_ctl.py stop_all   # silence everything queued

Exits 0 on success, 1 if the daemon isn't running.
"""

import os
import sys
import json
import socket

SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")

VALID = {"pause", "resume", "toggle", "skip", "stop", "stop_all",
         "repeat", "back", "mute", "unmute", "focus", "ping"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in VALID:
        print(f"usage: {os.path.basename(sys.argv[0])} <{'|'.join(sorted(VALID))}> [sid]",
              file=sys.stderr)
        sys.exit(2)
    msg = {"cmd": sys.argv[1]}
    if len(sys.argv) > 2:
        msg["sid"] = sys.argv[2]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        s.close()
    except (OSError, socket.timeout):
        print("claude-speak daemon not reachable (is it running?)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
