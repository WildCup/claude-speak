# claude-speak on Windows

claude-speak runs on Windows with the same daemon, UI and controls as on Linux. Linux behavior doesn't change.

## Platform seams
Each gets a Windows branch next to the Linux one.
1. **Control socket:** the daemon, `speak_ui`, `speak_ctl` and `send_to_daemon` all use `AF_UNIX`. Keep it if Windows Python supports it, otherwise use 127.0.0.1 TCP. The one-JSON-per-line protocol and the `_daemon_is_live()` single-daemon probe keep working.
2. **mpv IPC:** `--input-ipc-server=\\.\pipe\<name>` in `_MpvProc._command`.
3. **Pausing:** Windows has no SIGSTOP/SIGCONT, which the ffplay path and mpv's pause-before-IPC-is-up fallback rely on. Both need a Windows answer, or ffplay isn't supported there.
4. **`_spawn_player`:** no console window per spawn.

## Around the daemon
- Clipboard hotkey without `xsel`.
- Autostart from the Startup folder or Task Scheduler instead of the systemd unit, and a Start-menu shortcut instead of the desktop entry. Both live in `setup/`.
- The local voice engine and its model install and run on Windows.

## Docs
- README: a Windows install section.
- CLAUDE.md: Linux and Windows instead of "Ubuntu only", and the Windows seams under Platform seams.

## Test on Windows
- Pause, skip, repeat, tab switching and read-along behave as on Linux.
- A second daemon start exits and leaves the first one running.
- The clipboard hotkey speaks the selection.
- The daemon is up after login.
