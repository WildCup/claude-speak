#!/usr/bin/env python3
"""
speak_engine: text cleaning + edge-tts synthesis, shared by the daemon and the
clipboard hotkey.

As a library, speak_daemon imports clean_text / extract_speakable_chunks /
_chunk_text / play_audio from here.

As a script, it reads text (argument, file or stdin) and hands it to the running
daemon so it gets its own UI tab and transport controls:

    echo "$(xsel -o)" | python speak_engine.py

If the daemon isn't up it synthesizes and plays the text itself.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

SPEAK_SOCKET = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")

# Only used when no daemon is running; otherwise the daemon's own setting wins.
DEFAULT_VOICE = "en-US-AndrewMultilingualNeural"

# ─── Text Cleaning ────────────────────────────────────────────────────────────

# ANSI escape sequences (colors, cursor moves, etc.)
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\x1b[()][AB012]|\x1b\[[\d;]*m")

# Box-drawing and decorative Unicode chars
RE_BOX = re.compile(r"[─━│┃┌┐└┘├┤┬┴┼╭╮╰╯╔╗╚╝╠╣╦╩╬═║▀▄█▌▐░▒▓●○◆◇■□▪▫★☆✓✗✔✘⎿⎡⎣⎤⎦►▶◀◁▷▸▹◂◃]")

# Spinner and progress characters
RE_SPINNER = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣷⣯⣟⡿⢿⣻⣽⣾✻◐◑◒◓⏳⌛🔄]")

# Diff markers at line start
RE_DIFF = re.compile(r"^[+\-]{1,3}(?=\s)", re.MULTILINE)

# Lines that are purely decorative (only special chars and whitespace)
RE_DECORATIVE_LINE = re.compile(r"^[\s─━═╌╍┈┉•·…\-_~*#=+|<>\/\\]+$", re.MULTILINE)

# Tool use / XML-like tags from Claude output
RE_TOOL_TAGS = re.compile(r"</?(?:tool|artifact|function|parameter|result|content|antml)[^>]*>")

# File paths that look like absolute paths (common in Claude Code output)
RE_FILE_PATH = re.compile(r"(?:^|\s)(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){2,}(?:\:\d+)?", re.MULTILINE)

# Windows paths
RE_WIN_PATH = re.compile(r"(?:^|\s)[A-Za-z]:\\(?:[\w.\-]+\\?)+", re.MULTILINE)

# Repeated blank lines
RE_MULTI_BLANK = re.compile(r"\n{3,}")

# Progress percentage patterns
RE_PROGRESS = re.compile(r"\d+%\s*[|█▓▒░\-=>#\[\]]+")

# Token/cost lines
RE_TOKENS = re.compile(r"^\s*[\d,.]+\s*(?:tokens?|tok)\b.*$", re.MULTILINE | re.IGNORECASE)

# Duration/timing lines from Claude Code
RE_TIMING = re.compile(r"^\s*(?:✻\s*)?(?:Worked|Completed|Duration|Elapsed)\s+(?:for\s+)?\d+.*$", re.MULTILINE | re.IGNORECASE)

# Tool invocation lines (Read, Write, Bash, etc.)
RE_TOOL_INVOKE = re.compile(r"^\s*(?:Read|Write|Edit|Bash|Glob|Grep|Task|TodoWrite)\s*\(.*\)\s*$", re.MULTILINE)

# Cost/token summary patterns
RE_COST = re.compile(r"^\s*(?:Cost|Tokens?|Input|Output|Cache)[\s:]+[\d$.,]+.*$", re.MULTILINE | re.IGNORECASE)

# ─── Code Block & Tool Call Filtering ─────────────────────────────────────────

# Fenced code blocks (``` ... ```)
RE_FENCED_CODE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)

# Indented code blocks (4+ spaces, 3+ consecutive lines)
RE_INDENTED_CODE = re.compile(r"(?:^[ \t]{4,}\S.*\n){3,}", re.MULTILINE)

# JSON blocks (tool call outputs) — objects/arrays spanning multiple lines
RE_JSON_BLOCK = re.compile(r"^\s*[\[{][\s\S]*?[\]}]\s*$", re.MULTILINE)

# Tool call output sections (e.g., "Read(...)" followed by indented content)
RE_TOOL_OUTPUT_SECTION = re.compile(
    r"^\s*⎿?\s*(?:Read|Write|Edit|Bash|Glob|Grep|Task|TodoWrite|Search)\s*\(.*\).*(?:\n(?:[ \t]+.*|\s*))*",
    re.MULTILINE,
)

# Standalone URLs (http/https/ftp)
RE_STANDALONE_URL = re.compile(r"(?:^|\s)(?:https?|ftp)://\S+", re.MULTILINE)

# File path lines (lines that are primarily a file path with optional line numbers)
RE_PATH_LINE = re.compile(
    r"^\s*(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){2,}(?::\d+(?::\d+)?)?\s*$", re.MULTILINE
)

# Command output patterns ($ command ... or > command ...)
RE_COMMAND_OUTPUT = re.compile(r"^\s*[$>]\s+\S+.*$", re.MULTILINE)


def filter_non_speech_content(text: str) -> str:
    """Remove code blocks, tool outputs, JSON, file paths, URLs, and command outputs.

    This filter runs BEFORE clean_text() to strip large non-speech blocks that
    would otherwise leave behind noisy residue.
    """
    # Remove fenced code blocks entirely (not just collapse to [code block])
    text = RE_FENCED_CODE.sub("", text)

    # Remove indented code blocks
    text = RE_INDENTED_CODE.sub("", text)

    # Remove tool output sections
    text = RE_TOOL_OUTPUT_SECTION.sub("", text)

    # Remove JSON blocks (multi-line objects/arrays)
    text = RE_JSON_BLOCK.sub("", text)

    # Remove lines that are just file paths
    text = RE_PATH_LINE.sub("", text)

    # Remove standalone URLs
    text = RE_STANDALONE_URL.sub("", text)

    # Remove command output lines
    text = RE_COMMAND_OUTPUT.sub("", text)

    return text


def clean_text(raw: str, skip_code: bool = True, skip_paths: bool = True,
               filter_tool_output: bool = True) -> str:
    """Strip terminal formatting and noise from Claude Code output for natural speech."""
    text = raw

    # Strip ANSI escapes
    text = RE_ANSI.sub("", text)

    # Strip spinner/progress chars
    text = RE_SPINNER.sub("", text)

    # Strip box-drawing chars
    text = RE_BOX.sub(" ", text)

    # Strip tool tags
    text = RE_TOOL_TAGS.sub("", text)

    # Strip progress bars
    text = RE_PROGRESS.sub("", text)

    # Strip token counts, timing lines, costs
    text = RE_TOKENS.sub("", text)
    text = RE_TIMING.sub("", text)
    text = RE_COST.sub("", text)

    # Strip tool invocations
    text = RE_TOOL_INVOKE.sub("", text)

    # Strip diff markers
    text = RE_DIFF.sub("", text)

    # Strip decorative lines
    text = RE_DECORATIVE_LINE.sub("", text)

    # Filter non-speech content (code blocks, tool outputs, JSON, etc.)
    if filter_tool_output:
        text = filter_non_speech_content(text)

    # Optionally strip file paths (they sound awful read aloud)
    if skip_paths:
        text = RE_WIN_PATH.sub(" ", text)
        text = RE_FILE_PATH.sub(" ", text)

    # Optionally collapse remaining code blocks
    if skip_code:
        # Fenced code blocks (```...```) — greedy match between fences
        text = re.sub(
            r"```[^\n]*\n.*?```",
            "\n[code block]\n",
            text,
            flags=re.DOTALL,
        )
        # Indented code blocks (4+ spaces, 3+ consecutive lines)
        text = re.sub(
            r"(?:^[ \t]{4,}\S.*\n){3,}",
            "[code block]\n",
            text,
            flags=re.MULTILINE,
        )
        # Inline backtick code (replace with just the content, no backticks)
        text = re.sub(r"`([^`]+)`", r"\1", text)

    # --- Markdown and formatting cleanup for natural speech ---

    # Markdown links [text](url) -> just the text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Markdown images ![alt](url) -> remove entirely
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)

    # Markdown bold/italic: **text**, __text__, *text*, _text_
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(\S[^_]*\S)_{1,3}", r"\1", text)

    # Markdown headers (# Header) -> just the text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Markdown horizontal rules
    text = re.sub(r"^[\-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Markdown bullet points: - item, * item -> just the text
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)

    # Numbered lists: 1. item -> just the text
    text = re.sub(r"^[\s]*\d+[.)]\s+", "", text, flags=re.MULTILINE)

    # HTML tags that might appear
    text = re.sub(r"<[^>]+>", "", text)

    # URLs (standalone) -> skip them
    text = re.sub(r"https?://\S+", "", text)

    # Arrow characters -> natural words
    text = text.replace("\u2192", " to ")
    text = text.replace("\u2190", " from ")
    text = text.replace("=>", " to ")
    text = text.replace("->", " to ")
    text = text.replace(">>", " ")
    text = text.replace("<<", " ")

    # Common symbols that get read literally
    text = text.replace("&amp;", " and ")
    text = text.replace("&", " and ")
    text = text.replace("|", ". ")
    text = text.replace("@", " at ")
    text = text.replace("~", " ")
    text = text.replace("≈", "is around")
    text = text.replace("²", "squared")
    text = text.replace("³", "cubed")

    # Underscores and slashes are silent (e.g. _someField -> someField, src/foo -> src foo)
    text = text.replace("_", " ")
    text = text.replace("/", " ")

    # Dots in qualified names (e.g., "item.image_url") -> spaces
    # But preserve decimal numbers, ellipsis, and abbreviations (Dr., U.S.A., etc.)
    # Only replace dots between lowercase identifier segments (not after uppercase/digits)
    text = re.sub(r"(?<=[a-z])\.(?=[a-z])", " ", text)

    # Parenthetical references like (line 42) or (file.php:123) - keep meaningful ones
    text = re.sub(r"\([^)]*\.\w+:\d+\)", "", text)

    # Strip standalone special chars: $, ^, ~, `, \
    # But be careful not to strip $ before digits (e.g. "$5") or ^ in math
    text = re.sub(r"(?<!\w)[\\$^`~](?!\w)", " ", text)

    # Curly braces, square brackets (outside of already-handled markdown)
    text = re.sub(r"[{}\[\]]", " ", text)

    # Multiple punctuation (... is ok, but ---- or ==== etc.)
    # Preserve plus signs so "C++", "g++", etc. stay intact
    text = re.sub(r"([=\-_]){2,}", " ", text)

    # Collapse multiple spaces
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Collapse all blank lines to single newline (reduces TTS pauses)
    text = re.sub(r"\n\s*\n", "\n", text)

    # Strip leading/trailing whitespace per line
    text = "\n".join(line.strip() for line in text.splitlines())

    # Final trim
    text = text.strip()

    return text


# ─── TTS Backends ─────────────────────────────────────────────────────────────


async def tts_edge_async(text: str, voice: str, rate: str, output_path: str,
                         volume: int = 100) -> str:
    """Generate speech using edge-tts (free)."""
    try:
        import edge_tts
    except ImportError:
        print("ERROR: edge-tts not installed. Run: pip install edge-tts", file=sys.stderr)
        sys.exit(1)

    # Convert volume 0-100 to edge-tts volume string (-100% to +0%)
    # edge-tts volume: -100% (silent) to +0% (full volume)
    volume_str = f"{volume - 100}%" if volume < 100 else "+0%"

    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume_str)
        await communicate.save(output_path)
    except Exception as e:
        err = str(e)
        if "name resolution" in err or "connect" in err.lower():
            print("ERROR: Cannot reach Microsoft TTS service. Check internet connection.", file=sys.stderr)
        else:
            print(f"ERROR: edge-tts failed: {e}", file=sys.stderr)
        return None

    return output_path


def tts_edge(text: str, voice: str, rate: str, output_path: str,
             volume: int = 100, loop: asyncio.AbstractEventLoop = None) -> str:
    """Sync wrapper for edge-tts.

    When called from a worker thread, pass a dedicated event loop to avoid
    conflicts with asyncio.run() which cannot be nested or called when another
    loop is already running.
    """
    if loop is not None:
        return loop.run_until_complete(
            tts_edge_async(text, voice, rate, output_path, volume=volume)
        )
    return asyncio.run(tts_edge_async(text, voice, rate, output_path, volume=volume))




def _chunk_text(text: str, max_chars: int) -> list:
    """Split text into chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = ""

    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text[:max_chars]]


# ─── Audio Playback ───────────────────────────────────────────────────────────


def _validate_audio_file(path: str) -> bool:
    """Verify an audio file exists and has non-zero size."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def play_audio(path: str, blocking: bool = True, volume: int = 100):
    """Play an audio file with no visible window.

    Only used as a last resort: the daemon drives mpv itself so it can pause and
    seek, and falls back here when no controllable player exists.
    """
    if not _validate_audio_file(path):
        print(f"WARNING: Audio file missing or empty: {path}", file=sys.stderr)
        return False

    vol = max(0, min(100, volume))
    players = [
        ["mpv", "--no-video", "--no-terminal", "--really-quiet",
         f"--volume={vol}", path],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
         "-volume", str(vol), path],
    ]

    for cmd in players:
        if shutil.which(cmd[0]):
            try:
                if blocking:
                    subprocess.run(cmd, check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except subprocess.CalledProcessError:
                continue

    print(f"WARNING: No audio player found. Audio saved to: {path}", file=sys.stderr)
    return False


# ─── Chunking ─────────────────────────────────────────────────────────────────




def extract_speakable_chunks(text: str) -> list:
    """Extract speakable chunks from text, splitting at natural boundaries."""
    # Split on paragraph boundaries (double newlines) or sentence endings
    chunks = []

    # First split on paragraphs
    paragraphs = re.split(r'\n\s*\n', text)

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If paragraph is short enough, use as-is
        if len(para) < 500:
            chunks.append(para)
        else:
            # Split long paragraphs into sentences
            sentences = re.split(r'(?<=[.!?])\s+', para)
            current = ""
            for sent in sentences:
                if len(current) + len(sent) < 400:
                    current = f"{current} {sent}".strip()
                else:
                    if current:
                        chunks.append(current)
                    current = sent
            if current:
                chunks.append(current)

    return chunks






# ─── Main ─────────────────────────────────────────────────────────────────────


def send_to_daemon(text, label=None):
    """Hand text to a running speak_daemon so it speaks under its own UI tab, with
    the transport controls and read-along highlighting. False if no daemon is up,
    in which case the caller should synthesize locally as usual."""
    if not hasattr(socket, "AF_UNIX") or not os.path.exists(SPEAK_SOCKET):
        return False
    msg = {"cmd": "speak", "text": text}
    if label:
        msg["label"] = label
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(SPEAK_SOCKET)
        s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        s.close()
        return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(
        description="Clean text and speak it through the claude-speak daemon.")
    ap.add_argument("text", nargs="?", help="Text to read (default: stdin)")
    ap.add_argument("--file", "-f", help="Read the text from this file instead")
    ap.add_argument("--rate", "-r", default="+10%",
                    help="Rate for local playback when no daemon is running")
    ap.add_argument("--volume", "-V", type=int, default=100)
    ap.add_argument("--label", default="clipboard",
                    help="Tab name to use in the UI")
    ap.add_argument("--keep-code", action="store_true")
    ap.add_argument("--keep-paths", action="store_true")
    ap.add_argument("--raw", action="store_true", help="Skip all text cleaning")
    ap.add_argument("--preview", action="store_true",
                    help="Print the cleaned text instead of speaking it")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.text:
        raw = args.text
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        ap.error("no input: pass text, use --file, or pipe on stdin")

    text = raw if args.raw else clean_text(raw, skip_code=not args.keep_code,
                                           skip_paths=not args.keep_paths)
    if not text.strip():
        print("nothing readable after cleaning", file=sys.stderr)
        return

    if args.preview:
        print(text)
        return

    if send_to_daemon(text, args.label):
        return

    # No daemon: synthesize and play it here so the hotkey still does something.
    print("daemon not running; playing locally", file=sys.stderr)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        if tts_edge(text, DEFAULT_VOICE, args.rate, tmp.name, volume=args.volume):
            play_audio(tmp.name, volume=args.volume)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    main()
