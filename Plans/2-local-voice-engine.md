# Local voices next to edge-tts

Voices from a local engine show up in the voice list next to edge-tts voices. The local engine is optional: without it, claude-speak behaves as before and needs no extra setup.

## Availability
- The daemon checks once at startup whether the local engine and its model load.
- Not available: the voice list holds only edge-tts voices and nothing else changes.

## One voice list
- The `voices` event tags each voice with its engine; the UI marks local voices in the list.
- `voice` in `claude-speak.json` stays the only voice setting. Local voice ids are prefixed (`local:<name>`) so they can't collide with edge-tts ids.
- A configured local voice whose engine isn't available is reported over the `tts` warning, and edge-tts's default voice speaks instead.

## Rendering
`_render` follows the voice's engine:
- **Local voice:** local engine only. No network, no retries.
- **edge-tts voice:** edge-tts with its retries, then the fallback local voice, then espeak if no local engine is available.

The fallback local voice is the last local voice picked, stored as `local_voice` in `claude-speak.json`, or the engine's default voice if none was ever picked.

## Rate, volume, timings
- The configured rate maps to the engine's speed setting. Volume stays on mpv.
- The engine's timings convert to the existing `{t, d, w}` word list. Without word timings, the chunk is highlighted whole.

## UI
- The voice preview works for local voices.
- The `tts` fallback warning names the voice that is speaking (local or espeak).

## Docs
- README: an optional "local voice" install section.
- CLAUDE.md: `_render`'s fallback order in the edge-tts trap.
