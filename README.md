# Noisy-Neighbour Tamer

A user-space Python daemon that finds processes hogging the CPU on Linux and
limits them to a fair share, without root privileges for your own processes.

## How it works
- Reads `/proc/[pid]/stat` every second and flags processes above a CPU
  threshold for several consecutive samples.
- Splits a CPU budget among the hogs, equally or weighted by nice value.
- Enforces each share with a SIGSTOP/SIGCONT duty cycle, corrected by a
  proportional feedback loop.
- Resumes every throttled process on exit and guards against PID reuse.

## Usage
    python3 tamer.py --pid <PID> --limit 30
    python3 tamer.py --auto --threshold 80 --samples 3 --budget 60 --whitelist ""

## Notes
Single-run results on WSL2; see the accompanying paper for details and
limitations. Do not throttle servers, SSH sessions or lock-holding processes.
