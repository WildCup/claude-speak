#!/usr/bin/env python3
"""
speak_daemon: watch all Claude Code conversation logs and speak assistant output,
with a Unix-socket control channel for per-session pause / resume / skip / stop /
repeat, and word-level timing streamed to the UI for live highlighting.

Architecture:
    watcher thread   -> discovers JSONL files, tails them, enqueues Utterances
    synth worker     -> Utterance -> edge-tts mp3 + WordBoundary timings -> PlayItem,
                        routed into the owning session's own play queue
    scheduler thread -> plays one session at a time (the "active" session), with
                        per-session pause, skip, stop, repeat
    socket server    -> accepts UI/CLI clients, dispatches commands, broadcasts events

Playback model (one speaker, many sessions):
    Only one session is audible at a time (you can't listen to two at once). Each
    session has its own queue, so background sessions accumulate under their own tab
    instead of interrupting what you're hearing. When the speaker is free, the
    longest-waiting session auto-plays. Pausing a session holds the speaker for it;
    pressing Play on another tab switches the speaker to that session and pauses the
    first exactly where it was.
"""

import os
import sys
import json
import glob
import time
import queue
import shutil
import signal
import socket
import atexit
import asyncio
import hashlib
import tempfile
import itertools
import threading
import subprocess
import collections
from datetime import datetime, timezone

# ─── Engine import (cc-speak.py has a hyphen, can't be imported normally) ──────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from importlib.util import spec_from_file_location, module_from_spec

_cc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc-speak.py")
_spec = spec_from_file_location("cc_speak", _cc_path)
cc_speak = module_from_spec(_spec)
_spec.loader.exec_module(cc_speak)

import edge_tts  # noqa: E402  (used directly for WordBoundary timings)

CLAUDE_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")

# Only actively tail files touched within this window (keeps stat load bounded).
ACTIVE_WINDOW_SEC = 2 * 60 * 60
POLL_SEC = 1.0
RESCAN_SEC = 3.0
TICK_SEC = 0.04

# edge-tts is a network call and rejects very long inputs. Retry transient
# failures, then fall back to a local voice so a message is never silently lost.
MAX_SYNTH_CHARS = 3000
SYNTH_ATTEMPTS = 3
SYNTH_RETRY_SEC = 0.6


def now_ms():
    return time.monotonic() * 1000.0


def _hard_split(text, limit):
    """Split on whitespace so no piece exceeds `limit` chars."""
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > limit:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
        while len(cur) > limit:          # a single token longer than the limit
            out.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        out.append(cur)
    return out


def cap_chunks(chunks):
    """Backstop for chunks edge-tts would choke on.

    extract_speakable_chunks already targets ~400-500 chars, but it splits long
    paragraphs on sentence boundaries only — an unpunctuated wall of text or a
    huge URL comes back as one oversized chunk and fails synthesis.
    """
    out = []
    for c in chunks:
        if len(c) <= MAX_SYNTH_CHARS:
            out.append(c)
            continue
        for piece in cc_speak._chunk_text(c, MAX_SYNTH_CHARS):
            if len(piece) <= MAX_SYNTH_CHARS:
                out.append(piece)
            else:
                out.extend(_hard_split(piece, MAX_SYNTH_CHARS))
    return out


def parse_ts(s):
    """Parse a Claude JSONL ISO timestamp to epoch seconds, or None."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


# ─── Pipeline channel: ordered, thread-safe, filterable ────────────────────────


class Channel:
    """A deque guarded by a Condition. Supports filtered removal (for stop/skip)."""

    def __init__(self):
        self._dq = collections.deque()
        self._cv = threading.Condition()
        self._closed = False

    def put(self, item):
        with self._cv:
            self._dq.append(item)
            self._cv.notify()

    def put_front(self, item):
        with self._cv:
            self._dq.appendleft(item)
            self._cv.notify()

    def get(self, timeout=0.5):
        with self._cv:
            if not self._dq and not self._closed:
                self._cv.wait(timeout)
            if self._dq:
                return self._dq.popleft()
            return None

    def drop_where(self, pred):
        """Remove all queued items matching pred. Returns how many were dropped."""
        with self._cv:
            kept = [x for x in self._dq if not pred(x)]
            dropped = len(self._dq) - len(kept)
            self._dq.clear()
            self._dq.extend(kept)
            return dropped

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class Utterance:
    __slots__ = ("session", "msg_id", "seq", "text", "full", "gen", "tok", "idx")

    def __init__(self, session, msg_id, seq, text, full, gen, tok, idx):
        self.session = session
        self.msg_id = msg_id
        self.seq = seq
        self.text = text        # this chunk
        self.full = full        # whole message text (for restart)
        self.gen = gen          # global generation (stop_all)
        self.tok = tok          # per-session token (per-session stop/replay)
        self.idx = idx          # this chunk's position within the message


class PlayItem:
    __slots__ = ("session", "msg_id", "seq", "text", "full", "path", "words",
                 "gen", "idx")

    def __init__(self, session, msg_id, seq, text, full, path, words, gen, idx):
        self.session = session
        self.msg_id = msg_id
        self.seq = seq
        self.text = text
        self.full = full
        self.path = path
        self.words = words
        self.gen = gen
        self.idx = idx


# ─── Session bookkeeping ───────────────────────────────────────────────────────


class Session:
    def __init__(self, sid, label, path):
        self.sid = sid              # stable id (sessionId or file stem)
        self.label = label          # human label (basename of cwd)
        self.path = path            # jsonl path
        self.config_dir = os.path.dirname(path)
        self.file_pos = 0
        self.identity = None        # inode, to detect file replacement
        self.last_active = 0.0

        # ─ per-session playback state (guarded by Daemon.play_lock) ─
        self.q = collections.deque()     # PlayItems waiting to play
        self.cur = None                  # PlayItem currently on the speaker
        self.proc = None                 # its player process (_MpvProc / _SignalProc)
        self.player_paused = False       # is the player itself currently paused?
        self.paused = False              # user paused this session
        self.inflight = 0                # utterances queued to synth, not yet in q
        self.gen_token = 0               # bumped by this session's stop/replay
        self.pending_since = 0.0         # when this session first had work waiting
        self.playing_msg_id = None       # msg_id of the message on the speaker now
        self.played = False              # has this session ever spoken a chunk?
        self.disabled = False            # muted for ALL future output until re-enabled
        self.order_seq = 0               # stable tab order (discovery order)
        self.state = "idle"              # idle | playing | paused | ended | disabled
        self.adhoc = False               # ad-hoc tab (piped text), not a jsonl file
        self.voice = None                # per-session voice override

        # current message, for paragraph navigation (Repeat) and whole (Restart)
        self.msg_chunks = []             # ordered chunk texts of the current message
        self.msg_clean = []              # same chunks, cleaned — what the UI displays
        self.msg_full = None             # its full text
        self.cur_idx = 0                 # chunk index currently playing / last played
        self.cur_started = 0.0           # monotonic time the current chunk began

    def can_replay(self):
        return self.played and bool(self.msg_chunks)

    def has_work(self):
        return self.cur is not None or bool(self.q) or self.inflight > 0


# ─── The daemon ────────────────────────────────────────────────────────────────


class Daemon:
    def __init__(self, voice="en-US-GuyNeural", rate="+10%", debounce_ms=1500):
        self.voice = voice
        self.rate = rate
        self.debounce_ms = debounce_ms
        self.running = True
        self.start_epoch = time.time()

        self.synth_chan = Channel()
        self.temp_dir = tempfile.mkdtemp(prefix="claude_speak_")
        self.file_counter = 0
        self.seq_counter = 0
        self.order_counter = 0          # assigns each session a stable display order

        # dedup of spoken message ids (bounded)
        self._spoken = collections.OrderedDict()
        self._spoken_max = 4000

        # One reentrant lock guards both the sessions dict and all playback state
        # (queues / procs / active_sid). Using a single lock keeps ordering trivial
        # and deadlock-free; it is only ever held for quick, non-blocking work.
        self.play_lock = threading.RLock()
        self.sessions_lock = self.play_lock

        # sessions keyed by jsonl path
        self.sessions = {}
        self.closed_paths = set()   # tabs the user closed; never re-register them
        self.focus_sid = None       # tab the UI currently shows (for repeat default)
        self.active_sid = None      # session that currently owns the speaker

        # cancellation: a message stopped mid-flight, or a global flush
        self.generation = 0                 # bumped by stop_all
        self.cancelled = collections.OrderedDict()   # cancelled msg_ids (bounded)
        self.cancel_lock = threading.Lock()

        # debounce accumulator, per session path
        self.pending = {}                   # path -> {"text":..., "msg_id":..., "t":...}
        self.pending_lock = threading.Lock()

        # connected clients for event broadcast
        self.clients = []
        self.clients_lock = threading.Lock()

        atexit.register(self._cleanup)

        self._threads = [
            threading.Thread(target=self._synth_worker, daemon=True),
            threading.Thread(target=self._scheduler, daemon=True),
            threading.Thread(target=self._debounce_flusher, daemon=True),
            threading.Thread(target=self._watcher, daemon=True),
            threading.Thread(target=self._socket_server, daemon=True),
        ]
        for t in self._threads:
            t.start()

    # ─── helpers ──────────────────────────────────────────────────────────────

    def _cleanup(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
        except OSError:
            pass

    def _record_spoken(self, msg_id):
        self._spoken[msg_id] = True
        if len(self._spoken) > self._spoken_max:
            for _ in range(self._spoken_max // 2):
                self._spoken.popitem(last=False)

    def _is_cancelled(self, item):
        """True if this item belongs to a stopped message or a flushed generation."""
        if item.gen < self.generation:
            return True
        with self.cancel_lock:
            return item.msg_id in self.cancelled

    def _mark_cancelled(self, msg_id):
        with self.cancel_lock:
            self.cancelled[msg_id] = True
            while len(self.cancelled) > 256:
                self.cancelled.popitem(last=False)

    def _session_by_sid(self, sid):
        with self.sessions_lock:
            for s in self.sessions.values():
                if s.sid == sid:
                    return s
        return None

    # ─── event broadcast ────────────────────────────────────────────────────────

    def broadcast(self, obj):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self.clients_lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(line)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)

    def emit_sessions(self):
        with self.sessions_lock:
            sessions = list(self.sessions.values())
        with self.play_lock:
            payload = [
                {
                    "sid": s.sid,
                    "label": s.label,
                    "state": s.state,
                    "disabled": s.disabled,
                    "can_replay": s.can_replay(),
                    "has_work": s.has_work(),
                    "active": s.sid == self.active_sid,
                    "last_active": s.last_active,
                }
                for s in sorted(sessions, key=lambda s: s.order_seq)   # stable order
            ]
        self.broadcast({"ev": "sessions", "sessions": payload})

    def _set_state(self, sess, state):
        if sess.state != state:
            sess.state = state
            self.emit_sessions()

    # ─── watcher ────────────────────────────────────────────────────────────────

    def _discover(self):
        """Return jsonl paths modified within the active window."""
        out = []
        cutoff = time.time() - ACTIVE_WINDOW_SEC
        try:
            for d in os.listdir(CLAUDE_PROJECTS_DIR):
                dp = os.path.join(CLAUDE_PROJECTS_DIR, d)
                if not os.path.isdir(dp):
                    continue
                for f in glob.glob(os.path.join(dp, "*.jsonl")):
                    try:
                        if os.path.getmtime(f) >= cutoff:
                            out.append(f)
                    except OSError:
                        continue
        except OSError:
            pass
        return out

    def _register(self, path):
        """Create a Session for a newly-seen file, starting at end-of-file."""
        sid = os.path.splitext(os.path.basename(path))[0]
        label = self._label_for(path)
        sess = Session(sid, label, path)
        self.order_counter += 1
        sess.order_seq = self.order_counter
        try:
            sess.file_pos = os.path.getsize(path)
            sess.identity = os.stat(path).st_ino
        except OSError:
            sess.file_pos = 0
        self.sessions[path] = sess
        return sess

    def _label_for(self, path):
        """Best-effort human label: basename of the cwd recorded in the file."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        cwd = json.loads(line).get("cwd")
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if cwd:
                        return os.path.basename(cwd.rstrip("/")) or cwd
        except OSError:
            pass
        return os.path.basename(os.path.dirname(path))

    def _watcher(self):
        last_rescan = 0.0
        while self.running:
            # A single bad iteration (a file vanishing mid-stat, a transient read
            # error) must never kill discovery permanently: log and keep looping.
            try:
                now = time.time()
                if now - last_rescan > RESCAN_SEC:
                    last_rescan = now
                    found_new = False
                    for path in self._discover():
                        with self.sessions_lock:
                            if path not in self.sessions and path not in self.closed_paths:
                                self._register(path)
                                found_new = True
                    changed_prune = self._prune()
                    if found_new or changed_prune:
                        self.emit_sessions()

                with self.sessions_lock:
                    sessions = list(self.sessions.values())

                changed = False
                for sess in sessions:
                    changed |= self._tail(sess)
                if changed:
                    self.emit_sessions()
            except Exception as e:
                print(f"watcher error: {e}", file=sys.stderr)

            time.sleep(POLL_SEC)

    def _prune(self):
        """Drop sessions whose file is gone or has gone quiet, so the UI's tab list
        tracks live conversations. Never drops one that's busy (playing / paused /
        queued) or currently focused/active. Returns True if anything was removed."""
        cutoff = time.time() - ACTIVE_WINDOW_SEC
        removed = False
        with self.play_lock:
            for path, sess in list(self.sessions.items()):
                if sess.adhoc:
                    continue        # ad-hoc tabs live until closed by hand
                gone = False
                stale = False
                try:
                    stale = os.path.getmtime(path) < cutoff
                except OSError:
                    gone = True
                busy = (sess.has_work() or sess.state in ("playing", "paused")
                        or sess.disabled or sess.sid == self.active_sid)
                # a deleted file is definitely dead — prune even if it's the focus;
                # a merely-quiet (stale) file stays if the UI is focused on it.
                if not gone and sess.sid == self.focus_sid:
                    busy = True
                if (gone or stale) and not busy:
                    self._drain_session(sess)
                    self.sessions.pop(path, None)
                    if self.focus_sid == sess.sid:
                        self.focus_sid = None
                    removed = True
        return removed

    def _tail(self, sess):
        """Read new lines from one session file. Returns True if it became active."""
        if sess.adhoc:
            return False            # no file behind it; its text arrives over the socket
        try:
            size = os.path.getsize(sess.path)
            ident = os.stat(sess.path).st_ino
        except OSError:
            return False

        if sess.identity is not None and ident != sess.identity:
            sess.file_pos = 0           # file replaced
            sess.identity = ident
        if size < sess.file_pos:
            sess.file_pos = size        # truncated
        if size <= sess.file_pos:
            return False

        try:
            with open(sess.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(sess.file_pos)
                data = f.read()
                sess.file_pos = f.tell()
        except OSError:
            return False

        became_active = False
        for line in data.split("\n"):
            line = line.strip()
            if not line:
                continue
            text, msg_id, ts = self._extract(line)
            if not text:
                continue
            if ts is not None and ts < self.start_epoch:
                continue                # older than daemon start: never speak
            if msg_id in self._spoken:
                continue
            self._record_spoken(msg_id)
            sess.last_active = time.time()
            became_active = True
            with self.pending_lock:
                p = self.pending.setdefault(
                    sess.path, {"text": "", "msg_id": msg_id, "t": 0.0})
                p["text"] += " " + text
                p["msg_id"] = msg_id
                p["t"] = now_ms()
        if became_active and self.focus_sid is None:
            self.focus_sid = sess.sid
        return became_active

    @staticmethod
    def _extract(line):
        """Return (text, msg_id, epoch_ts) for an assistant text message."""
        try:
            data = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            return None, None, None
        if data.get("type") != "assistant":
            return None, None, None
        msg = data.get("message", {})
        texts = [
            b.get("text", "")
            for b in msg.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
        ]
        if not texts:
            return None, None, None
        combined = " ".join(texts)
        msg_id = msg.get("id") or data.get("uuid") or (
            "_h_" + hashlib.sha256(combined.encode()).hexdigest()[:16])
        return combined, msg_id, parse_ts(data.get("timestamp"))

    # ─── debounce: accumulate a message, then split into speakable chunks ──────

    def _debounce_flusher(self):
        while self.running:
            time.sleep(0.1)
            try:
                ready = []
                with self.pending_lock:
                    for path, p in list(self.pending.items()):
                        if p["text"] and now_ms() - p["t"] > self.debounce_ms:
                            ready.append((path, p["text"], p["msg_id"]))
                            del self.pending[path]
                for path, text, msg_id in ready:
                    with self.sessions_lock:
                        sess = self.sessions.get(path)
                    if not sess:
                        continue
                    self._enqueue_message(sess, msg_id, text)
            except Exception as e:
                print(f"debounce error: {e}", file=sys.stderr)

    def _enqueue_message(self, sess, msg_id, full_text):
        """Split a full message into chunks and hand them to the synth worker."""
        if sess.disabled:
            return                      # session is muted for all output until enabled
        chunks = cap_chunks(cc_speak.extract_speakable_chunks(full_text))
        if not chunks:
            return
        with self.play_lock:
            sess.msg_chunks = chunks       # remembered for Repeat / Previous / Restart
            # the UI shows these while audio for a seek target is still synthesizing,
            # so they must match what _synth_worker will eventually speak
            sess.msg_clean = [cc_speak.clean_text(c, skip_code=True, skip_paths=True)
                              for c in chunks]
            sess.msg_full = full_text
        self._enqueue_chunks(sess, msg_id, chunks, full_text, 0)

    def _enqueue_chunks(self, sess, msg_id, chunks, full_text, start_idx):
        """Queue chunks[start_idx:] for synthesis, preserving their message index."""
        for i in range(start_idx, len(chunks)):
            self.seq_counter += 1
            with self.play_lock:
                sess.inflight += 1
                tok = sess.gen_token
            self.synth_chan.put(
                Utterance(sess.sid, msg_id, self.seq_counter, chunks[i], full_text,
                          self.generation, tok, i))

    # ─── synth worker ───────────────────────────────────────────────────────────

    def _synth_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while self.running:
            u = self.synth_chan.get()
            if u is None:
                continue
            sess = self._session_by_sid(u.session)
            try:
                if self._is_cancelled(u):
                    continue
                cleaned = cc_speak.clean_text(u.text, skip_code=True, skip_paths=True)
                if not cleaned.strip() or len(cleaned.split()) < 3:
                    continue
                self.file_counter += 1
                out = os.path.join(self.temp_dir, f"s{self.file_counter}.mp3")
                voice = self._voice_for(sess)
                out, words = self._render(loop, cleaned, voice, out)
                if self._is_cancelled(u):       # stopped while we were synthesizing
                    self._safe_remove(out)
                    continue
                if out and sess is not None:
                    item = PlayItem(u.session, u.msg_id, u.seq, cleaned, u.full,
                                    out, words, u.gen, u.idx)
                    with self.play_lock:
                        if u.tok != sess.gen_token:     # session stopped/replayed since
                            self._safe_remove(out)
                        else:
                            if not sess.q and sess.cur is None:
                                sess.pending_since = time.time()
                            sess.q.append(item)
            except Exception as e:
                print(f"synth error: {e}", file=sys.stderr)
            finally:
                if sess is not None:
                    with self.play_lock:
                        sess.inflight = max(0, sess.inflight - 1)

    def _render(self, loop, text, voice, out_path):
        """Synthesize `text` to an audio file, returning (path, word_timings).

        edge-tts is a network call, so transient failures are retried before
        falling back to a local voice — otherwise a dropped request means the
        message is silently never spoken. Returns (None, []) if nothing rendered.
        """
        for attempt in range(SYNTH_ATTEMPTS):
            if not self.running:
                return None, []
            try:
                words = loop.run_until_complete(
                    self._synth(text, voice, self.rate, out_path))
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path, words
                raise RuntimeError("edge-tts returned no audio")
            except Exception as e:
                self._safe_remove(out_path)
                if attempt == SYNTH_ATTEMPTS - 1:
                    print(f"synth failed after {SYNTH_ATTEMPTS} attempts: {e}",
                          file=sys.stderr)
                else:
                    time.sleep(SYNTH_RETRY_SEC)

        wav = self._synth_local(text, out_path)
        if wav:
            print("edge-tts unavailable; spoke with local fallback voice",
                  file=sys.stderr)
            return wav, []       # no timings, so the UI highlights the chunk whole
        return None, []

    def _synth_local(self, text, out_path):
        """Render with an offline TTS binary. Returns the wav path, or None."""
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if not espeak:
            return None
        wav = os.path.splitext(out_path)[0] + ".wav"
        try:
            subprocess.run([espeak, "-w", wav, text],
                           check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError):
            self._safe_remove(wav)
            return None
        if os.path.exists(wav) and os.path.getsize(wav) > 0:
            return wav
        self._safe_remove(wav)
        return None

    async def _synth(self, text, voice, rate, out_path):
        """edge-tts streaming: write audio + collect word timings (ms)."""
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        words = []
        with open(out_path, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    words.append({
                        "t": chunk["offset"] / 10000.0,      # 100ns ticks -> ms
                        "d": chunk["duration"] / 10000.0,
                        "w": chunk["text"],
                    })
        return words

    def _voice_for(self, sess):
        if sess and sess.voice:
            return sess.voice
        if sess:
            vf = os.path.join(sess.config_dir, "speech-voice")
            if os.path.exists(vf):
                try:
                    with open(vf) as f:
                        v = f.read().strip()
                        if v:
                            return v
                except OSError:
                    pass
        gv = os.path.join(os.path.expanduser("~"), ".claude", "speech-voice")
        if os.path.exists(gv):
            try:
                with open(gv) as f:
                    v = f.read().strip()
                    if v:
                        return v
            except OSError:
                pass
        return self.voice

    # ─── scheduler: plays one session (the active one) at a time ────────────────

    def _scheduler(self):
        while self.running:
            try:
                with self.play_lock:
                    self._schedule_tick()
            except Exception as e:
                print(f"scheduler error: {e}", file=sys.stderr)
            time.sleep(TICK_SEC)

    def _pick_active(self):
        """Return the session that should own the speaker right now (or None)."""
        with self.sessions_lock:
            sessions = list(self.sessions.values())
        by_sid = {s.sid: s for s in sessions}

        if self.active_sid:
            s = by_sid.get(self.active_sid)
            # keep the speaker only while the active session is live and unblocked;
            # a paused or disabled session RELEASES it so other tabs can play.
            if s and not s.paused and not s.disabled \
                    and (s.cur is not None or s.q or s.inflight > 0):
                return s
            if s is not None and s.state == "playing" \
                    and not (s.cur is not None or s.q or s.inflight > 0):
                self._set_state(s, "ended")     # drained: nothing left to say
            self.active_sid = None

        # speaker is free: auto-play the non-paused, non-disabled session that has
        # waited longest
        cand = [s for s in sessions
                if not s.paused and not s.disabled and (s.cur is not None or s.q)]
        if not cand:
            return None
        cand.sort(key=lambda s: s.pending_since)
        self.active_sid = cand[0].sid
        return cand[0]

    def _schedule_tick(self):
        sess = self._pick_active()
        if sess is None:
            return

        # need a current chunk?
        if sess.cur is None:
            if sess.q:
                item = sess.q.popleft()
                if self._is_cancelled(item):
                    self._safe_remove(item.path)
                    return
                sess.cur = item
                sess.proc = None
                sess.player_paused = False
            else:
                # nothing left to play for this session right now
                if sess.inflight == 0 and sess.state == "playing":
                    self._set_state(sess, "ended")
                    if self.active_sid == sess.sid and not sess.paused:
                        self.active_sid = None
                return

        # have a current chunk: launch it if not started
        if sess.proc is None:
            if sess.paused:
                return                          # don't start audio while paused
            self._start_proc(sess)
            return

        # proc exists: manage pause/resume and completion
        rc = sess.proc.poll()
        if rc is None:
            if sess.paused and not sess.player_paused:
                sess.proc.pause()
                sess.player_paused = True
            elif not sess.paused and sess.player_paused:
                sess.proc.resume()
                sess.player_paused = False
            return

        # chunk finished
        self._safe_remove(sess.cur.path)
        sess.proc.kill()                # already exited; this reaps its IPC socket
        self.broadcast({"ev": "end", "sid": sess.sid, "seq": sess.cur.seq})
        sess.cur = None
        sess.proc = None
        sess.player_paused = False

    def _start_proc(self, sess):
        item = sess.cur
        sess.playing_msg_id = item.msg_id
        sess.cur_idx = item.idx                  # where we are in the message
        sess.cur_started = time.monotonic()
        sess.played = True
        proc = self._spawn_player(item.path)
        sess.proc = proc
        sess.player_paused = False
        self._set_state(sess, "playing")
        self.broadcast({
            "ev": "play",
            "sid": sess.sid,
            "msg_id": item.msg_id,
            "seq": item.seq,
            "idx": item.idx,
            "text": item.text,
            "words": item.words,
            "chunks": list(sess.msg_clean),   # lets the UI jump paragraphs instantly
            "epoch_ms": now_ms(),
        })

    def _spawn_player(self, path):
        mpv = shutil.which("mpv")
        if mpv:
            sock = _ipc_socket_path()
            try:
                proc = subprocess.Popen(
                    [mpv, "--no-video", "--no-terminal", "--really-quiet",
                     f"--input-ipc-server={sock}", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return _MpvProc(proc, sock)
            except OSError:
                pass

        ffplay = shutil.which("ffplay")
        if ffplay:
            try:
                return _SignalProc(subprocess.Popen(
                    [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except OSError:
                pass

        # no controllable player: fall back to a blocking play (rare)
        threading.Thread(target=cc_speak.play_audio, args=(path,),
                         daemon=True).start()
        return _DummyProc()

    def _kill(self, proc):
        if proc is None:
            return
        proc.kill()

    @staticmethod
    def _safe_remove(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ─── transport commands (all per-session) ───────────────────────────────────

    def cmd_pause(self, sid=None):
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=True)
            if not sess:
                return
            sess.paused = True
            if sess.proc is not None and not sess.player_paused:
                sess.proc.pause()
                sess.player_paused = True
            self._set_state(sess, "paused")
        self.broadcast({"ev": "pause", "sid": sess.sid})

    def cmd_resume(self, sid=None):
        """Resume / play the given session, making it the active speaker."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=True)
            if not sess:
                return
            # switching speaker: pause whoever holds it now
            if self.active_sid and self.active_sid != sess.sid:
                other = self._session_by_sid(self.active_sid)
                if other and other.proc is not None and not other.player_paused:
                    other.paused = True
                    other.proc.pause()
                    other.player_paused = True
                    self._set_state(other, "paused")
            self.active_sid = sess.sid
            sess.paused = False
            if sess.proc is not None and sess.player_paused:
                sess.proc.resume()
                sess.player_paused = False
            if sess.cur is not None or sess.q or sess.inflight > 0:
                self._set_state(sess, "playing")
        self.broadcast({"ev": "resume", "sid": sess.sid})

    def cmd_toggle(self, sid=None):
        sess = self._resolve(sid, prefer_active=True)
        if not sess:
            return
        if sess.paused or sess.state != "playing":
            self.cmd_resume(sess.sid)
        else:
            self.cmd_pause(sess.sid)

    def cmd_skip(self, sid=None):
        """Stop the current chunk only; the next queued chunk plays."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=True)
            if not sess or sess.proc is None:
                return
            self._kill(sess.proc)
            # the scheduler will observe the finished proc and advance

    def cmd_stop(self, sid=None):
        """Silence a whole session: kill its player and drop its queue + backlog."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=True)
            if not sess:
                return
            sess.gen_token += 1             # invalidate this session's in-flight synth
            if sess.cur is not None:
                self._mark_cancelled(sess.cur.msg_id)
            self._drain_session(sess)
            self._kill(sess.proc)
            sess.proc = None
            sess.player_paused = False
            if sess.cur is not None:
                self._safe_remove(sess.cur.path)
            sess.cur = None
            sess.paused = False
            sess.inflight = 0
            sess.playing_msg_id = None
            if self.active_sid == sess.sid:
                self.active_sid = None
            self._set_state(sess, "ended" if sess.can_replay() else "idle")

    def cmd_disable(self, sid=None):
        """Mute a session for ALL future output: stop it now and refuse new messages
        until it is enabled again."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=True)
            if not sess:
                return
            sess.gen_token += 1             # invalidate this session's in-flight synth
            if sess.cur is not None:
                self._mark_cancelled(sess.cur.msg_id)
                self._safe_remove(sess.cur.path)
            self._drain_session(sess)
            self._kill(sess.proc)
            sess.cur = None
            sess.proc = None
            sess.player_paused = False
            sess.paused = False
            sess.inflight = 0
            sess.playing_msg_id = None
            sess.disabled = True
            if self.active_sid == sess.sid:
                self.active_sid = None
            self._set_state(sess, "disabled")

    def cmd_enable(self, sid=None):
        """Re-enable a disabled session so future messages speak again."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=False)
            if not sess:
                return
            sess.disabled = False
            self._set_state(sess, "ended" if sess.can_replay() else "idle")

    def cmd_stop_all(self):
        """Silence everything queued, across all sessions."""
        with self.play_lock:
            self.generation += 1
            self.synth_chan.drop_where(lambda x: True)
            with self.sessions_lock:
                sessions = list(self.sessions.values())
            for sess in sessions:
                sess.gen_token += 1
                self._drain_session(sess)
                self._kill(sess.proc)
                if sess.cur is not None:
                    self._safe_remove(sess.cur.path)
                sess.cur = None
                sess.proc = None
                sess.player_paused = False
                sess.paused = False
                sess.inflight = 0
                sess.playing_msg_id = None
                sess.state = "ended" if sess.can_replay() else "idle"
            self.active_sid = None
        with self.pending_lock:
            self.pending.clear()
        self.emit_sessions()

    def cmd_repeat(self, sid=None):
        """Replay the current paragraph from its start, then continue forward."""
        self._seek(sid, lambda sess: sess.cur_idx)

    def cmd_prev(self, sid=None):
        """Step back exactly one paragraph, then continue forward from there."""
        self._seek(sid, lambda sess: sess.cur_idx - 1)

    def cmd_restart(self, sid=None):
        """Re-speak the whole current message from the beginning."""
        self._seek(sid, lambda sess: 0)

    def _seek(self, sid, pick_idx):
        """Re-queue a session's current message starting at the chosen paragraph."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=False)
            if not sess or not sess.msg_chunks or not sess.played:
                return
            target = max(0, min(pick_idx(sess), len(sess.msg_chunks) - 1))
            chunks, full = sess.msg_chunks, sess.msg_full
            self._clear_for_seek(sess)
            sess.cur_idx = target       # so a follow-up Prev steps from the new spot
            msg_id = "_nav_%d" % (self.seq_counter + 1)
        self._enqueue_chunks(sess, msg_id, chunks, full, target)

    def _clear_for_seek(self, sess):
        """Tear down a session's current playback so it can be re-queued from a
        chosen paragraph. Keeps msg_chunks/msg_full intact for navigation."""
        sess.gen_token += 1                 # invalidate this session's in-flight synth
        if sess.cur is not None:
            self._mark_cancelled(sess.cur.msg_id)
            self._safe_remove(sess.cur.path)
        self._drain_session(sess)
        self._kill(sess.proc)
        sess.cur = None
        sess.proc = None
        sess.player_paused = False
        sess.paused = False
        sess.inflight = 0
        sess.playing_msg_id = None
        self.active_sid = sess.sid
        self._set_state(sess, "playing")

    def cmd_focus(self, sid):
        self.focus_sid = sid

    def cmd_speak(self, text, voice=None, label=None):
        """Speak text handed over by a cc-speak invocation (e.g. a clipboard hotkey),
        under its own tab so it gets the same transport + read-along as a session."""
        if not text or not text.strip():
            return
        label = label or "clipboard"
        key = "<adhoc>:" + label         # not a real path: the watcher skips it
        with self.play_lock:
            sess = self.sessions.get(key)
            if sess is None:
                sess = Session("adhoc-" + label, label, key)
                sess.adhoc = True
                sess.config_dir = os.path.join(os.path.expanduser("~"), ".claude")
                self.order_counter += 1
                sess.order_seq = self.order_counter
                self.sessions[key] = sess
            if voice:
                sess.voice = voice
            sess.disabled = False
            sess.last_active = time.time()
            self.seq_counter += 1
            msg_id = "_adhoc_%d" % self.seq_counter
        self._enqueue_message(sess, msg_id, text)
        self.emit_sessions()

    def cmd_close(self, sid=None):
        """Close a tab: silence the session and stop tracking it. A file-backed
        session stays closed for the daemon's lifetime, even if the file grows."""
        with self.play_lock:
            sess = self._resolve(sid, prefer_active=False)
            if not sess:
                return
            self.cmd_stop(sess.sid)
            for path, s in list(self.sessions.items()):
                if s.sid == sess.sid:
                    self.sessions.pop(path, None)
                    if not s.adhoc:
                        self.closed_paths.add(path)
            if self.focus_sid == sess.sid:
                self.focus_sid = None
            if self.active_sid == sess.sid:
                self.active_sid = None
        self.emit_sessions()

    def _drain_session(self, sess):
        """Remove all queued PlayItems for a session and delete their temp files."""
        while sess.q:
            item = sess.q.popleft()
            self._safe_remove(item.path)
        self.synth_chan.drop_where(lambda x: x.session == sess.sid)

    def _resolve(self, sid, prefer_active):
        """Pick the session a command targets: explicit sid, else active/focus."""
        if sid:
            return self._session_by_sid(sid)
        if prefer_active and self.active_sid:
            return self._session_by_sid(self.active_sid)
        if self.focus_sid:
            return self._session_by_sid(self.focus_sid)
        return None

    # ─── socket server ──────────────────────────────────────────────────────────

    def _socket_server(self):
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        srv.listen(8)
        srv.settimeout(0.5)
        while self.running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.clients_lock:
                self.clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,),
                             daemon=True).start()
            self.emit_sessions()    # send fresh state to the new client
        srv.close()

    def _client_loop(self, conn):
        buf = b""
        try:
            conn.settimeout(None)
            while self.running:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(line.decode("utf-8", "replace").strip())
        except OSError:
            pass
        finally:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, line):
        if not line:
            return
        try:
            msg = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            msg = {"cmd": line}        # allow bare "pause" etc.
        cmd = msg.get("cmd")
        sid = msg.get("sid")
        if cmd == "pause":
            self.cmd_pause(sid)
        elif cmd == "resume" or cmd == "play":
            self.cmd_resume(sid)
        elif cmd == "toggle":
            self.cmd_toggle(sid)
        elif cmd == "skip":
            self.cmd_skip(sid)
        elif cmd == "stop":
            self.cmd_stop(sid)
        elif cmd == "stop_all":
            self.cmd_stop_all()
        elif cmd == "disable":
            self.cmd_disable(sid)
        elif cmd == "enable":
            self.cmd_enable(sid)
        elif cmd == "repeat":
            self.cmd_repeat(sid)
        elif cmd in ("prev", "previous", "back"):
            self.cmd_prev(sid)
        elif cmd == "restart":
            self.cmd_restart(sid)
        elif cmd == "focus":
            self.cmd_focus(sid)
        elif cmd == "speak":
            self.cmd_speak(msg.get("text"), msg.get("voice"), msg.get("label"))
        elif cmd == "close":
            self.cmd_close(sid)
        elif cmd == "ping":
            self.emit_sessions()

    def stop(self):
        self.running = False
        self.cmd_stop_all()
        self.synth_chan.close()
        self._cleanup()


_ipc_counter = itertools.count()

# sockaddr_un.sun_path is 108 bytes; a long TMPDIR would silently break IPC and
# drop us back to the SIGSTOP path, so fall back to /tmp when it won't fit.
_SUN_PATH_MAX = 100


def _ipc_socket_path():
    name = f"ccspeak-mpv-{os.getpid()}-{next(_ipc_counter)}.sock"
    path = os.path.join(tempfile.gettempdir(), name)
    if len(path.encode()) > _SUN_PATH_MAX:
        path = os.path.join("/tmp", name)
    return path


class _SignalProc:
    """A player paused by freezing the process itself.

    SIGSTOP leaves the player's audio stream open but unfed, so the sink keeps
    pulling and loops whatever stale samples are still in the ring buffer — an
    audible whine for as long as the pause lasts. Only used when mpv, which can
    pause its output stream properly, isn't available.
    """

    def __init__(self, proc):
        self.proc = proc
        self.paused = False

    def poll(self):
        return self.proc.poll()

    def _signal(self, sig):
        try:
            self.proc.send_signal(sig)
            return True
        except (OSError, ValueError):
            return False

    def pause(self):
        self.paused = self._signal(signal.SIGSTOP)
        return self.paused

    def resume(self):
        self._signal(signal.SIGCONT)
        self.paused = False
        return True

    def kill(self):
        if self.paused:
            self._signal(signal.SIGCONT)   # a stopped proc can't reap its own SIGKILL
        try:
            self.proc.kill()
            self.proc.wait(timeout=1.0)    # reap it; nothing polls this proc again
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass


class _MpvProc(_SignalProc):
    """mpv driven over a JSON IPC socket.

    Pausing via IPC makes mpv cork its audio stream, so the sink goes idle
    instead of underrunning on a frozen client. If the socket isn't up yet
    (pause pressed within a few ms of spawn) we fall back to SIGSTOP, and
    remember that so resume sends the matching SIGCONT.
    """

    IPC_WAIT = 0.4          # how long to wait for mpv to create its socket

    def __init__(self, proc, ipc_path):
        super().__init__(proc)
        self.ipc_path = ipc_path
        self.froze = False   # did we fall back to SIGSTOP for this pause?

    def _command(self, *args):
        """Send one IPC command, waiting briefly for the socket to appear."""
        deadline = time.monotonic() + self.IPC_WAIT
        payload = json.dumps({"command": list(args)}).encode() + b"\n"
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(self.ipc_path)
                    s.sendall(payload)
                    return True
            except (OSError, socket.timeout):
                if time.monotonic() >= deadline or self.proc.poll() is not None:
                    return False
                time.sleep(0.02)

    def pause(self):
        if self._command("set_property", "pause", True):
            self.froze = False
        else:
            self.froze = self._signal(signal.SIGSTOP)
        self.paused = True
        return True

    def resume(self):
        if self.froze:
            self._signal(signal.SIGCONT)
            self.froze = False
        self._command("set_property", "pause", False)
        self.paused = False
        return True

    def kill(self):
        super().kill()
        try:
            os.unlink(self.ipc_path)
        except OSError:
            pass


class _DummyProc:
    """Stand-in when no controllable player exists; behaves as an instantly-done proc."""

    def poll(self):
        return 0

    def pause(self):
        return True

    def resume(self):
        return True

    def kill(self):
        pass


def _daemon_is_live():
    """True if another daemon already owns the control socket.

    Probing beats a pid file here: the socket is the resource being contended,
    so a refused connect proves the file is stale with no pid-reuse ambiguity.
    """
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(SOCKET_PATH)
        return True
    except OSError:
        return False        # leftover socket file, nobody listening


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Claude Speak daemon (socket-controlled)")
    ap.add_argument("--voice", "-v", default="en-US-GuyNeural")
    ap.add_argument("--rate", "-r", default="+10%")
    ap.add_argument("--debounce", "-d", type=int, default=1500)
    args = ap.parse_args()

    # Must run before constructing the Daemon: _socket_server unlinks the socket
    # before binding, so a second instance would otherwise steal it from the
    # live one and leave two watchers tailing the same logs.
    if _daemon_is_live():
        print(f"claude-speak daemon already running (socket: {SOCKET_PATH})",
              file=sys.stderr)
        sys.exit(1)

    daemon = Daemon(voice=args.voice, rate=args.rate, debounce_ms=args.debounce)

    def handler(sig, frame):
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    print(f"claude-speak daemon running. socket: {SOCKET_PATH}", file=sys.stderr)
    try:
        while daemon.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        daemon.stop()


if __name__ == "__main__":
    main()
