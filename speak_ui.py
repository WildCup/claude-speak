#!/usr/bin/env python3
"""
speak_ui: a tiny transport window for the claude-speak daemon.

  - one pill tab per active Claude session, along the top
  - "Stop all" on the top right
  - transport buttons act on the SELECTED tab's session, and enable / disable /
    morph to match that session's state (Pause<->Resume, Stop<->Restart)
  - live word highlighting of the current paragraph (edge-tts WordBoundary timing),
    spoken words dimmed

Run alongside the daemon:
    .venv/bin/python speak_ui.py
"""

import os
import math
import json
import time
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, font as tkfont

SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")

# ─── palette ───────────────────────────────────────────────────────────────────
BG        = "#f4f4f6"
CARD      = "#ffffff"
INK       = "#20222a"
MUTED     = "#8a8d98"
ACCENT    = "#4c8bf5"
ACCENT_HI = "#3b73d6"
PILL_BG   = "#e6e7ec"
PILL_HI   = "#d5d7df"
BTN_BG    = "#eceef2"
BTN_HI    = "#dfe2e8"
BORDER    = "#e0e1e6"
HL_BG     = "#ffe08a"     # word being spoken
SPOKEN    = "#9a9da8"     # words already spoken


class SpeakUI:
    def __init__(self, root):
        self.root = root
        root.title("claude-speak")
        root.geometry("560x340")
        root.minsize(420, 240)
        root.configure(bg=BG)

        self.events = queue.Queue()
        self.sock = None
        self.sock_lock = threading.Lock()

        self.order = []                 # sids, in display order
        self.sessions = {}              # sid -> dict(label,state,can_replay,has_work,active)
        self.pills = {}                 # sid -> tk.Label pill widget
        self.selected = None            # selected sid
        self.follow = True              # auto-select the session that starts talking

        # per-session read-along state
        self.view = {}                  # sid -> dict(text,words,spans,t0,pause_accum,pause_started)

        self._fonts()
        self._set_icon()
        self._build()
        threading.Thread(target=self._reader, daemon=True).start()
        self.root.after(33, self._tick)
        self.root.after(50, self._drain)

    # ─── fonts / styles ─────────────────────────────────────────────────────────

    def _fonts(self):
        self.f_read = tkfont.Font(family="DejaVu Sans", size=13)
        self.f_pill = tkfont.Font(family="DejaVu Sans", size=10, weight="bold")
        self.f_btn = tkfont.Font(family="DejaVu Sans", size=11)
        self.f_status = tkfont.Font(family="DejaVu Sans", size=9)

    def _set_icon(self):
        """A speaker-with-soundwaves app icon, drawn into a PhotoImage (no assets)."""
        S = 64
        cx, cy = 30.0, 32.0
        rows = []
        for y in range(S):
            row = []
            for x in range(S):
                c = ACCENT
                if 14 <= x <= 23 and 25 <= y <= 39:            # speaker back plate
                    c = "#ffffff"
                elif 23 < x <= 33:                              # speaker cone
                    hh = 6 + (x - 23) * 0.9
                    if cy - hh <= y <= cy + hh:
                        c = "#ffffff"
                else:                                           # two sound-wave arcs
                    d = math.hypot(x - 24, y - cy)
                    if x >= 35 and abs(y - cy) <= (x - 26) * 0.95 \
                            and (15.5 <= d <= 17.3 or 21.0 <= d <= 22.8):
                        c = "#ffffff"
                row.append(c)
            rows.append("{" + " ".join(row) + "}")
        try:
            img = tk.PhotoImage(width=S, height=S)
            img.put(" ".join(rows))
            self.root.iconphoto(True, img)
            self._icon = img            # keep a ref so it isn't garbage-collected
        except tk.TclError:
            pass

    # ─── widgets ────────────────────────────────────────────────────────────────

    def _build(self):
        # top: tab strip (left) + stop-all (right)
        top = tk.Frame(self.root, bg=BG)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 6))

        self.stopall = tk.Label(top, text="⏹  Stop all", font=self.f_pill,
                                bg=PILL_BG, fg=INK, padx=12, pady=6, cursor="hand2")
        self.stopall.pack(side=tk.RIGHT)
        self._hover(self.stopall, PILL_BG, PILL_HI)
        self.stopall.bind("<Button-1>", lambda e: self.send("stop_all"))

        self.tabstrip = tk.Frame(top, bg=BG)
        self.tabstrip.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Pack the fixed strips (status, transport) from the bottom FIRST, so the
        # reading card's expand=True only claims the leftover middle — otherwise the
        # card eats everything and the transport bar collapses to zero height.
        self.status = tk.Label(self.root, text="connecting…", anchor=tk.W,
                               font=self.f_status, bg=BG, fg=MUTED, padx=12, pady=3)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        # transport for the selected session
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(6, 6))
        self.b_repeat = self._tbtn(bar, "⏮  Repeat", lambda: self.send("repeat", self.selected))
        self.b_play   = self._tbtn(bar, "⏸  Pause", self._toggle)
        self.b_skip   = self._tbtn(bar, "⏭  Skip", lambda: self.send("skip", self.selected))
        self.b_stop   = self._tbtn(bar, "⏹  Stop", self._stop_or_restart)

        # middle: reading card (fills whatever is left between top and bottom strips)
        card = tk.Frame(self.root, bg=BORDER)
        card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=2)
        self.text = tk.Text(card, wrap=tk.WORD, font=self.f_read, relief=tk.FLAT,
                            bg=CARD, fg=INK, padx=14, pady=12, state=tk.DISABLED,
                            cursor="arrow", highlightthickness=0, borderwidth=0)
        self.text.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.text.tag_configure("hl", background=HL_BG, foreground=INK)
        self.text.tag_configure("spoken", foreground=SPOKEN)
        self.text.tag_configure("idle", foreground=MUTED)

        self._refresh_buttons()

    def _tbtn(self, parent, text, cmd):
        b = tk.Label(parent, text=text, font=self.f_btn, bg=BTN_BG, fg=INK,
                     padx=14, pady=7, cursor="hand2")
        b.pack(side=tk.LEFT, padx=(0, 6))
        b._cmd = cmd
        b._enabled = True
        self._hover(b, BTN_BG, BTN_HI)
        b.bind("<Button-1>", lambda e, w=b: w._cmd() if w._enabled else None)
        return b

    def _hover(self, w, base, hi):
        w._base = base
        w.bind("<Enter>", lambda e: w.configure(bg=hi) if getattr(w, "_enabled", True) else None)
        w.bind("<Leave>", lambda e: w.configure(bg=getattr(w, "_base", base)))

    def _set_enabled(self, b, on, text=None):
        b._enabled = on
        if text is not None:
            b.configure(text=text)
        if on:
            b.configure(fg=INK, bg=b._base, cursor="hand2")
        else:
            b.configure(fg="#c2c4cc", bg=BTN_BG, cursor="arrow")

    # ─── tab strip ──────────────────────────────────────────────────────────────

    def _rebuild_tabs(self):
        for w in self.tabstrip.winfo_children():
            w.destroy()
        self.pills = {}
        # disambiguate sessions that share a label (e.g. two convos in one project)
        counts = {}
        for sid in self.order:
            lbl = self.sessions.get(sid, {}).get("label") or sid[:8]
            counts[lbl] = counts.get(lbl, 0) + 1
        for sid in self.order:
            s = self.sessions.get(sid, {})
            label = s.get("label") or sid[:8]
            if counts.get(label, 0) > 1:
                label = f"{label}·{sid[:4]}"
            dot = {"playing": "● ", "paused": "❚❚ ", "ended": "", "idle": ""}.get(
                s.get("state"), "")
            pill = tk.Label(self.tabstrip, text=dot + label, font=self.f_pill,
                            padx=12, pady=6, cursor="hand2")
            pill.pack(side=tk.LEFT, padx=(0, 6))
            pill.bind("<Button-1>", lambda e, x=sid: self._select(x, user=True))
            self.pills[sid] = pill
        self._paint_pills()

    def _paint_pills(self):
        for sid, pill in self.pills.items():
            if sid == self.selected:
                pill.configure(bg=ACCENT, fg="#ffffff")
                pill._base = ACCENT
                pill.unbind("<Enter>"); pill.unbind("<Leave>")
            else:
                pill.configure(bg=PILL_BG, fg=INK)
                self._hover(pill, PILL_BG, PILL_HI)

    def _select(self, sid, user=False):
        if sid not in self.sessions:
            return
        if user:
            # user picked a tab: stop auto-following the talking session
            self.follow = (self.sessions.get(sid, {}).get("state") == "playing")
        self.selected = sid
        self.send("focus", sid)
        self._paint_pills()
        self._refresh_text()
        self._refresh_buttons()

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
                    self.events.put({"ev": "_connected"})
                except OSError:
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
        s = self.sessions.get(self.selected, {})
        if s.get("state") == "playing":
            self.send("pause", self.selected)
        else:
            self.send("resume", self.selected)

    def _stop_or_restart(self):
        s = self.sessions.get(self.selected, {})
        if s.get("state") in ("playing", "paused"):
            self.send("stop", self.selected)
        else:
            self.send("restart", self.selected)

    # ─── event handling (main thread) ──────────────────────────────────────────

    def _drain(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
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
            self._on_pause(ev.get("sid"))
        elif kind == "resume":
            self._on_resume(ev.get("sid"))

    def _update_sessions(self, ev):
        sess = ev.get("sessions", [])
        self.sessions = {s["sid"]: s for s in sess}
        self.order = [s["sid"] for s in sess]
        if self.selected not in self.sessions:
            self.selected = self.order[0] if self.order else None
        self._rebuild_tabs()
        self._refresh_text()
        self._refresh_buttons()
        n = len(sess)
        talk = sum(1 for s in sess if s["state"] == "playing")
        self.status.config(text=f"connected · {n} session(s)"
                           + (f" · {talk} speaking" if talk else ""))

    def _on_play(self, ev):
        sid = ev.get("sid")
        text = ev.get("text", "")
        words = ev.get("words", [])
        self.view[sid] = {
            "text": text, "words": words, "spans": self._map_spans(text, words),
            "t0": time.monotonic(), "pause_accum": 0.0, "pause_started": None,
        }
        if self.follow or sid == self.selected or self.selected is None:
            self.selected = sid
            self.follow = True
            self._paint_pills()
        if sid == self.selected:
            self._refresh_text()
        self._refresh_buttons()

    def _on_end(self, ev):
        sid = ev.get("sid")
        v = self.view.get(sid)
        if v:
            v["t0"] = None
        if sid == self.selected:
            self.text.tag_remove("hl", "1.0", tk.END)

    def _on_pause(self, sid):
        v = self.view.get(sid)
        if v and v.get("t0") is not None and v.get("pause_started") is None:
            v["pause_started"] = time.monotonic()

    def _on_resume(self, sid):
        v = self.view.get(sid)
        if v and v.get("pause_started") is not None:
            v["pause_accum"] += time.monotonic() - v["pause_started"]
            v["pause_started"] = None

    # ─── reading card ───────────────────────────────────────────────────────────

    def _refresh_text(self):
        w = self.text
        w.config(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        v = self.view.get(self.selected)
        if v and v.get("text"):
            w.insert("1.0", v["text"])
        else:
            s = self.sessions.get(self.selected, {})
            hint = {"ended": "— finished. Repeat or Restart to hear it again. —",
                    "paused": "— paused —"}.get(s.get("state"),
                                                "— waiting for output… —")
            w.insert("1.0", hint, "idle")
        w.config(state=tk.DISABLED)

    def _refresh_buttons(self):
        s = self.sessions.get(self.selected, {}) if self.selected else {}
        state = s.get("state", "idle")
        can_replay = s.get("can_replay", False)
        has_work = s.get("has_work", False)
        active = state in ("playing", "paused")

        # Pause / Resume / Play
        if state == "playing":
            self._set_enabled(self.b_play, True, "⏸  Pause")
        elif state == "paused":
            self._set_enabled(self.b_play, True, "▶  Resume")
        elif has_work:
            self._set_enabled(self.b_play, True, "▶  Play")
        else:
            self._set_enabled(self.b_play, False, "▶  Play")

        self._set_enabled(self.b_skip, active, "⏭  Skip")
        self._set_enabled(self.b_repeat, can_replay, "⏮  Repeat")

        if active:
            self._set_enabled(self.b_stop, True, "⏹  Stop")
        elif can_replay:
            self._set_enabled(self.b_stop, True, "⟳  Restart")
        else:
            self._set_enabled(self.b_stop, False, "⟳  Restart")

    # ─── highlight ticker ──────────────────────────────────────────────────────

    def _tick(self):
        v = self.view.get(self.selected)
        s = self.sessions.get(self.selected, {})
        if v and v.get("t0") is not None and v.get("words") \
                and s.get("state") == "playing":
            elapsed = (time.monotonic() - v["t0"] - v["pause_accum"]) * 1000.0
            self._apply_highlight(v, elapsed)
        self.root.after(33, self._tick)

    def _apply_highlight(self, v, elapsed):
        active = -1
        for i, wd in enumerate(v["words"]):
            if wd["t"] <= elapsed:
                active = i
            else:
                break
        w = self.text
        w.tag_remove("hl", "1.0", tk.END)
        spans = v["spans"]
        if active < 0 or active >= len(spans) or not spans[active]:
            return
        span = spans[active]
        start = f"1.0+{span[0]}c"
        end = f"1.0+{span[1]}c"
        try:
            w.tag_add("spoken", "1.0", start)
            w.tag_add("hl", start, end)
            w.see(start)
        except tk.TclError:
            pass

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
                pos = text.find(tok)
            if pos < 0:
                spans.append(None)
            else:
                spans.append((pos, pos + len(tok)))
                idx = pos + len(tok)
        return spans


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    SpeakUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
