# Droid sessions

claude-speak reads Factory Droid conversations the same way as Claude Code ones: each gets its own tab, title and read-along, next to the Claude Code tabs.

## Known
- Droid writes JSONL transcripts under `~/.factory/projects/`, and its Stop hook passes `transcript_path`.
- The line format is unverified.

## Steps
1. Install droid (free tier or bring-your-own-key) and save a transcript sample.
2. Watch `~/.factory/projects/` next to `~/.claude/projects/`, parsing each format: assistant text, message id, timestamp, session label, and the tab title (Claude's is the `ai-title` record).
3. The socket and `claude-speak.json` live in `~/.claude/`, which exists only with Claude Code installed. Move them somewhere that needs neither tool, and update README and CLAUDE.md to the new paths.

## Test
- A Droid session gets a tab with its title and is spoken like a Claude session.
- Claude sessions behave as before.
- With only Droid installed, the daemon, UI and hotkeys work.
