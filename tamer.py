#!/usr/bin/env python3
"""Noisy-Neighbor Tamer: cap CPU hogs with SIGSTOP/SIGCONT duty cycling.

Manual mode : python3 tamer.py --pid 1234 --limit 30
Auto mode   : python3 tamer.py --auto --threshold 80 --samples 3 --budget 60
CPU % is percent of ONE core (like cpulimit). Linux only, no dependencies.
"""
import argparse
import os
import signal
import sys
import threading
import time

CLK = os.sysconf("SC_CLK_TCK")
DEFAULT_WHITELIST = {"sshd", "systemd", "Xorg", "gnome-shell", "python3", "bash", "sh"}
# NOTE: "sh"/"bash" are whitelisted by default in auto mode; use --whitelist "" to clear
# when your test burner is `sh -c 'while :; do :; done'`.


def read_stat(pid):
    """Parse /proc/<pid>/stat. Returns dict or None if the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # comm may contain spaces/parentheses, so split around the LAST ')'
    comm = data[data.index("(") + 1:data.rindex(")")]
    rest = data[data.rindex(")") + 2:].split()
    return {
        "comm": comm,
        "state": rest[0],            # field 3  (R, S, T, Z ...)
        "ppid": int(rest[1]),        # field 4
        "ticks": int(rest[11]) + int(rest[12]),  # fields 14+15: utime + stime
        "nice": int(rest[16]),       # field 19
        "start": int(rest[19]),      # field 22: start time, guards against PID reuse
    }


class Throttle(threading.Thread):
    """Duty-cycle controller for one PID with proportional feedback."""

    def __init__(self, pid, start, target, period=0.1):
        super().__init__(daemon=True)
        self.pid, self.start_time, self.target, self.period = pid, start, target, period
        self.stop_evt = threading.Event()
        self.usage = 0.0

    def alive(self):
        s = read_stat(self.pid)
        return s is not None and s["start"] == self.start_time and s["state"] not in "ZX"

    def run(self):
        frac = min(1.0, max(0.01, self.target / 100.0))
        s = read_stat(self.pid)
        last_ticks, last_t = (s["ticks"] if s else 0), time.time()
        try:
            while not self.stop_evt.is_set() and self.alive():
                run = self.period * frac
                os.kill(self.pid, signal.SIGCONT)
                self.stop_evt.wait(run)
                if frac < 1.0:
                    os.kill(self.pid, signal.SIGSTOP)
                    self.stop_evt.wait(self.period - run)
                now = time.time()
                if now - last_t >= 1.0:          # feedback once per second
                    s = read_stat(self.pid)
                    if s is None:
                        break
                    self.usage = (s["ticks"] - last_ticks) / CLK / (now - last_t) * 100
                    last_ticks, last_t = s["ticks"], now
                    frac = min(1.0, max(0.01, frac + 0.5 * (self.target - self.usage) / 100))
        except ProcessLookupError:
            pass
        finally:
            try:
                os.kill(self.pid, signal.SIGCONT)   # never leave a process stopped
            except ProcessLookupError:
                pass


workers = {}  # pid -> Throttle


def release_all(*_):
    for w in list(workers.values()):
        w.stop_evt.set()
    for w in list(workers.values()):
        w.join(timeout=1)
        try:
            os.kill(w.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    print("\nreleased all processes")
    sys.exit(0)


def manual_mode(pid, limit):
    s = read_stat(pid)
    if s is None:
        sys.exit(f"pid {pid} not found")
    w = Throttle(pid, s["start"], limit)
    workers[pid] = w
    w.start()
    print(f"limiting {pid} ({s['comm']}) to {limit}% - Ctrl+C to release")
    while w.is_alive():
        time.sleep(1)
        print(f"  measured: {w.usage:5.1f}%  target: {limit}%")


def auto_mode(threshold, samples, budget, interval, whitelist):
    me, uid = os.getpid(), os.getuid()
    prev, streak = {}, {}
    print(f"auto mode: threshold={threshold}% samples={samples} budget={budget}%")
    while True:
        now = time.time()
        seen = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in (1, me) or pid in workers:
                continue
            s = read_stat(pid)
            if s is None or s["ppid"] == 2:      # kernel threads have ppid 2
                continue
            try:
                if uid != 0 and os.stat(f"/proc/{pid}").st_uid != uid:
                    continue                      # can't signal others' processes
            except FileNotFoundError:
                continue
            if s["comm"] in whitelist:
                continue
            key = (pid, s["start"])
            seen[key] = (s["ticks"], now, s)
            if key in prev:
                pt, ptime, _ = prev[key]
                cpu = (s["ticks"] - pt) / CLK / (now - ptime) * 100
                streak[key] = streak.get(key, 0) + 1 if cpu > threshold else 0
                if streak[key] >= samples:
                    w = Throttle(pid, s["start"], budget)
                    workers[pid] = w
                    w.start()
                    print(f"hog detected: {pid} ({s['comm']}) {cpu:.0f}%")
        prev = seen
        # drop finished workers, then fair-share the budget by nice weight
        for pid in [p for p, w in workers.items() if not w.is_alive()]:
            del workers[pid]
        if workers:
            weights = {}
            for pid in workers:
                s = read_stat(pid)
                weights[pid] = max(1, 20 - s["nice"]) if s else 1
            total = sum(weights.values())
            for pid, w in workers.items():
                w.target = budget * weights[pid] / total
                print(f"  pid {pid}: target {w.target:4.1f}%  measured {w.usage:5.1f}%")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--limit", type=float, default=30, help="target CPU %% of one core")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--threshold", type=float, default=80)
    ap.add_argument("--samples", type=int, default=3, help="consecutive samples above threshold")
    ap.add_argument("--budget", type=float, default=60, help="total CPU %% shared by all hogs")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--whitelist", default=",".join(sorted(DEFAULT_WHITELIST)),
                    help="comma-separated process names to never touch")
    a = ap.parse_args()

    signal.signal(signal.SIGINT, release_all)
    signal.signal(signal.SIGTERM, release_all)
    if a.auto:
        auto_mode(a.threshold, a.samples, a.budget, a.interval,
                  {x for x in a.whitelist.split(",") if x})
    elif a.pid:
        manual_mode(a.pid, a.limit)
    else:
        ap.error("give --pid or --auto")


if __name__ == "__main__":
    main()
