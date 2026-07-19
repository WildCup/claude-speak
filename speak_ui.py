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
        root.geometry("680x360")
        root.minsize(560, 240)
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
        self.b_prev    = self._tbtn(bar, "⏪ Previous", lambda: self._nav("prev"))
        self.b_repeat  = self._tbtn(bar, "⏮ Repeat", lambda: self._nav("repeat"))
        self.b_play    = self._tbtn(bar, "⏸ Pause", self._toggle)
        self.b_skip    = self._tbtn(bar, "⏭ Skip", lambda: self.send("skip", self.selected))
        self.b_restart = self._tbtn(bar, "↺ Restart", lambda: self._nav("restart"))
        self.b_disable = self._tbtn(bar, "⊘ Disable", self._toggle_disable)

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
                pill.configure(bg=ACCENT, fg="#ffffff")
                pill._base = ACCENT
                pill.unbind("<Enter>"); pill.unbind("<Leave>")
            else:
                pill.configure(bg=PILL_BG, fg=INK)
                self._hover(pill, PILL_BG, PILL_HI)

    def _tab_menu(self, event, sid):
        m = tk.Menu(self.root, tearoff=0)
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
