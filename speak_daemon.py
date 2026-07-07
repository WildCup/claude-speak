#!/usr/bin/env python3
"""
speak_daemon: watch all Claude Code conversation logs and speak assistant output,
with a Unix-socket control channel for pause / resume / stop / skip / repeat /
per-session mute, and word-level timing streamed to the UI for live highlighting.

Architecture:
    watcher thread   -> discovers JSONL files, tails them, enqueues Utterances
    synth worker     -> Utterance -> edge-tts mp3 + WordBoundary timings -> PlayItem
    playback worker  -> PlayItem  -> ffplay (controllable: SIGSTOP/SIGCONT/kill)
    socket server    -> accepts UI/CLI clients, dispatches commands, broadcasts events

The whole pipeline is in one process so a single audio output is shared and the
transport controls (stop/pause/skip) act on exactly what you hear.
"""

import os
import sys
import json
import glob
import time
import queue
import signal
import socket
import atexit
import asyncio
import hashlib
import tempfile
import threading
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
GLOBAL_PAUSE_FLAG = os.path.join(os.path.expanduser("~"), ".claude", "speech-paused")

# Only actively tail files touched within this window (keeps stat load bounded).
ACTIVE_WINDOW_SEC = 2 * 60 * 60
POLL_SEC = 1.0
RESCAN_SEC = 3.0


def now_ms():
    return time.monotonic() * 1000.0


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
    __slots__ = ("session", "msg_id", "seq", "text", "gen")

    def __init__(self, session, msg_id, seq, text, gen):
        self.session = session
        self.msg_id = msg_id
        self.seq = seq
        self.text = text
        self.gen = gen


class PlayItem:
    __slots__ = ("session", "msg_id", "seq", "text", "path", "words", "gen")

    def __init__(self, session, msg_id, seq, text, path, words, gen):
        self.session = session
        self.msg_id = msg_id
        self.seq = seq
        self.text = text
        self.path = path
        self.words = words
        self.gen = gen


# ─── Session bookkeeping ───────────────────────────────────────────────────────


class Session:
    def __init__(self, sid, label, path):
        self.sid = sid              # stable id (sessionId or file stem)
        self.label = label          # human label (basename of cwd)
        self.path = path            # jsonl path
        self.config_dir = os.path.dirname(path)
        self.file_pos = 0
        self.identity = None        # inode, to detect file replacement
        self.muted = False
        self.last_active = 0.0
        # recent played utterances (text only) for "repeat / back one paragraph"
        self.history = collections.deque(maxlen=20)


# ─── The daemon ────────────────────────────────────────────────────────────────


class Daemon:
    def __init__(self, voice="en-US-GuyNeural", rate="+10%", debounce_ms=1500):
        self.voice = voice
        self.rate = rate
        self.debounce_ms = debounce_ms
        self.running = True
        self.start_epoch = time.time()

        self.synth_chan = Channel()
        self.play_chan = Channel()
        self.temp_dir = tempfile.mkdtemp(prefix="claude_speak_")
        self.file_counter = 0
        self.seq_counter = 0

        # dedup of spoken message ids (bounded)
        self._spoken = collections.OrderedDict()
        self._spoken_max = 4000

        # sessions keyed by jsonl path
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.focus_sid = None       # which session repeat/back acts on

        # cancellation: a message stopped mid-flight, or a global flush
        self.generation = 0                 # bumped by stop_all
        self.cancelled = collections.OrderedDict()   # cancelled msg_ids (bounded)
        self.cancel_lock = threading.Lock()

        # playback control
        self.paused = False                 # global pause
        self.play_gate = threading.Event()  # set => playback allowed to start next item
        self.play_gate.set()
        self.current_proc = None
        self.current_item = None
        self.proc_lock = threading.Lock()

        # debounce accumulator, per session
        self.pending = {}                   # path -> {"text":..., "msg_id":..., "t":...}
        self.pending_lock = threading.Lock()

        # connected clients for event broadcast
        self.clients = []
        self.clients_lock = threading.Lock()

        atexit.register(self._cleanup)

        self._threads = [
            threading.Thread(target=self._synth_worker, daemon=True),
            threading.Thread(target=self._play_worker, daemon=True),
            threading.Thread(target=self._debounce_flusher, daemon=True),
            threading.Thread(target=self._watcher, daemon=True),
            threading.Thread(target=self._socket_server, daemon=True),
        ]
        for t in self._threads:
            t.start()

    # ─── helpers ──────────────────────────────────────────────────────────────

    def _cleanup(self):
        import shutil
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

    def _is_session_paused(self, sess):
        """Per-session pause = global pause flag-file, project flag-file, or mute."""
        if sess.muted:
            return True
        if os.path.exists(GLOBAL_PAUSE_FLAG):
            return True
        if sess.config_dir and os.path.exists(
            os.path.join(sess.config_dir, "speech-paused")
        ):
            return True
        return False

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
            payload = [
                {
                    "sid": s.sid,
                    "label": s.label,
                    "muted": s.muted,
                    "focus": s.sid == self.focus_sid,
                    "last_active": s.last_active,
                }
                for s in sorted(self.sessions.values(),
                                key=lambda s: -s.last_active)
            ]
        self.broadcast({"ev": "sessions", "sessions": payload,
                        "paused": self.paused})

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
        # fall back to decoding the project dir name
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
                            if path not in self.sessions:
                                self._register(path)
                                found_new = True
                    if found_new:
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

    def _tail(self, sess):
        """Read new lines from one session file. Returns True if it became active."""
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
                    for chunk in cc_speak.extract_speakable_chunks(text):
                        self.seq_counter += 1
                        self.synth_chan.put(
                            Utterance(sess.sid, msg_id, self.seq_counter, chunk,
                                      self.generation))
            except Exception as e:
                print(f"debounce error: {e}", file=sys.stderr)

    # ─── synth worker ───────────────────────────────────────────────────────────

    def _synth_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while self.running:
            u = self.synth_chan.get()
            if u is None:
                continue
            try:
                if self._is_cancelled(u):
                    continue
                cleaned = cc_speak.clean_text(u.text, skip_code=True, skip_paths=True)
                if not cleaned.strip() or len(cleaned.split()) < 3:
                    continue
                sess = self._session_by_sid(u.session)
                if sess and self._is_session_paused(sess):
                    continue
                self.file_counter += 1
                out = os.path.join(self.temp_dir, f"s{self.file_counter}.mp3")
                voice = self._voice_for(sess)
                words = loop.run_until_complete(
                    self._synth(cleaned, voice, self.rate, out))
                if self._is_cancelled(u):       # stopped while we were synthesizing
                    self._safe_remove(out)
                    continue
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    self.play_chan.put(
                        PlayItem(u.session, u.msg_id, u.seq, cleaned, out, words,
                                 u.gen))
            except Exception as e:
                print(f"synth error: {e}", file=sys.stderr)

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

    def _session_by_sid(self, sid):
        with self.sessions_lock:
            for s in self.sessions.values():
                if s.sid == sid:
                    return s
        return None

    def _voice_for(self, sess):
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

    # ─── playback worker ────────────────────────────────────────────────────────

    def _play_worker(self):
        while self.running:
            item = self.play_chan.get()
            if item is None:
                continue
            try:
                if self._is_cancelled(item):
                    self._safe_remove(item.path)
                    continue
                sess = self._session_by_sid(item.session)
                if sess and self._is_session_paused(sess):
                    self._safe_remove(item.path)
                    continue
                # wait out a global pause that happened between items
                while self.running and not self.play_gate.is_set():
                    self.play_gate.wait(0.2)
                if not self.running:
                    break

                if sess:
                    sess.history.append(item.text)
                self.current_item = item
                # tell UI what is about to be spoken + the word timings
                self.broadcast({
                    "ev": "play",
                    "sid": item.session,
                    "msg_id": item.msg_id,
                    "seq": item.seq,
                    "text": item.text,
                    "words": item.words,
                    "epoch_ms": now_ms(),
                })
                self._play_file(item.path)
                self.broadcast({"ev": "end", "sid": item.session, "seq": item.seq})
                self.current_item = None
                self._safe_remove(item.path)
            except Exception as e:
                print(f"play error: {e}", file=sys.stderr)
                self.current_item = None
                self._safe_remove(item.path)

    def _play_file(self, path):
        import shutil
        ffplay = shutil.which("ffplay")
        if not ffplay:
            cc_speak.play_audio(path)    # fallback (not pausable)
            return
        import subprocess
        try:
            proc = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return
        with self.proc_lock:
            self.current_proc = proc
        proc.wait()
        with self.proc_lock:
            self.current_proc = None

    @staticmethod
    def _safe_remove(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ─── transport commands ────────────────────────────────────────────────────

    def cmd_pause(self):
        self.paused = True
        self.play_gate.clear()
        with self.proc_lock:
            if self.current_proc:
                try:
                    self.current_proc.send_signal(signal.SIGSTOP)
                except OSError:
                    pass
        self.broadcast({"ev": "pause"})
        self.emit_sessions()

    def cmd_resume(self):
        self.paused = False
        self.play_gate.set()
        with self.proc_lock:
            if self.current_proc:
                try:
                    self.current_proc.send_signal(signal.SIGCONT)
                except OSError:
                    pass
        self.broadcast({"ev": "resume"})
        self.emit_sessions()

    def cmd_skip(self):
        """Stop the current chunk only; the next queued chunk plays."""
        with self.proc_lock:
            if self.current_proc:
                try:
                    self.current_proc.send_signal(signal.SIGCONT)  # in case paused
                    self.current_proc.kill()
                except OSError:
                    pass

    def cmd_stop(self):
        """Silence the rest of the CURRENT message: kill player + drop its chunks."""
        cur = self.current_item
        if cur is not None:
            mid = cur.msg_id
            self._mark_cancelled(mid)       # also cancels in-flight synthesis
            self.play_chan.drop_where(lambda x: x.msg_id == mid)
            self.synth_chan.drop_where(lambda x: x.msg_id == mid)
        with self.proc_lock:
            if self.current_proc:
                try:
                    self.current_proc.send_signal(signal.SIGCONT)
                    self.current_proc.kill()
                except OSError:
                    pass

    def cmd_stop_all(self):
        """Silence everything queued, across all sessions."""
        self.generation += 1                # cancels everything in flight
        self.play_chan.drop_where(lambda x: True)
        self.synth_chan.drop_where(lambda x: True)
        with self.pending_lock:
            self.pending.clear()
        with self.proc_lock:
            if self.current_proc:
                try:
                    self.current_proc.send_signal(signal.SIGCONT)
                    self.current_proc.kill()
                except OSError:
                    pass

    def cmd_repeat(self, sid=None):
        """Re-speak the last paragraph of the focused (or given) session."""
        sid = sid or self.focus_sid
        sess = self._session_by_sid(sid) if sid else None
        if not sess or not sess.history:
            return
        text = sess.history[-1]
        self.seq_counter += 1
        self.synth_chan.put_front(
            Utterance(sess.sid, "_repeat_%d" % self.seq_counter, self.seq_counter,
                      text, self.generation))

    def cmd_mute(self, sid, muted):
        sess = self._session_by_sid(sid)
        if sess:
            sess.muted = muted
            if muted:
                # drop anything queued for it
                self.play_chan.drop_where(lambda x: x.session == sid)
                self.synth_chan.drop_where(lambda x: x.session == sid)
            self.emit_sessions()

    def cmd_focus(self, sid):
        self.focus_sid = sid
        self.emit_sessions()

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
            self.cmd_pause()
        elif cmd == "resume":
            self.cmd_resume()
        elif cmd == "toggle":
            self.cmd_resume() if self.paused else self.cmd_pause()
        elif cmd == "skip":
            self.cmd_skip()
        elif cmd == "stop":
            self.cmd_stop()
        elif cmd == "stop_all":
            self.cmd_stop_all()
        elif cmd == "repeat" or cmd == "back":
            self.cmd_repeat(sid)
        elif cmd == "mute":
            self.cmd_mute(sid, True)
        elif cmd == "unmute":
            self.cmd_mute(sid, False)
        elif cmd == "focus":
            self.cmd_focus(sid)
        elif cmd == "ping":
            self.emit_sessions()

    def stop(self):
        self.running = False
        self.cmd_stop_all()
        self.synth_chan.close()
        self.play_chan.close()
        self._cleanup()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Claude Speak daemon (socket-controlled)")
    ap.add_argument("--voice", "-v", default="en-US-GuyNeural")
    ap.add_argument("--rate", "-r", default="+10%")
    ap.add_argument("--debounce", "-d", type=int, default=1500)
    args = ap.parse_args()

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
