"""Streamlit frontend for the Noisy-Neighbor Tamer.

Run (in the same folder as tamer.py, on Linux / WSL2):
    pip install streamlit
    streamlit run app.py

It imports tamer.py and reuses read_stat() and Throttle, so the throttling
logic is exactly the one from the CLI. Auto mode is re-implemented as a
background thread (the CLI version is a blocking loop).
"""
import os
import signal
import threading
import time
from collections import deque

import pandas as pd
import streamlit as st

import tamer

st.set_page_config(page_title="Noisy-Neighbor Tamer", page_icon="🐢", layout="wide")

if not os.path.isdir("/proc"):
    st.error("This tool needs Linux /proc. Run it inside WSL2 / Ubuntu.")
    st.stop()

CLK = tamer.CLK


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.workers = tamer.workers          # pid -> Throttle (shared with tamer module)
        self.prev = {}                        # (pid, start) -> (ticks, time)
        self.history = {}                     # pid -> deque[(t, measured, target)]
        self.auto_pids = set()
        self.auto_thread = None
        self.auto_stop = threading.Event()
        self.auto_cfg = {}
        self.log = deque(maxlen=200)
        self.me = os.getpid()
        self.uid = os.getuid()

    # ---- helpers ---------------------------------------------------------
    def _log(self, msg):
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _eligible(self, pid):
        """Return stat dict if this pid may be shown/throttled, else None."""
        if pid in (1, self.me):
            return None
        s = tamer.read_stat(pid)
        if s is None or s["ppid"] == 2:
            return None
        try:
            if self.uid != 0 and os.stat(f"/proc/{pid}").st_uid != self.uid:
                return None
        except FileNotFoundError:
            return None
        return s

    def cleanup(self):
        with self.lock:
            for pid in [p for p, w in self.workers.items() if not w.is_alive()]:
                del self.workers[pid]
                self.auto_pids.discard(pid)
                self.history.pop(pid, None)

    # ---- process snapshot for the dashboard --------------------------------
    def snapshot(self):
        now = time.time()
        rows, seen = [], {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            s = self._eligible(pid)
            if s is None:
                continue
            key = (pid, s["start"])
            seen[key] = (s["ticks"], now)
            cpu = 0.0
            if key in self.prev:
                pt, ptime = self.prev[key]
                if now > ptime:
                    cpu = (s["ticks"] - pt) / CLK / (now - ptime) * 100
            rows.append({
                "PID": pid, "Name": s["comm"], "State": s["state"],
                "Nice": s["nice"], "CPU %": round(max(cpu, 0.0), 1),
                "Throttled": pid in self.workers,
            })
        self.prev = seen
        return rows

    # ---- throttling ------------------------------------------------------
    def limit(self, pid, pct, auto=False):
        with self.lock:
            if pid in self.workers:
                self.workers[pid].target = pct
                return True, f"updated target of {pid} to {pct:.0f}%"
            s = tamer.read_stat(pid)
            if s is None:
                return False, f"pid {pid} not found"
            w = tamer.Throttle(pid, s["start"], pct)
            self.workers[pid] = w
            w.start()
            if auto:
                self.auto_pids.add(pid)
            self._log(f"limiting {pid} ({s['comm']}) to {pct:.0f}%")
            return True, f"limiting {pid} ({s['comm']}) to {pct:.0f}%"

    def release(self, pid):
        with self.lock:
            w = self.workers.pop(pid, None)
            self.auto_pids.discard(pid)
            self.history.pop(pid, None)
        if w:
            w.stop_evt.set()
            w.join(timeout=1)
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self._log(f"released {pid}")

    def release_all(self):
        self.stop_auto()
        for pid in list(self.workers):
            self.release(pid)

    def record_history(self):
        t = time.time()
        for pid, w in list(self.workers.items()):
            h = self.history.setdefault(pid, deque(maxlen=120))
            h.append((t, w.usage, w.target))

    # ---- auto mode -------------------------------------------------------
    @property
    def auto_running(self):
        return self.auto_thread is not None and self.auto_thread.is_alive()

    def start_auto(self, cfg):
        if self.auto_running:
            return
        self.auto_cfg = cfg
        self.auto_stop = threading.Event()
        self.auto_thread = threading.Thread(
            target=self._auto_loop, args=(cfg, self.auto_stop), daemon=True)
        self.auto_thread.start()
        self._log(f"auto mode ON  {cfg}")

    def stop_auto(self):
        if self.auto_running:
            self.auto_stop.set()
            self.auto_thread.join(timeout=3)
            self._log("auto mode OFF")
        for pid in list(self.auto_pids):
            self.release(pid)

    def _auto_loop(self, cfg, stop):
        prev, streak = {}, {}
        while not stop.is_set():
            now = time.time()
            seen = {}
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                pid = int(name)
                if pid in self.workers:
                    continue
                s = self._eligible(pid)
                if s is None or s["comm"] in cfg["whitelist"]:
                    continue
                key = (pid, s["start"])
                seen[key] = (s["ticks"], now)
                if key in prev:
                    pt, ptime = prev[key]
                    cpu = (s["ticks"] - pt) / CLK / (now - ptime) * 100
                    streak[key] = streak.get(key, 0) + 1 if cpu > cfg["threshold"] else 0
                    if streak[key] >= cfg["samples"]:
                        self.limit(pid, cfg["budget"], auto=True)
                        self._log(f"hog detected: {pid} ({s['comm']}) {cpu:.0f}%")
            prev = seen
            self.cleanup()
            with self.lock:
                pids = [p for p in self.auto_pids if p in self.workers]
                weights = {}
                for p in pids:
                    s = tamer.read_stat(p)
                    weights[p] = max(1, 20 - s["nice"]) if s else 1
                total = sum(weights.values())
                for p in pids:
                    self.workers[p].target = cfg["budget"] * weights[p] / total
            stop.wait(cfg["interval"])


@st.cache_resource
def get_engine():
    return Engine()


eng = get_engine()
eng.cleanup()

# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.title("🐢 Tamer")
    st.caption("SIGSTOP/SIGCONT duty-cycle CPU limiter with proportional feedback.\n"
               "CPU % = percent of ONE core.")
    st.metric("Throttled processes", len(eng.workers))
    st.metric("Auto mode", "ON" if eng.auto_running else "OFF")
    st.divider()
    if st.button("🛑 Release ALL (SIGCONT)", type="primary", width="stretch"):
        eng.release_all()
        st.rerun()
    st.caption("Throttled processes are always resumed on release. If the server is "
               "killed with `kill -9`, run `pkill -CONT -f 'while :'` (or `kill -CONT <pid>`) "
               "to resume anything left stopped.")

st.title("Noisy-Neighbor Tamer")

tab_dash, tab_manual, tab_auto, tab_active = st.tabs(
    ["📊 Processes", "🎯 Manual limit", "🤖 Auto mode", "⚙️ Active throttles"])

# --------------------------------------------------------------------------
# Tab 1: live process table
# --------------------------------------------------------------------------
with tab_dash:
    @st.fragment(run_every=2)
    def process_table():
        rows = eng.snapshot()
        df = pd.DataFrame(rows)
        if df.empty:
            st.info("No processes visible.")
            return
        df = df.sort_values("CPU %", ascending=False).reset_index(drop=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("Your processes", len(df))
        c2.metric("Total CPU % (sum, own procs)", f"{df['CPU %'].sum():.0f}%")
        c3.metric("Top hog", f"{df.iloc[0]['Name']} ({df.iloc[0]['CPU %']:.0f}%)")
        only_busy = st.checkbox("Only show processes above 1% CPU", value=True,
                                key="only_busy")
        view = df[df["CPU %"] > 1] if only_busy else df
        st.dataframe(
            view, hide_index=True, width="stretch",
            column_config={
                "CPU %": st.column_config.ProgressColumn(
                    "CPU %", min_value=0, max_value=100, format="%.1f"),
            })
        st.caption("Refreshes every 2 s. State: R running, S sleeping, T stopped by the tamer.")

    process_table()

# --------------------------------------------------------------------------
# Tab 2: manual mode
# --------------------------------------------------------------------------
with tab_manual:
    st.subheader("Limit one process")
    candidates = sorted(eng.snapshot(), key=lambda r: -r["CPU %"])
    labels = {f"{r['PID']}  {r['Name']}  ({r['CPU %']:.0f}%)": r["PID"] for r in candidates}
    col1, col2 = st.columns([2, 1])
    with col1:
        choice = st.selectbox("Pick a process", list(labels) or ["(none)"],
                              help="CPU % shown is a quick sample, reload the page to refresh the list")
    with col2:
        manual_pid = st.number_input("...or type a PID", min_value=0, value=0, step=1)
    limit = st.slider("CPU limit (% of one core)", 1, 100, 30)
    pid = int(manual_pid) if manual_pid else labels.get(choice)

    if st.button("Apply limit", disabled=pid is None):
        ok, msg = eng.limit(pid, float(limit))
        (st.success if ok else st.error)(msg)
        time.sleep(0.3)
        st.rerun()

# --------------------------------------------------------------------------
# Tab 3: auto mode
# --------------------------------------------------------------------------
with tab_auto:
    st.subheader("Automatic hog detection")
    st.caption("A process above the threshold for K consecutive samples is throttled; "
               "the budget is shared between hogs by weight (20 − nice).")
    with st.form("auto_form"):
        c1, c2, c3, c4 = st.columns(4)
        threshold = c1.number_input("Threshold H (%)", 10, 100, 80)
        samples = c2.number_input("Samples K", 1, 20, 3)
        budget = c3.number_input("Budget B (% total)", 5, 400, 60)
        interval = c4.number_input("Interval T (s)", 0.5, 10.0, 1.0, step=0.5)
        wl = st.text_input(
            "Whitelist (comma-separated process names, never touched)",
            value=",".join(sorted(tamer.DEFAULT_WHITELIST)))
        st.caption('Test burners run as `sh`/`bash`, which are whitelisted by default. '
                   'Clear the field to detect them.')
        go = st.form_submit_button("Start auto mode", disabled=eng.auto_running)
    if go:
        eng.start_auto({
            "threshold": float(threshold), "samples": int(samples),
            "budget": float(budget), "interval": float(interval),
            "whitelist": {x.strip() for x in wl.split(",") if x.strip()},
        })
        st.rerun()
    if eng.auto_running:
        st.success("Auto mode is running.")
        if st.button("Stop auto mode (releases auto-throttled processes)"):
            eng.stop_auto()
            st.rerun()

# --------------------------------------------------------------------------
# Tab 4: active throttles
# --------------------------------------------------------------------------
with tab_active:
    @st.fragment(run_every=1)
    def active_panel():
        eng.cleanup()
        eng.record_history()
        if not eng.workers:
            st.info("Nothing is being throttled.")
        else:
            rows = []
            for pid, w in list(eng.workers.items()):
                s = tamer.read_stat(pid)
                rows.append({
                    "PID": pid, "Name": s["comm"] if s else "?",
                    "State": s["state"] if s else "?",
                    "Nice": s["nice"] if s else 0,
                    "Target %": round(w.target, 1),
                    "Measured %": round(w.usage, 1),
                    "Source": "auto" if pid in eng.auto_pids else "manual",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

            chart = {}
            for pid, h in eng.history.items():
                if pid in eng.workers and h:
                    chart[f"{pid} measured"] = pd.Series(
                        [m for _, m, _ in h], index=[t for t, _, _ in h])
                    chart[f"{pid} target"] = pd.Series(
                        [tg for _, _, tg in h], index=[t for t, _, _ in h])
            if chart:
                df = pd.DataFrame(chart).ffill()
                df.index = pd.to_datetime(df.index, unit="s")
                st.line_chart(df, height=300)

            rc1, rc2 = st.columns([2, 1])
            sel = rc1.selectbox("Release a process", list(eng.workers), key="rel_pid")
            if rc2.button("Release selected"):
                eng.release(sel)
                st.rerun()

        with st.expander("Event log", expanded=True):
            st.text("\n".join(eng.log) or "(empty)")

    active_panel()
