import time
t = time.perf_counter()
x = 0
for i in range(30_000_000):
    x += i
print(f"{time.perf_counter() - t:.2f} s")
