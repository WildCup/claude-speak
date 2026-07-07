#!/usr/bin/env python3
"""
speak_ui: tiny transport window for the claude-speak daemon.

  - one tab per active Claude session (auto-created)
  - transport buttons: pause/resume, stop-this-message, skip, repeat, silence-all
  - live word highlighting of the current paragraph (edge-tts WordBoundary timing)
  - per-tab mute, so a noisy session can be silenced while another keeps talking

Run alongside the daemon:
    .venv/bin/python speak_ui.py
"""

import os
import json
import time
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk

SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")


class SpeakUI:
    def __init__(self, root):
        self.root = root
        root.title("claude-speak")
        root.geometry("520x300")
        root.minsize(380, 200)

        self.events = queue.Queue()
        self.sock = None
        self.sock_lock = threading.Lock()
        self.connected = False

        self.tabs = {}          # sid -> dict(frame, text, label, muted_var)
        self.paused = False

        # current-playback highlight state (only one plays at a time)
        self.cur_sid = None
        self.cur_words = []
        self.cur_spans = []
        self.t0 = None
        self.pause_accum = 0.0
        self._pause_started = None

        self._build()
        threading.Thread(target=self._reader, daemon=True).start()
        self.root.after(33, self._tick)
        self.root.after(50, self._drain)

    # ─── widgets ──────────────────────────────────────────────────────────────

    def _build(self):
        bar = ttk.Frame(self.root, padding=(6, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        self.btn_pause = ttk.Button(bar, text="⏸ Pause", width=10,
                                    command=self._toggle)
        self.btn_pause.pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⏮ Repeat", width=9,
                   command=lambda: self.send("repeat", self.cur_sid)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⏭ Skip", width=8,
                   command=lambda: self.send("skip")).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⏹ Stop msg", width=10,
                   command=lambda: self.send("stop")).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="🔇 All", width=6,
                   command=lambda: self.send("stop_all")).pack(side=tk.LEFT, padx=2)

        self.status = ttk.Label(self.root, text="connecting…", anchor=tk.W,
                                padding=(8, 2))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

    def _ensure_tab(self, sid, label):
        if sid in self.tabs:
            return self.tabs[sid]
        frame = ttk.Frame(self.nb)
        top = ttk.Frame(frame)
        top.pack(side=tk.TOP, fill=tk.X)
        muted_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="mute", variable=muted_var,
                        command=lambda s=sid, v=muted_var: self.send(
                            "unmute" if not v.get() else "mute", s)
                        ).pack(side=tk.RIGHT, padx=4, pady=2)
        text = tk.Text(frame, wrap=tk.WORD, height=8, font=("DejaVu Sans", 12),
                       relief=tk.FLAT, padx=8, pady=6, state=tk.DISABLED,
                       cursor="arrow")
        text.pack(fill=tk.BOTH, expand=True)
        text.tag_configure("hl", background="#ffe08a")
        text.tag_configure("spoken", foreground="#888888")
        self.nb.add(frame, text=label or sid[:8])
        self.tabs[sid] = {"frame": frame, "text": text, "label": label,
                          "muted_var": muted_var}
        return self.tabs[sid]

    # ─── networking ───────────────────────────────────────────────────────────

    def _reader(self):
        buf = b""
        while True:
            if self.sock is None:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(SOCKET_PATH)
                    with self.sock_lock:
                        self.sock = s
                    self.connected = True
                    self.events.put({"ev": "_connected"})
                except OSError:
                    self.connected = False
                    self.events.put({"ev": "_disconnected"})
                    time.sleep(1.0)
                    continue
            try:
                data = self.sock.recv(8192)
                if not data:
                    raise OSError
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self.events.put(json.loads(line.decode("utf-8", "replace")))
                    except (ValueError, json.JSONDecodeError):
                        pass
            except OSError:
                with self.sock_lock:
                    self.sock = None
                self.connected = False
                self.events.put({"ev": "_disconnected"})
                buf = b""
                time.sleep(1.0)

    def send(self, cmd, sid=None):
        msg = {"cmd": cmd}
        if sid:
            msg["sid"] = sid
        with self.sock_lock:
            if self.sock is None:
                return
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            except OSError:
                self.sock = None

    def _toggle(self):
        self.send("toggle")

    # ─── event handling (main thread) ──────────────────────────────────────────

    def _drain(self):
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _handle(self, ev):
        kind = ev.get("ev")
        if kind == "_connected":
            self.status.config(text="connected")
            self.send("ping")
        elif kind == "_disconnected":
            self.status.config(text="daemon not running — retrying…")
        elif kind == "sessions":
            self._update_sessions(ev)
        elif kind == "play":
            self._on_play(ev)
        elif kind == "end":
            self._on_end(ev)
        elif kind == "pause":
            self.paused = True
            self._pause_started = time.monotonic()
            self.btn_pause.config(text="▶ Resume")
        elif kind == "resume":
            self.paused = False
            if self._pause_started is not None:
                self.pause_accum += time.monotonic() - self._pause_started
                self._pause_started = None
            self.btn_pause.config(text="⏸ Pause")

    def _update_sessions(self, ev):
        self.paused = ev.get("paused", False)
        self.btn_pause.config(text="▶ Resume" if self.paused else "⏸ Pause")
        for s in ev.get("sessions", []):
            tab = self._ensure_tab(s["sid"], s["label"])
            tab["muted_var"].set(s.get("muted", False))
            try:
                self.nb.tab(tab["frame"], text=(
                    ("🔇 " if s.get("muted") else "") + (s["label"] or s["sid"][:8])))
            except tk.TclError:
                pass
        n = len(ev.get("sessions", []))
        self.status.config(text=f"connected · {n} session(s)"
                           + (" · paused" if self.paused else ""))

    def _on_play(self, ev):
        sid = ev.get("sid")
        tab = self._ensure_tab(sid, ev.get("label") or sid)
        text = ev.get("text", "")
        self.cur_sid = sid
        self.cur_words = ev.get("words", [])
        self.cur_spans = self._map_spans(text, self.cur_words)
        self.t0 = time.monotonic()
        self.pause_accum = 0.0
        self._pause_started = time.monotonic() if self.paused else None

        w = tab["text"]
        w.config(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        w.insert("1.0", text)
        w.config(state=tk.DISABLED)
        # focus the tab that's speaking
        try:
            self.nb.select(tab["frame"])
        except tk.TclError:
            pass

    def _on_end(self, ev):
        if ev.get("sid") == self.cur_sid:
            tab = self.tabs.get(self.cur_sid)
            if tab:
                tab["text"].tag_remove("hl", "1.0", tk.END)
            self.cur_words = []
            self.cur_spans = []
            self.t0 = None

    @staticmethod
    def _map_spans(text, words):
        """Map each WordBoundary token to a (start,end) char offset in text."""
        spans = []
        idx = 0
        for wd in words:
            tok = wd.get("w", "")
            if not tok:
                spans.append(None)
                continue
            pos = text.find(tok, idx)
            if pos < 0:
                pos = text.find(tok)        # retry from the front
            if pos < 0:
                spans.append(None)
            else:
                spans.append((pos, pos + len(tok)))
                idx = pos + len(tok)
        return spans

    def _on_tab_change(self, _evt):
        try:
            frame = self.nb.nametowidget(self.nb.select())
        except (tk.TclError, KeyError):
            return
        for sid, tab in self.tabs.items():
            if tab["frame"] is frame:
                self.send("focus", sid)
                break

    # ─── highlight ticker ──────────────────────────────────────────────────────

    def _tick(self):
        if self.cur_sid and self.t0 is not None and self.cur_words and not self.paused:
            tab = self.tabs.get(self.cur_sid)
            if tab:
                elapsed = (time.monotonic() - self.t0 - self.pause_accum) * 1000.0
                self._apply_highlight(tab["text"], elapsed)
        self.root.after(33, self._tick)

    def _apply_highlight(self, w, elapsed):
        # find the last word whose start time has passed
        active = -1
        for i, wd in enumerate(self.cur_words):
            if wd["t"] <= elapsed:
                active = i
            else:
                break
        w.tag_remove("hl", "1.0", tk.END)
        if active < 0 or active >= len(self.cur_spans):
            return
        span = self.cur_spans[active]
        if not span:
            return
        start = f"1.0+{span[0]}c"
        end = f"1.0+{span[1]}c"
        try:
            w.tag_add("spoken", "1.0", start)
            w.tag_add("hl", start, end)
            w.see(start)
        except tk.TclError:
            pass


def main():
    root = tk.Tk()
    SpeakUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
