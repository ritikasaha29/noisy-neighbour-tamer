import time, statistics
d = []
for _ in range(1000):
    t = time.perf_counter(); time.sleep(0.01)
    d.append((time.perf_counter() - t - 0.01) * 1000)
d.sort()
print(f"mean {statistics.mean(d):.2f} ms | p99 {d[989]:.2f} ms | max {d[-1]:.2f} ms")
