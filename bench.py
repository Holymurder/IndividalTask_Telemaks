#!/usr/bin/env python3
"""
Навантажувальний тест месенджера.

N віртуальних клієнтів підключаються, логіняться й по кільцю шлють одне одному
особисті повідомлення (клієнт i -> клієнт i+1). Вимірюється наскрізна затримка
"відправка -> отримання одержувачем" (включно із записом в SQLite на сервері).

Запуск (сервер має бути запущений):
    python bench.py --clients 10 50 100 200 --msgs 20 --rate 10
"""
import argparse
import json
import socket
import statistics
import threading
import time


class Bot:
    def __init__(self, host, port, name):
        self.name = name
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.f = self.sock.makefile("rb")
        self.latencies: list[float] = []
        self.got = 0

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def login(self):
        t0 = time.perf_counter()
        self.send({"type": "register", "user": self.name, "password": "benchpass"})
        while True:
            m = json.loads(self.f.readline())
            if m["type"] == "auth_err":                     # вже існує -> звичайний вхід
                self.send({"type": "login", "user": self.name, "password": "benchpass"})
            elif m["type"] == "auth_ok":
                return time.perf_counter() - t0

    def reader(self):
        try:
            for line in self.f:
                m = json.loads(line)
                if m["type"] == "msg" and m["to"] == self.name:
                    sent_at = float(m["text"].split("|", 1)[0])
                    self.latencies.append(time.perf_counter() - sent_at)
                    self.got += 1
        except (OSError, ValueError):
            pass

    def run_send(self, to, count, rate):
        pad = "x" * 40
        for _ in range(count):
            self.send({"type": "msg", "to": to, "text": f"{time.perf_counter()!r}|{pad}"})
            time.sleep(1 / rate)


def run(host, port, n, msgs, rate, tag):
    bots = [Bot(host, port, f"b{tag}_{i}") for i in range(n)]
    login_times: list[float] = []
    lt_lock = threading.Lock()

    def do_login(b):
        d = b.login()
        with lt_lock:
            login_times.append(d)
    ths = [threading.Thread(target=do_login, args=(b,)) for b in bots]
    [t.start() for t in ths]; [t.join() for t in ths]

    readers = [threading.Thread(target=b.reader, daemon=True) for b in bots]
    [t.start() for t in readers]

    t0 = time.perf_counter()
    senders = [threading.Thread(target=b.run_send, args=(bots[(i + 1) % n].name, msgs, rate))
               for i, b in enumerate(bots)]
    [t.start() for t in senders]; [t.join() for t in senders]
    time.sleep(1.5)                                       # дочекатись хвоста доставки
    elapsed = time.perf_counter() - t0 - 1.5

    lat = sorted(x for b in bots for x in b.latencies)
    sent, got = n * msgs, len(lat)
    for b in bots:
        b.sock.close()

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] * 1000 if lat else float("nan")
    return {
        "clients": n, "sent": sent, "recv": got,
        "loss%": 100 * (sent - got) / sent,
        "login_ms": statistics.mean(login_times) * 1000,
        "avg_ms": statistics.mean(lat) * 1000 if lat else float("nan"),
        "p95_ms": pct(.95), "max_ms": lat[-1] * 1000 if lat else float("nan"),
        "msg/s": got / elapsed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--clients", type=int, nargs="+", default=[10, 50, 100, 200])
    ap.add_argument("--msgs", type=int, default=20, help="повідомлень від кожного клієнта")
    ap.add_argument("--rate", type=float, default=10, help="повідомлень/с від кожного клієнта")
    a = ap.parse_args()

    cols = ["clients", "sent", "recv", "loss%", "login_ms", "avg_ms", "p95_ms", "max_ms", "msg/s"]
    print(" | ".join(f"{c:>9}" for c in cols))
    print("-+-".join("-" * 9 for _ in cols))
    for n in a.clients:
        r = run(a.host, a.port, n, a.msgs, a.rate, tag=str(int(time.time()) % 100000))
        print(" | ".join(f"{r[c]:>9.1f}" if isinstance(r[c], float) else f"{r[c]:>9}"
                         for c in cols), flush=True)


if __name__ == "__main__":
    main()
