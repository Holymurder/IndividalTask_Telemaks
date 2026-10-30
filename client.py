#!/usr/bin/env python3
"""
Графічний клієнт месенджера (tkinter) у стилі Telegram.

Запуск:  python client.py [--host localhost] [--port 5050]
Можна запускати кілька копій одночасно (різні акаунти) для перевірки.
Зображення лежать у папці assets/ поруч зі скриптом (без них клієнт теж працює).
"""
import argparse
import json
import math
import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

GENERAL = "#general"
PENDING, SENT, DELIVERED, READ = -1, 0, 1, 2

# ---- палітра (Telegram, світла тема) ----
BLUE, BLUE_DARK = "#3390ec", "#2b7fd1"
WALL = "#cddfbf"                      # шпалери чату
OUT_BG, IN_BG = "#effdde", "#ffffff"  # бульбашки
TXT, GRAY, LINE = "#000000", "#8e969b", "#e6e8ea"
META_IN, META_OUT = "#a0acb6", "#6cb36a"
TICK_DELIVERED, TICK_READ = "#9bc79a", "#2e9e4a"
CHIP_BG = "#7a9a6c"
AVATAR_COLORS = ["#e17076", "#faa774", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ROW_H = 68


def load_image(name):
    try:
        return tk.PhotoImage(file=os.path.join(ASSETS, name))
    except tk.TclError:
        return None


# --------------------------------------------------------------------------- #
#  Мережевий шар: читає сокет в окремому потоці, кладе події у чергу для GUI
# --------------------------------------------------------------------------- #
class Net:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.sock = None
        self.wlock = threading.Lock()

    def connect(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=4)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=self._reader, args=(self.sock,), daemon=True).start()

    def _reader(self, sock):
        try:
            for line in sock.makefile("rb"):
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        self.events.put({"type": "_closed", "sock": sock})

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        try:
            with self.wlock:
                self.sock.sendall(data)
        except (OSError, AttributeError):
            pass

    def close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


# --------------------------------------------------------------------------- #
#  Допоміжні елементи
# --------------------------------------------------------------------------- #
def rpoly(x1, y1, x2, y2, rad):
    """Контур прямокутника зі своїм радіусом для кожного кута (tl, tr, br, bl)."""
    pts = []

    def arc(cx, cy, r, a0):
        for k in range(7):
            a = math.radians(a0 + k * 15)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    tl, tr, br, bl = rad
    arc(x1 + tl, y1 + tl, tl, 180)
    arc(x2 - tr, y1 + tr, tr, 270)
    arc(x2 - br, y2 - br, br, 0)
    arc(x1 + bl, y2 - bl, bl, 90)
    return pts


def name_color(name):
    return AVATAR_COLORS[sum(map(ord, name)) % len(AVATAR_COLORS)]


def draw_avatar(c, cx, cy, r, name, logo, tag=None):
    if name == GENERAL and logo:
        c.create_image(cx, cy, image=logo, tags=tag)
        return
    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=name_color(name), outline="", tags=tag)
    c.create_text(cx, cy, text=name[:1].upper(), fill="white", tags=tag,
                  font=(tkfont.nametofont("TkDefaultFont").actual("family"), int(r * 0.78), "bold"))


def ellipsize(text, font, maxw):
    text = text.replace("\n", " ")
    if font.measure(text) <= maxw:
        return text
    while text and font.measure(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


def day_label(ts):
    t = time.localtime(ts)
    today = time.localtime()
    d = (time.mktime(time.struct_time((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1)))
         - time.mktime(time.struct_time((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))) / 86400
    if round(d) == 0:
        return "Сьогодні"
    if round(d) == 1:
        return "Вчора"
    s = f"{t.tm_mday} {MONTHS[t.tm_mon - 1]}"
    return s if t.tm_year == today.tm_year else f"{s} {t.tm_year}"


def short_time(ts):
    t, now = time.localtime(ts), time.localtime()
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%d.%m", t)


class PEntry(tk.Entry):
    """Entry із сірим текстом-підказкою."""

    def __init__(self, master, placeholder, **kw):
        super().__init__(master, **kw)
        self.ph, self.on, self.fg = placeholder, False, kw.get("fg", TXT)
        self.bind("<FocusIn>", self._in)
        self.bind("<FocusOut>", self._out)
        self._out()

    def _in(self, _=None):
        if self.on:
            super().delete(0, "end")
            self.config(fg=self.fg)
            self.on = False

    def _out(self, _=None):
        if not super().get():
            super().insert(0, self.ph)
            self.config(fg="#a0a7ad")
            self.on = True

    def text(self):
        return "" if self.on else super().get()

    def clear(self):
        super().delete(0, "end")
        self.on = False
        self.config(fg=self.fg)
        if self.focus_get() is not self:
            self._out()


def flat_button(parent, text, cmd, primary=True):
    fam = tkfont.nametofont("TkDefaultFont").actual("family")
    b = tk.Label(parent, text=text, font=(fam, 11, "bold"), pady=10, cursor="hand2",
                 bg=BLUE if primary else "white", fg="white" if primary else BLUE)
    b.bind("<Button-1>", lambda e: cmd())
    if primary:
        b.bind("<Enter>", lambda e: b.config(bg=BLUE_DARK))
        b.bind("<Leave>", lambda e: b.config(bg=BLUE))
    return b


def bind_wheel(widget, handler):
    widget.bind("<MouseWheel>", lambda e: handler(
        -e.delta / 120 * 56 if abs(e.delta) >= 120 else -e.delta * 8))
    widget.bind("<Button-4>", lambda e: handler(-56))
    widget.bind("<Button-5>", lambda e: handler(56))


# --------------------------------------------------------------------------- #
#  Список чатів (ліва панель)
# --------------------------------------------------------------------------- #
class ChatList(tk.Canvas):
    def __init__(self, master, app):
        super().__init__(master, bg="white", highlightthickness=0)
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.app, self.off, self.rows = app, 0, []
        self.f_name = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_prev = tkfont.Font(family=fam, size=10)
        self.f_time = tkfont.Font(family=fam, size=9)
        self.f_badge = tkfont.Font(family=fam, size=9, weight="bold")
        self.bind("<Configure>", lambda e: self.render())
        self.bind("<Button-1>", self.click)
        bind_wheel(self, self.scroll)

    def scroll(self, d):
        self.off = max(0, min(self.off + d, max(0, len(self.rows) * ROW_H - self.winfo_height())))
        self.render()

    def click(self, e):
        i = int((e.y + self.off) // ROW_H)
        if 0 <= i < len(self.rows):
            self.app.open_chat(self.rows[i])

    def render(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        app = self.app
        self.rows = app.chat_rows()
        self.off = max(0, min(self.off, max(0, len(self.rows) * ROW_H - H)))
        for i, name in enumerate(self.rows):
            y = i * ROW_H - self.off
            if y > H or y + ROW_H < 0:
                continue
            self.row(name, y, W)

    def row(self, name, y, W):
        app = self.app
        info = app.info(name)
        sel = name == app.current
        fg, sub, tm = ("white", "#e3f0fc", "#e3f0fc") if sel else (TXT, GRAY, GRAY)
        if sel:
            self.create_rectangle(0, y, W, y + ROW_H, fill=BLUE, outline="")
        cx, cy = 38, y + ROW_H // 2
        draw_avatar(self, cx, cy, 25, name, app.logo)
        if name != GENERAL and info["online"]:
            self.create_oval(cx + 13, cy + 13, cx + 25, cy + 25, fill="#4dcd5e",
                             outline="#3390ec" if sel else "white", width=2)
        last = info.get("last")
        title = "Загальний чат" if name == GENERAL else name
        # час і лічильник праворуч
        right = W - 14
        if last:
            t = short_time(last["ts"])
            self.create_text(right, y + 14, text=t, anchor="ne", font=self.f_time, fill=tm)
            tw = self.f_time.measure(t)
        else:
            tw = 0
        badge_w = 0
        if info["unread"]:
            s = str(info["unread"] if info["unread"] < 1000 else "999+")
            badge_w = max(22, self.f_badge.measure(s) + 14)
            self.create_polygon(rpoly(right - badge_w, y + 38, right, y + 60, (11, 11, 11, 11)),
                                fill="white" if sel else BLUE, outline="")
            self.create_text(right - badge_w / 2, y + 49, text=s, font=self.f_badge,
                             fill=BLUE if sel else "white")
        self.create_text(76, y + 13, text=ellipsize(title, self.f_name, W - 76 - tw - 24),
                         anchor="nw", font=self.f_name, fill=fg)
        # попередній перегляд
        if last:
            who = "Ви: " if last["from"] == app.me else (f"{last['from']}: " if name == GENERAL else "")
            prev = who + last["text"]
        else:
            prev = "Спільний канал" if name == GENERAL else "Немає повідомлень"
        self.create_text(76, y + 38, text=ellipsize(prev, self.f_prev, W - 76 - 14 - (badge_w + 8 if badge_w else 0)),
                         anchor="nw", font=self.f_prev, fill=sub)


# --------------------------------------------------------------------------- #
#  Вікно повідомлень: бульбашки на шпалерах із напівпрозорим логотипом
# --------------------------------------------------------------------------- #
class ChatView(tk.Canvas):
    PADX, GAP_SAME, GAP_DIFF = 11, 2, 8

    def __init__(self, master, app):
        super().__init__(master, bg=WALL, highlightthickness=0)
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.app = app
        self.msgs, self.group = [], False
        self.off, self.maxoff, self.stick = 0, 0, True
        self.f_body = tkfont.Font(family=fam, size=11)
        self.f_meta = tkfont.Font(family=fam, size=8)
        self.f_name = tkfont.Font(family=fam, size=10, weight="bold")
        self.f_chip = tkfont.Font(family=fam, size=9, weight="bold")
        self.lh = self.f_body.metrics("linespace")
        self.wm_big = load_image("wallpaper_logo.png")
        self.wm_small = load_image("wallpaper_logo_s.png")
        self.bind("<Configure>", lambda e: self.render())
        bind_wheel(self, self.scroll)

    def show(self, msgs, group):
        self.msgs, self.group = msgs, group
        self.render()

    def scroll(self, d):
        self.off = max(0, min(self.off + d, self.maxoff))
        self.stick = self.off >= self.maxoff - 2
        self.render()

    # --- розбиття тексту на рядки
    def wrap(self, m, maxpx):
        k = m.get("_w")
        if k and k[0] == maxpx and k[1] == m["text"]:
            return k[2], k[3]
        f, lines = self.f_body, []
        for para in m["text"].split("\n"):
            cur = ""
            for word in para.split(" "):
                cand = word if not cur else cur + " " + word
                if f.measure(cand) <= maxpx:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                while f.measure(word) > maxpx:         # надто довге слово
                    n = len(word)
                    while n > 1 and f.measure(word[:n]) > maxpx:
                        n -= 1
                    lines.append(word[:n])
                    word = word[n:]
                cur = word
            lines.append(cur)
        widths = [f.measure(l) for l in lines]
        m["_w"] = (maxpx, m["text"], lines, widths)
        return lines, widths

    def chip(self, cx, y, text):
        w = self.f_chip.measure(text) + 22
        self.create_polygon(rpoly(cx - w / 2, y, cx + w / 2, y + 24, (12, 12, 12, 12)),
                            fill=CHIP_BG, outline="")
        self.create_text(cx, y + 12, text=text, font=self.f_chip, fill="white")

    def tick(self, m):
        s = m["status"]
        if s == PENDING:
            return "◷", META_OUT
        if self.group or s == SENT:
            return "✓", META_OUT
        return "✓✓", TICK_READ if s == READ else TICK_DELIVERED

    def render(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 60 or H < 60:
            return
        wm = self.wm_big if H > 520 else self.wm_small
        if wm:
            self.create_image(W // 2, H // 2, image=wm)
        if not self.msgs:
            self.chip(W / 2, H / 2 - 12, "Повідомлень поки немає. Напишіть перше!")
            return

        cl = max(12, (W - 780) // 2)
        cr = W - cl
        maxtext = min(int((cr - cl) * 0.78), 480) - 2 * self.PADX
        me, lh = self.app.me, self.lh
        ops, y, prev_day, prev_from = [], 0, None, None
        n = len(self.msgs)
        for i, m in enumerate(self.msgs):
            day = time.strftime("%Y-%m-%d", time.localtime(m["ts"]))
            if day != prev_day:
                ops.append(("chip", y + 6, day_label(m["ts"])))
                y += 6 + 24 + 8
                prev_from = None
            prev_day = day
            mine = m["from"] == me
            lines, widths = self.wrap(m, maxtext)
            show_name = self.group and not mine
            name_h = 17 if show_name else 0
            tm = time.strftime("%H:%M", time.localtime(m["ts"]))
            tk_txt, tk_col = self.tick(m) if mine else ("", META_IN)
            tick_w = self.f_meta.measure(tk_txt) + 4 if tk_txt else 0
            meta_w = self.f_meta.measure(tm) + tick_w
            if widths[-1] + 8 + meta_w <= maxtext:
                tw, extra = max(max(widths), widths[-1] + 8 + meta_w), 0
            else:
                tw, extra = max(max(widths), meta_w), 13
            if show_name:
                tw = max(tw, self.f_name.measure(m["from"]))
            bw = tw + 2 * self.PADX
            bh = 6 + name_h + len(lines) * lh + extra + 6
            nxt = self.msgs[i + 1] if i + 1 < n else None
            last_in_series = (nxt is None or nxt["from"] != m["from"] or
                              time.strftime("%Y-%m-%d", time.localtime(nxt["ts"])) != day)
            gap = self.GAP_SAME if (nxt and not last_in_series) else self.GAP_DIFF
            ops.append(("msg", y, m, mine, lines, bw, bh, show_name, name_h, tm, tk_txt, tk_col,
                        tick_w, last_in_series))
            y += bh + gap
            prev_from = m["from"]

        content = y + 8
        if content <= H:
            base, self.maxoff, self.off = H - content + 8, 0, 0
        else:
            self.maxoff = content - H
            if self.stick:
                self.off = self.maxoff
            self.off = max(0, min(self.off, self.maxoff))
            base = 8 - self.off

        for op in ops:
            yy = base + op[1]
            if yy > H + 200 or yy < -400:
                continue
            if op[0] == "chip":
                self.chip(W / 2, yy, op[2])
                continue
            (_, _, m, mine, lines, bw, bh, show_name, name_h, tm, tk_txt, tk_col,
             tick_w, tail) = op
            x1 = cr - bw if mine else cl
            x2, y2 = x1 + bw, yy + bh
            rad = [14, 14, 14, 14]
            if tail:
                rad[2 if mine else 3] = 3
            self.create_polygon(rpoly(x1, yy, x2, y2, rad), fill=OUT_BG if mine else IN_BG, outline="")
            ty = yy + 6
            if show_name:
                self.create_text(x1 + self.PADX, ty, text=m["from"], anchor="nw",
                                 font=self.f_name, fill=name_color(m["from"]))
                ty += name_h
            for line in lines:
                self.create_text(x1 + self.PADX, ty, text=line, anchor="nw",
                                 font=self.f_body, fill=TXT)
                ty += self.lh
            mx = x2 - self.PADX
            if tk_txt:
                self.create_text(mx, y2 - 5, text=tk_txt, anchor="se", font=self.f_meta, fill=tk_col)
            self.create_text(mx - tick_w, y2 - 5, text=tm, anchor="se", font=self.f_meta,
                             fill=META_OUT if mine else META_IN)


# --------------------------------------------------------------------------- #
#  Застосунок
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self, host, port):
        super().__init__()
        self.title("Messenger")
        self.geometry("1000x680")
        self.minsize(760, 480)
        self.fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.events = queue.Queue()
        self.net = Net(self.events)
        self.host, self.port = host, port
        self.logo = load_image("logo.png")
        self.logo_big = load_image("logo_big.png")

        self.me = None
        self.contacts: dict[str, dict] = {}     # ім'я -> {"online", "unread", "last"}
        self.gen = {"online": True, "unread": 0, "last": None}   # загальний канал
        self.chats: dict[str, list] = {}
        self.loaded: set[str] = set()
        self.current = None
        self.cid = 0
        self.filter = ""
        self.last_typing_sent = 0.0
        self.typing_job = None
        self.main = None
        self.build_login()
        self.after(40, self.poll)

    def info(self, name):
        return self.gen if name == GENERAL else self.contacts[name]

    def chat_rows(self):
        f = self.filter.lower()
        names = [n for n in self.contacts if f in n.lower()]
        names.sort(key=lambda n: (0 if self.contacts[n]["last"] else 1,
                                  -(self.contacts[n]["last"]["ts"] if self.contacts[n]["last"] else 0),
                                  not self.contacts[n]["online"], n.lower()))
        general = [GENERAL] if (not f or f in "загальний чат") else []
        return general + names

    # ------------------------------------------------------------------ вхід
    def build_login(self):
        self.login = tk.Frame(self, bg="white")
        self.login.pack(fill="both", expand=True)
        box = tk.Frame(self.login, bg="white")
        box.place(relx=.5, rely=.5, anchor="center")
        if self.logo_big:
            tk.Label(box, image=self.logo_big, bg="white").pack()
        tk.Label(box, text="Messenger", font=(self.fam, 24, "bold"), bg="white").pack(pady=(10, 2))
        tk.Label(box, text="Увійдіть або створіть новий акаунт", fg=GRAY,
                 bg="white").pack(pady=(0, 14))
        self.e_host = self._field(box, "Сервер", f"{self.host}:{self.port}")
        self.e_user = self._field(box, "Ім'я користувача", "")
        self.e_pass = self._field(box, "Пароль", "", show="•")
        self.lbl_err = tk.Label(box, text="", fg="#d14b4b", bg="white", wraplength=300)
        self.lbl_err.pack(pady=(6, 0))
        flat_button(box, "УВІЙТИ", lambda: self.try_auth("login")).pack(fill="x", pady=(10, 4))
        flat_button(box, "СТВОРИТИ АКАУНТ", lambda: self.try_auth("register"),
                    primary=False).pack(fill="x")
        self.e_user.bind("<Return>", lambda e: self.e_pass.focus_set())
        self.e_pass.bind("<Return>", lambda e: self.try_auth("login"))
        self.e_user.focus_set()

    def _field(self, parent, label, value, show=None):
        tk.Label(parent, text=label, bg="white", fg=GRAY, anchor="w",
                 font=(self.fam, 9)).pack(fill="x", pady=(8, 0))
        e = tk.Entry(parent, width=34, show=show, font=(self.fam, 12), relief="flat", bd=0,
                     highlightthickness=1, highlightbackground="#d5d8dc", highlightcolor=BLUE)
        e.insert(0, value)
        e.pack(fill="x", ipady=7, pady=(2, 0))
        return e

    def try_auth(self, kind):
        self.lbl_err.config(text="")
        try:
            host, _, port = self.e_host.get().strip().rpartition(":")
            self.net.close()
            self.net.connect(host or "localhost", int(port))
        except (ValueError, OSError) as e:
            self.lbl_err.config(text=f"Не вдалося підключитись: {e}")
            return
        self.net.send({"type": kind, "user": self.e_user.get().strip(),
                       "password": self.e_pass.get()})

    # ------------------------------------------------------------ головне вікно
    def build_main(self):
        self.login.destroy()
        self.main = tk.Frame(self, bg="white")
        self.main.pack(fill="both", expand=True)

        # ліва панель
        left = tk.Frame(self.main, bg="white", width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        top = tk.Frame(left, bg="white", height=58)
        top.pack(fill="x")
        top.pack_propagate(False)
        av = tk.Canvas(top, width=46, height=58, bg="white", highlightthickness=0)
        av.pack(side="left", padx=(8, 0))
        draw_avatar(av, 23, 29, 19, self.me, None)
        box = tk.Frame(top, bg="#f1f3f4")
        box.pack(side="left", fill="x", expand=True, padx=(4, 12), pady=10)
        self.search = PEntry(box, "Пошук", font=(self.fam, 11), relief="flat", bd=0,
                             bg="#f1f3f4", highlightthickness=0)
        self.search.pack(fill="x", padx=12, ipady=6)
        self.search.bind("<KeyRelease>", self.on_search)
        self.list = ChatList(left, self)
        self.list.pack(fill="both", expand=True)
        self.lbl_rtt = tk.Label(left, text="● підключено", bg="white", fg=GRAY, anchor="w",
                                padx=14, pady=5, font=(self.fam, 9))
        self.lbl_rtt.pack(fill="x")
        tk.Frame(self.main, bg=LINE, width=1).pack(side="left", fill="y")

        # права панель
        right = tk.Frame(self.main, bg=WALL)
        right.pack(side="left", fill="both", expand=True)
        head = tk.Frame(right, bg="white", height=58)
        head.pack(fill="x")
        head.pack_propagate(False)
        self.h_av = tk.Canvas(head, width=62, height=58, bg="white", highlightthickness=0)
        self.h_av.pack(side="left")
        txt = tk.Frame(head, bg="white")
        txt.pack(side="left", fill="y", pady=9)
        self.lbl_title = tk.Label(txt, text="", bg="white", anchor="w",
                                  font=(self.fam, 12, "bold"))
        self.lbl_title.pack(fill="x")
        self.lbl_sub = tk.Label(txt, text="", bg="white", anchor="w", fg=GRAY, font=(self.fam, 9))
        self.lbl_sub.pack(fill="x")
        tk.Frame(right, bg=LINE, height=1).pack(fill="x")

        bar = tk.Frame(right, bg="white", height=62)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self.view = ChatView(right, self)
        self.view.pack(fill="both", expand=True)

        self.entry = PEntry(bar, "Повідомлення", font=(self.fam, 12), relief="flat", bd=0,
                            highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=(20, 8), ipady=8)
        self.entry.bind("<Return>", self.send_current)
        self.entry.bind("<Key>", self.on_key)
        btn = tk.Canvas(bar, width=50, height=62, bg="white", highlightthickness=0, cursor="hand2")
        btn.pack(side="right", padx=(0, 8))
        btn.create_oval(5, 12, 45, 52, fill=BLUE, outline="")
        btn.create_polygon(15, 24, 36, 32, 15, 40, 19, 32, fill="white", outline="")
        btn.bind("<Button-1>", lambda e: self.send_current())

        self.open_chat(GENERAL)
        self.entry.focus_set()
        self.ping()

    def on_search(self, _):
        self.filter = self.search.text().strip()
        self.list.off = 0
        self.list.render()

    def open_chat(self, name):
        self.current = name
        self.info(name)["unread"] = 0
        self.view.stick = True
        if name not in self.loaded:
            self.net.send({"type": "history", "with": name})
        else:
            self.mark_read(name)
        self.update_header()
        self.list.render()
        self.render()
        if hasattr(self, "entry"):
            self.entry.focus_set()

    def update_header(self, typing=False):
        c = self.h_av
        c.delete("all")
        draw_avatar(c, 31, 29, 21, self.current, self.logo)
        if self.current == GENERAL:
            members = len(self.contacts) + 1
            online = sum(1 for x in self.contacts.values() if x["online"]) + 1
            self.lbl_title.config(text="Загальний чат")
            self.lbl_sub.config(text=f"{members} учасників, {online} в мережі", fg=GRAY)
        else:
            self.lbl_title.config(text=self.current)
            if typing:
                self.lbl_sub.config(text="друкує…", fg=BLUE)
            elif self.contacts[self.current]["online"]:
                self.lbl_sub.config(text="в мережі", fg=BLUE)
            else:
                self.lbl_sub.config(text="не в мережі", fg=GRAY)

    def render(self):
        self.view.show(self.chats.get(self.current, []), self.current == GENERAL)

    def bump_last(self, chat, m):
        self.info(chat)["last"] = {"from": m["from"], "text": m["text"], "ts": m["ts"]}

    # --------------------------------------------------------------- відправка
    def send_current(self, _=None):
        text = self.entry.text().strip()
        if not text or not self.current:
            return
        self.cid += 1
        m = {"cid": self.cid, "id": None, "from": self.me, "to": self.current,
             "text": text, "ts": time.time(), "status": PENDING}
        self.chats.setdefault(self.current, []).append(m)
        self.bump_last(self.current, m)
        self.net.send({"type": "msg", "to": self.current, "text": text, "cid": self.cid})
        self.entry.clear()
        self.view.stick = True
        self.render()
        self.list.render()

    def on_key(self, e):
        if self.current and self.current != GENERAL and len(e.keysym) > 0 \
                and e.keysym not in ("Return", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            now = time.time()
            if now - self.last_typing_sent > 2:
                self.last_typing_sent = now
                self.net.send({"type": "typing", "to": self.current})

    def mark_read(self, chat):
        if chat != GENERAL:
            self.net.send({"type": "read", "with": chat})

    def ping(self):
        if self.me and self.net.sock:
            self.net.send({"type": "ping", "t": time.perf_counter()})
            self.after(5000, self.ping)

    # --------------------------------------------------- обробка подій від сервера
    def poll(self):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(40, self.poll)

    def handle(self, ev):
        h = getattr(self, "ev_" + ev.get("type", ""), None)
        if h:
            h(ev)

    def ev_auth_ok(self, ev):
        self.me = ev["user"]
        self.contacts = {u["name"]: {"online": u["online"], "unread": u["unread"],
                                     "last": u.get("last")} for u in ev["users"]}
        self.gen["last"] = ev.get("general_last")
        self.build_main()

    def ev_auth_err(self, ev):
        self.lbl_err.config(text=ev["text"])
        self.net.close()

    def ev_error(self, ev):
        if self.me:
            self.lbl_sub.config(text=f"⚠ {ev['text']}", fg="#d14b4b")
        else:
            self.lbl_err.config(text=ev["text"])

    def ev_presence(self, ev):
        c = self.contacts.setdefault(ev["user"], {"online": False, "unread": 0, "last": None})
        c["online"] = ev["online"]
        self.list.render()
        self.update_header()

    def ev_msg(self, ev):
        mine = ev["from"] == self.me
        chat = ev["to"] if (ev["to"] == GENERAL or mine) else ev["from"]
        if chat != GENERAL and chat not in self.contacts:
            self.contacts[chat] = {"online": True, "unread": 0, "last": None}
        ev["status"] = SENT if mine else DELIVERED
        if chat in self.loaded or chat == self.current:
            self.chats.setdefault(chat, []).append(ev)
        self.bump_last(chat, ev)
        if chat == self.current:
            self.render()
            if not mine:
                self.mark_read(chat)
        elif not mine:
            self.info(chat)["unread"] += 1
            self.bell()
        self.list.render()

    def ev_ack(self, ev):
        for msgs in self.chats.values():
            for m in reversed(msgs):
                if m.get("cid") == ev["cid"] and m["id"] is None:
                    m.update(id=ev["id"], ts=ev["ts"], status=ev["status"])
                    break
        self.render()

    def ev_delivered(self, ev):
        for m in self.chats.get(ev["to"], []):
            if m["from"] == self.me and m["status"] == SENT:
                m["status"] = DELIVERED
        self.render()

    def ev_read(self, ev):
        for m in self.chats.get(ev["by"], []):
            if m["from"] == self.me:
                m["status"] = READ
        if ev["by"] == self.current:
            self.render()

    def ev_history(self, ev):
        chat = ev["with"]
        self.chats[chat] = ev["messages"]
        self.loaded.add(chat)
        if ev["messages"]:
            self.bump_last(chat, ev["messages"][-1])
        if chat == self.current:
            self.view.stick = True
            self.render()
            self.mark_read(chat)
        self.list.render()

    def ev_typing(self, ev):
        if ev["from"] == self.current:
            self.update_header(typing=True)
            if self.typing_job:
                self.after_cancel(self.typing_job)
            self.typing_job = self.after(2500, self.update_header)

    def ev_pong(self, ev):
        rtt = (time.perf_counter() - ev["t"]) * 1000
        self.lbl_rtt.config(text=f"● підключено · RTT {rtt:.1f} мс")

    def ev__closed(self, ev):
        if ev.get("sock") is not self.net.sock and self.net.sock is not None:
            return                       # це закритий старий сокет
        if self.me:
            messagebox.showwarning("Messenger", "З'єднання з сервером втрачено")
            self.me = None
            self.net.close()
            self.main.destroy()
            self.chats.clear()
            self.loaded.clear()
            self.current = None
            self.build_login()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Клієнт месенджера")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5050)
    a = ap.parse_args()
    App(a.host, a.port).mainloop()