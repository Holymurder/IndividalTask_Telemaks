#!/usr/bin/env python3
"""
Сервер месенджера (TCP, окремий потік на кожне підключення).

Протокол: JSON-рядки, один об'єкт на рядок (розділювач "\n"), UTF-8.
Зберігання: SQLite (користувачі з PBKDF2-хешами паролів + історія повідомлень).

Запуск:  python server.py [--host 0.0.0.0] [--port 5050] [--db messenger.db]
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time

GENERAL = "#general"          # назва спільного каналу
NAME_RE = re.compile(r"^\w{3,20}$")
MAX_TEXT = 2000               # макс. довжина повідомлення
MAX_LINE = 64 * 1024          # макс. розмір одного JSON-рядка
HISTORY_LIMIT = 100
PBKDF2_ROUNDS = 100_000

# статуси повідомлень
SENT, DELIVERED, READ = 0, 1, 2

log = logging.getLogger("server")


# --------------------------------------------------------------------------- #
#  Сховище (SQLite)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()          # SQLite-з'єднання спільне для потоків
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    name TEXT PRIMARY KEY, salt BLOB, hash BLOB, created REAL);
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT, recipient TEXT, text TEXT,
                    ts REAL, status INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_rcpt ON messages(recipient, status);
                CREATE INDEX IF NOT EXISTS idx_pair ON messages(sender, recipient);
            """)
            self.db.commit()

    @staticmethod
    def _hash(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)

    def create_user(self, name: str, password: str) -> bool:
        salt = os.urandom(16)
        h = self._hash(password, salt)               # важке обчислення — поза локом
        with self.lock:
            try:
                self.db.execute("INSERT INTO users VALUES(?,?,?,?)",
                                (name, salt, h, time.time()))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def check_user(self, name: str, password: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT salt, hash FROM users WHERE name=?",
                                  (name,)).fetchone()
        if not row:
            return False
        return hmac.compare_digest(self._hash(password, row[0]), row[1])

    def user_exists(self, name: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM users WHERE name=?",
                                   (name,)).fetchone() is not None

    def all_users(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT name FROM users ORDER BY name")]

    def add_message(self, sender, recipient, text, status=SENT):
        ts = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(sender,recipient,text,ts,status) VALUES(?,?,?,?,?)",
                (sender, recipient, text, ts, status))
            self.db.commit()
            return cur.lastrowid, ts

    def set_status(self, mid, status):
        with self.lock:
            self.db.execute("UPDATE messages SET status=? WHERE id=?", (status, mid))
            self.db.commit()

    def history(self, me, other):
        with self.lock:
            if other == GENERAL:
                rows = self.db.execute(
                    "SELECT id,sender,recipient,text,ts,status FROM messages "
                    "WHERE recipient=? ORDER BY id DESC LIMIT ?",
                    (GENERAL, HISTORY_LIMIT)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT id,sender,recipient,text,ts,status FROM messages "
                    "WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?) "
                    "ORDER BY id DESC LIMIT ?",
                    (me, other, other, me, HISTORY_LIMIT)).fetchall()
        keys = ("id", "from", "to", "text", "ts", "status")
        return [dict(zip(keys, r)) for r in reversed(rows)]

    def unread_counts(self, me) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT sender, COUNT(*) FROM messages "
                "WHERE recipient=? AND status<? GROUP BY sender", (me, READ)).fetchall()
        return dict(rows)

    def mark_delivered(self, me) -> list[str]:
        """Усі офлайн-повідомлення для me стають 'доставлено'. Повертає відправників."""
        with self.lock:
            senders = [r[0] for r in self.db.execute(
                "SELECT DISTINCT sender FROM messages WHERE recipient=? AND status=?",
                (me, SENT))]
            if senders:
                self.db.execute("UPDATE messages SET status=? WHERE recipient=? AND status=?",
                                (DELIVERED, me, SENT))
                self.db.commit()
        return senders

    def mark_read(self, me, other) -> int:
        with self.lock:
            cur = self.db.execute(
                "UPDATE messages SET status=? WHERE sender=? AND recipient=? AND status<?",
                (READ, other, me, READ))
            self.db.commit()
            return cur.rowcount


# --------------------------------------------------------------------------- #
#  Хаб: хто зараз онлайн
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.online: dict[str, list["Session"]] = {}   # ім'я -> активні сесії
        self.lock = threading.Lock()

    def sessions_of(self, user):
        with self.lock:
            return list(self.online.get(user, ()))

    def all_sessions(self):
        with self.lock:
            return [s for lst in self.online.values() for s in lst]

    def send_to(self, user, obj, exclude=None) -> int:
        n = 0
        for s in self.sessions_of(user):
            if s is not exclude:
                s.send(obj)
                n += 1
        return n

    def broadcast(self, obj, exclude=None):
        for s in self.all_sessions():
            if s is not exclude:
                s.send(obj)

    def is_online(self, user) -> bool:
        with self.lock:
            return bool(self.online.get(user))

    def join(self, session):
        with self.lock:
            lst = self.online.setdefault(session.user, [])
            first = not lst
            lst.append(session)
        if first:
            self.broadcast({"type": "presence", "user": session.user, "online": True},
                           exclude=session)

    def leave(self, session):
        if not session.user:
            return
        with self.lock:
            lst = self.online.get(session.user, [])
            if session in lst:
                lst.remove(session)
            last = not lst
            if last:
                self.online.pop(session.user, None)
        if last:
            self.broadcast({"type": "presence", "user": session.user, "online": False})


# --------------------------------------------------------------------------- #
#  Сесія одного клієнта (виконується у власному потоці)
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, hub: Hub, conn: socket.socket, addr):
        self.hub, self.store = hub, hub.store
        self.conn, self.addr = conn, addr
        self.user = None
        self.wlock = threading.Lock()         # щоб рядки різних потоків не змішувались

    # --- мережа ---
    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        try:
            with self.wlock:
                self.conn.sendall(data)
        except OSError:
            pass

    def error(self, text, **extra):
        self.send({"type": "error", "text": text, **extra})

    def run(self):
        log.info("connect %s", self.addr)
        f = self.conn.makefile("rb")
        try:
            while True:
                line = f.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    self.error("Повідомлення завелике")
                    break
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    self.error("Некоректний JSON")
                    continue
                self.dispatch(msg)
        except OSError:
            pass
        finally:
            self.hub.leave(self)
            try:
                self.conn.close()
            except OSError:
                pass
            log.info("disconnect %s (%s)", self.addr, self.user)

    # --- маршрутизація ---
    def dispatch(self, m):
        t = m.get("type")
        if t == "ping":
            self.send({"type": "pong", "t": m.get("t")})
        elif t in ("login", "register"):
            self.on_auth(m, register=(t == "register"))
        elif self.user is None:
            self.error("Спершу увійдіть в акаунт")
        elif t == "msg":
            self.on_msg(m)
        elif t == "history":
            other = m.get("with")
            self.send({"type": "history", "with": other,
                       "messages": self.store.history(self.user, other)})
        elif t == "read":
            self.on_read(m.get("with"))
        elif t == "typing":
            to = m.get("to")
            if to and to != GENERAL:
                self.hub.send_to(to, {"type": "typing", "from": self.user})
        else:
            self.error(f"Невідомий тип: {t}")

    # --- обробники ---
    def on_auth(self, m, register):
        if self.user:
            return self.error("Ви вже увійшли")
        name, pw = str(m.get("user", "")).strip(), str(m.get("password", ""))
        if register:
            if not NAME_RE.match(name):
                return self.send({"type": "auth_err",
                                  "text": "Ім'я: 3–20 символів (літери, цифри, _)"})
            if len(pw) < 4:
                return self.send({"type": "auth_err", "text": "Пароль щонайменше 4 символи"})
            if not self.store.create_user(name, pw):
                return self.send({"type": "auth_err", "text": "Таке ім'я вже зайняте"})
        elif not self.store.check_user(name, pw):
            return self.send({"type": "auth_err", "text": "Невірне ім'я або пароль"})

        self.user = name
        self.hub.join(self)
        unread = self.store.unread_counts(name)
        users = [{"name": u, "online": self.hub.is_online(u), "unread": unread.get(u, 0)}
                 for u in self.store.all_users() if u != name]
        self.send({"type": "auth_ok", "user": name, "users": users})
        # офлайн-повідомлення тепер доставлені — повідомляємо відправників
        for sender in self.store.mark_delivered(name):
            self.hub.send_to(sender, {"type": "delivered", "to": name})
        log.info("login %s", name)

    def on_msg(self, m):
        to, text, cid = m.get("to"), str(m.get("text", "")).strip(), m.get("cid")
        if not text:
            return
        if len(text) > MAX_TEXT:
            return self.error("Повідомлення задовге", cid=cid)

        if to == GENERAL:
            mid, ts = self.store.add_message(self.user, GENERAL, text)
            out = {"type": "msg", "id": mid, "from": self.user, "to": GENERAL,
                   "text": text, "ts": ts}
            self.hub.broadcast(out, exclude=self)
            self.send({"type": "ack", "cid": cid, "id": mid, "ts": ts, "status": SENT})
            return

        if not to or to == self.user or not self.store.user_exists(to):
            return self.error("Одержувача не знайдено", cid=cid)
        mid, ts = self.store.add_message(self.user, to, text)
        out = {"type": "msg", "id": mid, "from": self.user, "to": to, "text": text, "ts": ts}
        status = SENT
        if self.hub.send_to(to, out):                 # одержувач онлайн
            status = DELIVERED
            self.store.set_status(mid, DELIVERED)
        self.hub.send_to(self.user, out, exclude=self)  # копія в інші мої вкладки
        self.send({"type": "ack", "cid": cid, "id": mid, "ts": ts, "status": status})

    def on_read(self, other):
        if not other or other == GENERAL:
            return
        if self.store.mark_read(self.user, other):
            self.hub.send_to(other, {"type": "read", "by": self.user})


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Сервер месенджера")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--db", default="messenger.db")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    hub = Hub(Store(args.db))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(512)
    print(f"[STARTED] Messenger server on {args.host}:{args.port}  (db: {args.db})", flush=True)
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=Session(hub, conn, addr).run, daemon=True).start()
    except KeyboardInterrupt:
        print("\n[STOPPED]")


if __name__ == "__main__":
    main()
