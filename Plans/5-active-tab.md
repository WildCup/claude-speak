# Only the active tab talks

One tab is active at a time and only it speaks. The UI never switches tabs by itself.

## Rules
- **Active tab:** speaks its messages as they arrive. Clicking a tab makes it active, and it speaks what it has waiting unless you paused it.
- **The tab you leave:** stops where it was and continues when you click it again.
- **Other tabs:** never speak. Only the newest message waiting in one is kept, so an interrupted message continues only if nothing newer arrived.
- **Ready:** when a background session finishes its turn (its `~/.claude/sessions` file flips from `busy` to `idle`) with something waiting, its tab gets a marker and a chime plays. Opening the tab clears the marker.
- **Chime:** `/usr/share/sounds/freedesktop/stereo/message.oga` through mpv at the configured volume, held until the paragraph being spoken ends. No setting.
- **Fresh tab:** a tab that opens while no other tab exists becomes active and speaks.
- **Active tab closes:** the tab that was active before it becomes active again, else its left neighbour, else its right one. If that tab has something waiting, it starts paused.
- **Clipboard hotkey:** makes the clipboard tab active and speaks.
- **UI closed:** the active tab stays active, hotkeys act on it, and the other sessions only chime.
- Stop all and Disable work as before.

## Ownership
- `Daemon` owns the active tab and the order tabs were active in. One `active_sid` replaces `focus_sid` and the scheduler's pick of the longest-waiting session.
- `Session` holds `ready`, set from the session file's status.
- The `sessions` event carries the active sid and each tab's `ready`. The `focus` command makes a tab active.
- `SpeakUI.selected` mirrors the daemon's active sid. `follow` and every other place the UI picks a tab on its own go.

## Docs
- The playback model in `speak_daemon`'s module docstring.
- CLAUDE.md, Shape: "One speaker, many sessions" paragraph.

## Test
- One session speaks by itself, as before.
- Two sessions: while you're on one, the other never speaks or takes the tab. It chimes and shows the marker when ready, and clicking it speaks its newest message.
- Clicking back to an interrupted tab continues where it stopped.
- Exiting the active session: the previous tab becomes active, paused if it has something waiting.
- Clipboard hotkey: interrupts, speaks, and when it's done the previous tab is active again.
- After every session has exited, the next one to open speaks by itself.
