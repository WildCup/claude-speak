# claude-speak — working notes

## Goal

Make Claude Code speak aloud **nicely**, and give me control over **when** and **what** it speaks. When a choice is between "more automatic" and "more controllable", pick controllable.

Personal fork, one user, Ubuntu only. Not a product — no setup wizards, no cross-platform shims, no configuration for situations I don't have.

## The UI is the only control surface

Voice, speed, volume, which session talks, pause/skip/repeat: all of it goes through `speak_ui.py` and the control socket.
A new setting belongs in the settings panel and `~/.claude/claude-speak.json`, reached over the socket. Not a marker file, not an env var, not a web page, not a Claude skill.

The voice in `claude-speak.json` is used for every session and for the clipboard hotkey. There is deliberately no per-project or per-session override.

## Files

| File | Role |
|---|---|
| `speak_daemon.py` | Watches logs, synthesizes, schedules playback, serves the control socket |
| `speak_ui.py` | Tk client — tabs, transport, read-along, settings panel |
| `speak_engine.py` | Text cleaning, chunking, edge-tts; also the clipboard CLI |
| `speak_ctl.py` | One-shot socket command, for hotkeys |
| `setup/` | systemd unit + desktop entry the user copies into place |
| `claude-speak.svg` | The icon. Source of truth: hand-written rects, no generator script. `claude-speak.png` is a 128px Pillow raster of it (this box has no `rsvg-convert` / `inkscape`), also copied to `~/.local/share/icons/` for the desktop entry |

## Shape

```
watcher ─> synth worker ─> per-session queues ─> scheduler ─> mpv
                                                     │
                        socket server <──────────────┘  broadcasts events
```

One speaker, many sessions: only one session is audible at a time, each has its own queue, and background sessions pile up under their own tab.
`speak_daemon.py`'s module docstring has the full playback model — read it before touching the scheduler.

Clients talk to `~/.claude/claude-speak.sock`, one JSON object per line.
The daemon pushes `sessions` and `config` to every client on connect, and broadcasts `play` / `end` / `pause` / `resume` as playback moves.

## Traps

- **The daemon runs under systemd.** `systemctl --user restart claude-speak` after editing `speak_daemon.py`, or the change isn't live.
- **mpv is required for clean pausing.** It's paused over a JSON IPC socket so it corks its audio stream. The `ffplay` path pauses with SIGSTOP, which leaves the sink underrunning and whining, and its volume is fixed at spawn.
- **Only one daemon may run.** `_daemon_is_live()` probes the socket before startup because `_socket_server` unlinks the socket before binding — a second instance would steal it and leave two watchers on the same logs.
- **edge-tts is a network call.** `_render` retries, then falls back to local `espeak-ng`; a silently dropped message is the worst failure mode here.
- **Tk's Listbox fights you.** Its class bindings turn a held button into drag-selection plus an auto-scroll loop that only `<ButtonRelease-1>` cancels, and a window resize can strand that loop running forever. The voice list binds `<B1-Motion>` and `<B1-Leave>` to `"break"` to keep it click-only.
- **The daemon echoes a `config` event after every change.** UI code reacting to it must not rebuild or re-scroll the voice list, or the view jumps under the pointer while picking a voice.

## Platform seams

Linux only. Everything platform-specific lives in three places, and it stays there: `SOCKET_PATH` and the control socket (`AF_UNIX`), the mpv IPC transport (`_MpvProc._command`), and `_spawn_player`.
