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
import json
import time
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, font as tkfont

SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude-speak.sock")

# ─── palette ───────────────────────────────────────────────────────────────────
BG        = "#16181d"
CARD      = "#1d2026"
INK       = "#e4e7ee"
MUTED     = "#7f848f"
ACCENT    = "#3fb950"     # selected tab / active session
ON_ACCENT = "#0c1410"     # text drawn on top of ACCENT
DISABLED  = "#4b515c"     # label of a button that can't be pressed
PILL_BG   = "#262a32"
PILL_HI   = "#323740"
BTN_BG    = "#22262d"
BTN_HI    = "#2e333c"
BORDER    = "#2c313a"
HL_BG     = "#2f6d3c"     # word being spoken
SPOKEN    = "#5b606a"     # words already spoken
TRACK     = "#2c313a"     # slider groove
THUMB     = "#3a404a"     # scrollbar thumb
THUMB_HI  = "#4d545f"


def dark_menu(parent):
    """Tk popup menus keep the system (light) look unless every colour is given."""
    return tk.Menu(parent, tearoff=0, bg=CARD, fg=INK,
                   activebackground=ACCENT, activeforeground=ON_ACCENT,
                   bd=0, relief=tk.FLAT, activeborderwidth=0)

LANG_NAMES = {
    "ar": "Arabic", "cs": "Czech", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "fi": "Finnish", "fr": "French",
    "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian", "id": "Indonesian",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nb": "Norwegian",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sv": "Swedish", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese",
}


class Slider(tk.Canvas):
    """Flat slider. Clicking anywhere on the groove jumps straight to that value
    instead of creeping toward it one step at a time."""

    H = 24
    R = 7                       # thumb radius

    def __init__(self, parent, lo, hi, on_change=None, on_commit=None, bg=BG):
        super().__init__(parent, height=self.H, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.lo, self.hi = lo, hi
        self.value = lo
        self.on_change, self.on_commit = on_change, on_commit
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self.set(self._val_for(e.x)))
        self.bind("<B1-Motion>", lambda e: self.set(self._val_for(e.x)))
        self.bind("<ButtonRelease-1>", self._release)

    def get(self):
        return self.value

    def set(self, val, notify=True):
        val = max(self.lo, min(self.hi, int(val)))
        changed = val != self.value
        self.value = val
        self._draw()
        if changed and notify and self.on_change:
            self.on_change(val)

    def _span(self):
        return max(1, self.winfo_width() - 2 * self.R - 2)

    def _x_for(self, val):
        frac = (val - self.lo) / float(self.hi - self.lo)
        return self.R + 1 + frac * self._span()

    def _val_for(self, x):
        frac = max(0.0, min(1.0, (x - self.R - 1) / float(self._span())))
        return int(round(self.lo + frac * (self.hi - self.lo)))

    def _release(self, ev):
        self.set(self._val_for(ev.x))
        if self.on_commit:
            self.on_commit(self.value)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            return
        cy, x = self.H // 2, self._x_for(self.value)
        self.create_line(self.R + 1, cy, w - self.R - 1, cy,
                         fill=TRACK, width=4, capstyle=tk.ROUND)
        self.create_line(self.R + 1, cy, x, cy,
                         fill=ACCENT, width=4, capstyle=tk.ROUND)
        self.create_oval(x - self.R, cy - self.R, x + self.R, cy + self.R,
                         fill=CARD, outline=ACCENT, width=2)


class SlimScroll(tk.Canvas):
    """A thin rounded scrollbar thumb on a bare track — no arrows, no bevels."""

    W = 10
    MIN_THUMB = 28

    def __init__(self, parent, command, bg=CARD):
        super().__init__(parent, width=self.W, bg=bg, highlightthickness=0, bd=0)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.fill = THUMB
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._to_pointer)
        self.bind("<B1-Motion>", self._to_pointer)
        self.bind("<Enter>", lambda e: self._tint(THUMB_HI))
        self.bind("<Leave>", lambda e: self._tint(THUMB))

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _tint(self, color):
        self.fill = color
        self._draw()

    def _to_pointer(self, ev):
        h = max(1, self.winfo_height())
        span = self.last - self.first
        self.command("moveto", max(0.0, min(1.0 - span, ev.y / h - span / 2)))

    def _draw(self):
        self.delete("all")
        h = self.winfo_height()
        if h <= 1 or self.last - self.first >= 1.0:
            return                      # nothing to scroll: no thumb at all
        y0 = self.first * h
        y1 = max(self.last * h, y0 + self.MIN_THUMB)
        if y1 > h:
            y0, y1 = h - (y1 - y0), h
        r = (self.W - 4) / 2.0
        x0, x1 = 2, self.W - 2
        self.create_oval(x0, y0, x1, y0 + 2 * r, fill=self.fill, outline=self.fill)
        self.create_oval(x0, y1 - 2 * r, x1, y1, fill=self.fill, outline=self.fill)
        self.create_rectangle(x0, y0 + r, x1, y1 - r, fill=self.fill, outline=self.fill)


class SpeakUI:
    def __init__(self, root):
        self.root = root
        root.title("claude-speak")
        root.geometry("680x360")
        root.minsize(560, 240)
        root.configure(bg=BG)

        self.events = queue.Queue()
        self.sock = None
        self.sock_lock = threading.Lock()

        self.config = {"voice": "", "rate": "+10%", "volume": 100}
        self.voices = []                # edge-tts catalogue, filled on first request
        self.setpanel = None            # built lazily the first time ⚙ is opened
        self.mode = "read"              # "read" | "settings" — swapped in-window

        self.order = []                 # sids, in display order
        self.sessions = {}              # sid -> dict(label,state,can_replay,has_work,active)
        self.pills = {}                 # sid -> tk.Label pill widget
        self.selected = None            # selected sid
        self.follow = True              # auto-select the session that starts talking

        # per-session read-along state
        self.view = {}                  # sid -> dict(text,words,spans,chunks,idx,hl,t0,…)
        self._rendered = None           # (sid,body,tag) currently in the text widget

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
        """Set the window icon from the generated PNG (falls back silently)."""
        here = os.path.dirname(os.path.abspath(__file__))
        for path in (os.path.join(here, "claude-speak.png"),
                     os.path.expanduser("~/.local/share/icons/claude-speak.png")):
            if os.path.exists(path):
                try:
                    self._icon = tk.PhotoImage(file=path)   # keep ref (no GC)
                    self.root.iconphoto(True, self._icon)
                    return
                except tk.TclError:
                    continue

    # ─── widgets ────────────────────────────────────────────────────────────────

    def _build(self):
        # top: tab strip (left) + stop-all + gear (right)
        top = self.top = tk.Frame(self.root, bg=BG)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 6))

        # Gear is packed first so it is the RIGHTMOST widget: it then keeps its
        # position when "Stop all" is hidden for the settings view.
        self.gear = tk.Label(top, text="⚙", font=self.f_pill, bg=PILL_BG, fg=INK,
                             padx=12, pady=6, cursor="hand2")
        self.gear.pack(side=tk.RIGHT)
        self._hover(self.gear, PILL_BG, PILL_HI)
        self.gear.bind("<Button-1>", lambda e: self._toggle_settings())

        self.stopall = tk.Label(top, text="⏹  Stop all", font=self.f_pill,
                                bg=PILL_BG, fg=INK, padx=12, pady=6, cursor="hand2")
        self.stopall.pack(side=tk.RIGHT, padx=(0, 6))
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
        bar = self.bar = tk.Frame(self.root, bg=BG)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(6, 6))
        self.b_prev    = self._tbtn(bar, "⏪ Previous", lambda: self._nav("prev"))
        self.b_repeat  = self._tbtn(bar, "⏮ Repeat", lambda: self._nav("repeat"))
        self.b_play    = self._tbtn(bar, "⏸ Pause", self._toggle)
        self.b_skip    = self._tbtn(bar, "⏭ Skip", lambda: self.send("skip", self.selected))
        self.b_restart = self._tbtn(bar, "↺ Restart", lambda: self._nav("restart"))
        self.b_disable = self._tbtn(bar, "⊘ Disable", self._toggle_disable)

        # middle: reading card (fills whatever is left between top and bottom strips)
        card = self.card = tk.Frame(self.root, bg=BORDER)
        card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=2)
        self.text = tk.Text(card, wrap=tk.WORD, font=self.f_read, relief=tk.FLAT,
                            bg=CARD, fg=INK, padx=14, pady=12, state=tk.DISABLED,
                            cursor="arrow", highlightthickness=0, borderwidth=0,
                            selectbackground=PILL_HI, selectforeground=INK,
                            inactiveselectbackground=PILL_HI,
                            insertbackground=INK)
        self.text.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.text.tag_configure("hl", background=HL_BG, foreground=INK)
        self.text.tag_configure("spoken", foreground=SPOKEN)
        self.text.tag_configure("idle", foreground=MUTED)

        self._refresh_buttons()

    def _tbtn(self, parent, text, cmd):
        b = tk.Label(parent, text=text, font=self.f_btn, bg=BTN_BG, fg=INK,
                     padx=9, pady=7, cursor="hand2")
        b.pack(side=tk.LEFT, padx=(0, 5))
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
            b.configure(fg=DISABLED, bg=BTN_BG, cursor="arrow")

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
            dot = {"playing": "● ", "paused": "❚❚ ", "disabled": "⊘ ",
                   "ended": "", "idle": ""}.get(s.get("state"), "")
            pill = tk.Label(self.tabstrip, text=dot + label, font=self.f_pill,
                            padx=12, pady=6, cursor="hand2")
            pill.pack(side=tk.LEFT, padx=(0, 6))
            pill.bind("<Button-1>", lambda e, x=sid: self._select(x, user=True))
            pill.bind("<Button-3>", lambda e, x=sid: self._tab_menu(e, x))
            self.pills[sid] = pill
        self._paint_pills()

    def _paint_pills(self):
        for sid, pill in self.pills.items():
            if sid == self.selected:
                pill.configure(bg=ACCENT, fg=ON_ACCENT)
                pill._base = ACCENT
                pill.unbind("<Enter>"); pill.unbind("<Leave>")
            else:
                # a background session that is talking keeps the accent, as text
                talking = self.sessions.get(sid, {}).get("state") == "playing"
                pill.configure(bg=PILL_BG, fg=ACCENT if talking else INK)
                self._hover(pill, PILL_BG, PILL_HI)

    def _tab_menu(self, event, sid):
        m = dark_menu(self.root)
        m.add_command(label="Close tab", command=lambda: self._close(sid))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _close(self, sid):
        self.send("close", sid)
        self.view.pop(sid, None)

    def _select(self, sid, user=False):
        if sid not in self.sessions:
            return
        if user and self.mode == "settings":
            self._show_reading()        # picking a tab leaves settings, like a tab switch
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

    def send(self, cmd, sid=None, **extra):
        msg = {"cmd": cmd}
        if sid:
            msg["sid"] = sid
        msg.update(extra)
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

    def _nav(self, cmd):
        """Paragraph navigation. The daemon has to re-synthesize audio (~1s), so we
        move the reading card to the target paragraph right now — the text is correct
        immediately and the voice catches up when the 'play' event lands."""
        sid = self.selected
        if not sid:
            return
        self.send(cmd, sid)
        v = self.view.get(sid)
        chunks = (v or {}).get("chunks")
        if not chunks:
            return
        idx = v.get("idx", 0)
        target = {"repeat": idx, "prev": idx - 1, "restart": 0}[cmd]
        target = max(0, min(target, len(chunks) - 1))
        v.update(text=chunks[target], idx=target, words=[],
                 spans=[], t0=None, pause_accum=0.0, pause_started=None, hl=-1)
        self._refresh_text()

    def _toggle_disable(self):
        s = self.sessions.get(self.selected, {})
        if s.get("disabled"):
            self.send("enable", self.selected)
        else:
            self.send("disable", self.selected)

    # ─── settings ───────────────────────────────────────────────────────────────

    def _toggle_settings(self):
        self._show_reading() if self.mode == "settings" else self._show_settings()

    def _show_settings(self):
        """Swap the reading view for settings — same window, like switching tabs.

        The tab strip and "Stop all" belong to the sessions view, so they give up
        the top row to the voice filters; the gear stays put on the far right.
        """
        if self.setpanel is None:
            self.setpanel = SettingsPanel(self.root, self)
        self.tabstrip.pack_forget()
        self.stopall.pack_forget()
        self.card.pack_forget()
        self.bar.pack_forget()          # transport acts on sessions; not useful here
        self.setpanel.filters.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.setpanel.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=2)
        self.mode = "settings"
        self._paint_gear()
        self.send("get_config")
        if self.voices:
            self.setpanel.set_voices(self.voices)
        else:
            self.send("voices")

    def _show_reading(self):
        if self.setpanel is not None:
            self.setpanel.filters.pack_forget()
            self.setpanel.pack_forget()
        self.send("stop_preview")
        # re-pack right-to-left so "Stop all" lands back on the gear's left
        self.stopall.pack(side=tk.RIGHT, padx=(0, 6))
        self.tabstrip.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # status was packed BOTTOM first, so this lands directly above it
        self.bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(6, 6))
        self.card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=2)
        self.mode = "read"
        self._paint_gear()
        self._rendered = None           # force a repaint of the reading card
        self._refresh_text()

    def _paint_gear(self):
        on = self.mode == "settings"
        self.gear.configure(bg=ACCENT if on else PILL_BG,
                            fg=ON_ACCENT if on else INK)
        self.gear._base = ACCENT if on else PILL_BG

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
        elif kind == "config":
            self.config = ev.get("config") or self.config
            if self.setpanel is not None:
                self.setpanel.set_config(self.config)
        elif kind == "voices":
            self.voices = ev.get("voices") or []
            if self.setpanel is not None:
                self.setpanel.set_voices(self.voices, error=ev.get("error"))

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
            "chunks": ev.get("chunks") or [], "idx": ev.get("idx", 0), "hl": -1,
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
            v["hl"] = len(v.get("spans") or [])     # past the last word: all spoken
        if sid == self.selected and v:
            self._repaint(v)

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
        """Render the selected session's paragraph. Re-inserting the text drops every
        tag with it, so unchanged content is left alone — that is what keeps the
        highlight and the dimmed already-spoken words on screen while paused."""
        v = self.view.get(self.selected)
        if v and v.get("text"):
            body, tag = v["text"], None
        else:
            s = self.sessions.get(self.selected, {})
            body = {"ended": "— finished. Repeat to hear it again. —",
                    "paused": "— paused —",
                    "disabled": "— disabled. Enable to hear this session again. —"}.get(
                        s.get("state"), "— waiting for output… —")
            tag = "idle"

        key = (self.selected, body, tag)
        if key == self._rendered:
            return
        self._rendered = key

        w = self.text
        w.config(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        w.insert("1.0", body, () if tag is None else tag)
        w.config(state=tk.DISABLED)
        if tag is None and v:
            self._repaint(v)

    def _refresh_buttons(self):
        s = self.sessions.get(self.selected, {}) if self.selected else {}
        state = s.get("state", "idle")
        disabled = s.get("disabled", False)
        can_replay = s.get("can_replay", False)
        has_work = s.get("has_work", False)
        active = state in ("playing", "paused")

        if disabled:
            # a disabled session is fully silenced; only Enable is actionable
            self._set_enabled(self.b_play, False, "▶ Play")
            self._set_enabled(self.b_skip, False, "⏭ Skip")
            self._set_enabled(self.b_prev, False, "⏪ Previous")
            self._set_enabled(self.b_repeat, False, "⏮ Repeat")
            self._set_enabled(self.b_restart, False, "↺ Restart")
            self._set_enabled(self.b_disable, True, "✓ Enable")
            return

        # Pause / Resume / Play
        if state == "playing":
            self._set_enabled(self.b_play, True, "⏸ Pause")
        elif state == "paused":
            self._set_enabled(self.b_play, True, "▶ Resume")
        elif has_work:
            self._set_enabled(self.b_play, True, "▶ Play")
        else:
            self._set_enabled(self.b_play, False, "▶ Play")

        self._set_enabled(self.b_skip, active, "⏭ Skip")
        self._set_enabled(self.b_prev, can_replay, "⏪ Previous")
        self._set_enabled(self.b_repeat, can_replay, "⏮ Repeat")
        self._set_enabled(self.b_restart, can_replay, "↺ Restart")
        self._set_enabled(self.b_disable, True, "⊘ Disable")

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
        if active == v.get("hl"):
            return
        v["hl"] = active
        self._repaint(v, scroll=True)

    def _repaint(self, v, scroll=False):
        """Paint spoken/highlight tags for v's remembered word position. Safe to call
        any time — it derives everything from v, so it survives a re-render."""
        w = self.text
        w.tag_remove("hl", "1.0", tk.END)
        w.tag_remove("spoken", "1.0", tk.END)
        i = v.get("hl", -1)
        spans = v.get("spans") or []
        if i < 0:
            return
        if i >= len(spans):
            w.tag_add("spoken", "1.0", tk.END)      # paragraph finished
            return
        while i >= 0 and not spans[i]:              # unmapped token: use the last known
            i -= 1
        if i < 0:
            return
        start, end = f"1.0+{spans[i][0]}c", f"1.0+{spans[i][1]}c"
        try:
            w.tag_add("spoken", "1.0", start)
            w.tag_add("hl", start, end)
            if scroll:
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



class SettingsPanel(tk.Frame):
    """Voice / speed / volume, shown in place of the reading card.

    Every control saves as soon as you touch it — there is no OK button. Voice
    and speed are read per chunk by the daemon so they land on the next
    paragraph; volume is pushed into the running player immediately.
    """

    SAMPLE = "This is how Claude will sound with the selected voice."

    def __init__(self, parent, ui):
        super().__init__(parent, bg=BG)
        self.ui = ui
        self.all_voices = []
        self.shown = []
        self.current_voice = ui.config.get("voice", "")
        self.lang = "en"                # default filter; "" means every language
        self.gender = ""                # "" | "Male" | "Female"
        self._loading = True            # guard so painting widgets doesn't send
        self._checked = None            # row index carrying the ✓ (the live voice)
        self._shown_names = None        # rows on screen, to skip identical rebuilds

        # ─ filter row ─
        # Parented to the window's top strip, not this panel: it takes over the
        # row the session tabs vacate, so the gear stays on the same line.
        row = self.filters = tk.Frame(ui.top, bg=BG)

        self.lang_btn = self._pill(row, "English ▾", self._lang_menu)
        self.lang_btn.pack(side=tk.LEFT, padx=(0, 12))

        self.gender_btns = {}
        seg = tk.Frame(row, bg=BG)
        seg.pack(side=tk.LEFT)
        for key, text in (("", "All"), ("Male", "Male"), ("Female", "Female")):
            b = self._pill(seg, text, lambda k=key: self._set_gender(k))
            b.pack(side=tk.LEFT, padx=(0, 4))
            self.gender_btns[key] = b

        self.b_preview = self._pill(row, "🔊  Preview", self._preview)
        self.b_preview.pack(side=tk.RIGHT, padx=(0, 6))

        # ─ sliders ─
        # Packed from the BOTTOM before the voice list so they keep their space
        # when the window shrinks — the list gives up rows instead. Squeeze the
        # window far enough and pack drops them too, which is fine.
        self.vol = Slider(self, 0, 100,
                          on_change=lambda v: self.vol_lbl.config(
                              text=f"Volume  {v}"),
                          on_commit=lambda v: self._apply(volume=v))
        self.vol.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 4))
        self.vol_lbl = tk.Label(self, text="Volume  100", font=self.ui.f_pill,
                                bg=BG, fg=INK, anchor=tk.W)
        self.vol_lbl.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

        self.rate = Slider(self, -50, 100,
                           on_change=lambda v: self.rate_lbl.config(
                               text=f"Speed  {v:+d}%"),
                           on_commit=lambda v: self._apply(rate=f"{v:+d}%"))
        self.rate.pack(side=tk.BOTTOM, fill=tk.X)
        self.rate_lbl = tk.Label(self, text="Speed  +10%", font=self.ui.f_pill,
                                 bg=BG, fg=INK, anchor=tk.W)
        self.rate_lbl.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))

        # ─ voice list ─
        wrap = tk.Frame(self, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.list = tk.Listbox(wrap, font=self.ui.f_btn, relief=tk.FLAT, bd=0,
                               bg=CARD, fg=INK, selectbackground=ACCENT,
                               selectforeground=ON_ACCENT, highlightthickness=0,
                               activestyle="none", exportselection=False)
        self.scroll = SlimScroll(wrap, self.list.yview)
        self.list.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4), pady=4)
        self.list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        self.list.bind("<<ListboxSelect>>", self._on_pick)
        self.list.bind("<Double-Button-1>", lambda e: self._preview())

        # Tk's Listbox turns a held button into drag-selection plus an auto-scroll
        # repeat loop: <B1-Leave> starts tk::ListboxAutoScan, and only
        # <ButtonRelease-1> cancels it. Resizing the window is a button drag, so
        # the list can be handed a B1 event whose release goes to the window
        # manager instead — leaving that loop running forever, scrolling on its
        # own and dragging the selection down to the last row. This list only ever
        # needs click-to-pick, so refuse the drag bindings, and cancel any repeat
        # that did start as soon as the pointer comes back.
        for seq in ("<B1-Motion>", "<B1-Leave>"):
            self.list.bind(seq, lambda e: "break")
        self.list.bind("<Enter>", lambda e: self._cancel_autoscan())

        for w in (self.list, self.scroll):
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", self._wheel)
            w.bind("<Button-5>", self._wheel)

        self._paint_gender()
        self.set_config(ui.config)

    # ─── small widgets ──────────────────────────────────────────────────────────

    def _pill(self, parent, text, cmd):
        b = tk.Label(parent, text=text, font=self.ui.f_pill, bg=PILL_BG, fg=INK,
                     padx=12, pady=6, cursor="hand2")
        b._cmd = cmd
        b._enabled = True
        self.ui._hover(b, PILL_BG, PILL_HI)
        b.bind("<Button-1>", lambda e, w=b: w._cmd())
        return b

    def _cancel_autoscan(self):
        """Kill a stray tk::ListboxAutoScan repeat whose ButtonRelease never landed."""
        try:
            self.list.tk.call("tk::CancelRepeat")
        except tk.TclError:
            pass

    def _wheel(self, ev):
        step = -1 if getattr(ev, "num", 0) == 4 or getattr(ev, "delta", 0) > 0 else 1
        self.list.yview_scroll(step * 3, "units")
        return "break"

    # ─── filters ────────────────────────────────────────────────────────────────

    def _langs(self):
        """Language codes present in the catalogue, most-populated first."""
        counts = {}
        for v in self.all_voices:
            code = (v.get("locale") or "").split("-")[0].lower()
            if code:
                counts[code] = counts.get(code, 0) + 1
        return sorted(counts, key=lambda c: (LANG_NAMES.get(c, c.upper()).lower()))

    def _lang_label(self, code):
        return "All languages" if not code else LANG_NAMES.get(code, code.upper())

    def _lang_menu(self):
        m = dark_menu(self)
        m.add_command(label="All languages", command=lambda: self._set_lang(""))
        m.add_separator()
        for code in self._langs():
            m.add_command(label=self._lang_label(code),
                          command=lambda c=code: self._set_lang(c))
        try:
            m.tk_popup(self.lang_btn.winfo_rootx(),
                       self.lang_btn.winfo_rooty() + self.lang_btn.winfo_height())
        finally:
            m.grab_release()

    def _set_lang(self, code):
        self.lang = code
        self.lang_btn.config(text=f"{self._lang_label(code)} ▾")
        self._refilter()

    def _set_gender(self, g):
        self.gender = g
        self._paint_gender()
        self._refilter()

    def _paint_gender(self):
        for key, b in self.gender_btns.items():
            on = key == self.gender
            b.configure(bg=ACCENT if on else PILL_BG, fg=ON_ACCENT if on else INK)
            b._base = ACCENT if on else PILL_BG

    # ─── inbound state ──────────────────────────────────────────────────────────

    def set_config(self, cfg):
        """Take fresh config from the daemon.

        The daemon echoes a config event after every change, so this must not
        rebuild or re-scroll the list — doing so yanked the view out from under
        the pointer each time a voice was picked.
        """
        self._loading = True
        try:
            rate = int(str(cfg.get("rate", "+10%")).rstrip("%"))
        except ValueError:
            rate = 10
        vol = int(cfg.get("volume", 100))
        self.rate.set(rate, notify=False)
        self.vol.set(vol, notify=False)
        self.rate_lbl.config(text=f"Speed  {rate:+d}%")
        self.vol_lbl.config(text=f"Volume  {vol}")
        self._loading = False
        voice = cfg.get("voice", "")
        if voice != self.current_voice:
            self.current_voice = voice
            self._retick()          # move the ✓ in place; never scrolls

    def set_voices(self, voices, error=None):
        self.all_voices = voices
        if error:
            self.ui.status.config(text=f"could not load voices: {error}")
        elif not voices:
            self.ui.status.config(text="no voices returned")
        self._set_lang(self.lang)

    # ─── voice list ─────────────────────────────────────────────────────────────

    def _matches(self, v):
        if self.gender and (v.get("gender") or "") != self.gender:
            return False
        if not self.lang:
            return True
        # multilingual voices speak every language, so they belong in each filter
        if "multilingual" in v["name"].lower():
            return True
        return (v.get("locale") or "").lower().startswith(self.lang + "-")

    def _row_text(self, v):
        """A ✓ marks the voice actually in use, so the highlight can't be mistaken
        for a row that merely happens to be clicked."""
        label = v["name"]
        if "multilingual" in label.lower():
            label += "   · multi-language"
        return ("✓  " if v["name"] == self.current_voice else "      ") + label

    def _index_of(self, name):
        for i, v in enumerate(self.shown):
            if v["name"] == name:
                return i
        return None

    def _rewrite(self, *rows):
        """Redraw specific rows in place, keeping the ✓ and the view where they are."""
        self._loading = True            # rewriting rows re-fires <<ListboxSelect>>
        for i in rows:
            if i is not None and 0 <= i < len(self.shown):
                self.list.delete(i)
                self.list.insert(i, self._row_text(self.shown[i]))
        self.list.selection_clear(0, tk.END)
        if self._checked is not None:
            self.list.selection_set(self._checked)
        self._loading = False

    def _refilter(self):
        """Rebuild the rows — only for a filter change or a fresh catalogue.

        A rebuild that would produce the identical row set is skipped outright:
        any stray repeat (a reconnect, a re-open, a resize) then can't disturb
        the scroll position.
        """
        shown = [v for v in self.all_voices if self._matches(v)]
        names = [v["name"] for v in shown]
        if names == self._shown_names:
            self._retick()
            return
        self.shown, self._shown_names = shown, names
        self.list.delete(0, tk.END)
        for v in self.shown:
            self.list.insert(tk.END, self._row_text(v))
        self._checked = self._index_of(self.current_voice)
        self._loading = True
        self.list.selection_clear(0, tk.END)
        if self._checked is not None:
            self.list.selection_set(self._checked)
            self.list.update_idletasks()
            if self.list.bbox(self._checked) is None:    # only if off-screen
                self.list.see(self._checked)
        self._loading = False

    def _retick(self):
        """Move the ✓ after an outside change, without rebuilding or scrolling."""
        old, new = self._checked, self._index_of(self.current_voice)
        if old == new:
            return
        self._checked = new
        self._rewrite(old, new)

    def _on_pick(self, _ev):
        if self._loading:
            return
        sel = self.list.curselection()
        if not sel or sel[0] >= len(self.shown):
            return
        idx, old = sel[0], self._checked
        self.current_voice = self.shown[idx]["name"]
        self._checked = idx
        self._rewrite(old, idx)
        self._apply(voice=self.current_voice)

    # ─── apply (saves immediately; no OK button) ───────────────────────────────

    def _apply(self, **fields):
        if not self._loading:
            self.ui.send("set_config", **fields)

    def _preview(self):
        self.ui.send("preview", text=self.SAMPLE,
                     voice=self.current_voice or None,
                     rate=f"{self.rate.get():+d}%", volume=self.vol.get())


def main():
    # className becomes the window's WM_CLASS, which GNOME matches against the
    # .desktop's StartupWMClass to pick the dock/titlebar icon.
    root = tk.Tk(className="claude-speak")
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    SpeakUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
