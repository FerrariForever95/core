"""
zeno_stress_suite.py — Multi-Engine 240 MHz Hardware Stress Bench for Graphics.

Cycles through 4 intensive embedded compute engines:
  1. PRIME SIEVE (Eratosthenes bit-array cache stress)
  2. MANDELBROT FRACTAL (Hardware FPU & fixed-point escape loops)
  3. MATRIX MULTIPLICATION (O(N^3) memory bandwidth & register thrashing)
  4. SHA-256 HASH ENGINE (Bitwise rotation & contiguous byte-stream throughput)

Includes:
  - 120 KB GC collection sweep & real-time spool buffer
  - Total PSRAM / internal RAM utilization tracking
  - Live throughput metric badge (primes/s, pixels/s, MFLOPS, kHash/s)
"""

import gc
import os
import math
import time
import hashlib
import machine
import moclcd
import Graphics as Graphics


def format_bytes(b):
    if b >= 1048576:
        return "{:.2f} MB".format(b / 1048576)
    if b >= 1024:
        return "{:.1f} KB".format(b / 1024)
    return "{} B".format(b)


# =============================================================================
# COMPUTE ENGINES (HEAVY WORKLOADS)
# =============================================================================

def bench_primes(limit=4000):
    """Sieve of Eratosthenes: Branch & memory array stress."""
    sieve = bytearray(limit + 1)
    count = 0
    p = 2
    while p * p <= limit:
        if sieve[p] == 0:
            for i in range(p * p, limit + 1, p):
                sieve[i] = 1
        p += 1
    for i in range(2, limit + 1):
        if sieve[i] == 0:
            count += 1
    return count


def bench_mandelbrot(samples=600):
    """Floating-point FPU escape time calculation."""
    escaped = 0
    step = 3.0 / math.sqrt(samples)
    y = -1.2
    while y < 1.2:
        x = -2.0
        while x < 1.0:
            cr, ci = x, y
            zr, zi = 0.0, 0.0
            for _ in range(40):
                zr2 = zr * zr
                zi2 = zi * zi
                if zr2 + zi2 > 4.0:
                    escaped += 1
                    break
                zi = 2.0 * zr * zi + ci
                zr = zr2 - zi2 + cr
            x += step
        y += step
    return escaped


def bench_matrix_mult(n=18):
    """O(N^3) Double-loop Matrix Multiplication."""
    A = [[(i + j) * 0.1 for j in range(n)] for i in range(n)]
    B = [[(i - j) * 0.1 for j in range(n)] for i in range(n)]
    C = [[0.0 for _ in range(n)] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            s = 0.0
            for k in range(n):
                s += A[i][k] * B[k][j]
            C[i][j] = s
    return 2 * (n ** 3)  # FLOP count


def bench_sha256(chunks=12, chunk_size=1024):
    """Cryptographic hash bitwise rotation & streaming stress."""
    hasher = hashlib.sha256()
    block = bytearray(chunk_size)
    for i in range(chunks):
        block[0] = i & 0xFF
        hasher.update(block)
    digest = hasher.digest()
    return chunks * chunk_size


# =============================================================================
# MAIN BENCHMARK RUNNER
# =============================================================================

def run_stress_suite():
    # 1. Lock CPU Clock to 240 MHz
    machine.freq(240_000_000)

    # 2. Configure 120 KB GC Threshold
    GC_TARGET_BYTES = 120 * 1024
    gc.enable()
    gc.threshold(GC_TARGET_BYTES)
    gc.collect()

    base_alloc = gc.mem_alloc()

    # 3. Initialize Display Panel
    Graphics.init_display(pclk=15_000_000, width=480, height=320, madctl=0x28, dimmable=False)
    Graphics.set_background(Graphics.BLACK)
    Graphics.clear(Graphics.BLACK)

    # Layout Coordinates
    center_x = Graphics.WIDTH // 2
    center_y = (Graphics.HEIGHT // 2) - 40
    banner_y = center_y - 55
    counter_y = center_y + 35

    bar_x = 36
    bar_w = Graphics.WIDTH - 72
    bar_h = 7

    label1_y = Graphics.HEIGHT - 74
    bar1_y = Graphics.HEIGHT - 60

    label2_y = Graphics.HEIGHT - 44
    bar2_y = Graphics.HEIGHT - 30

    # Draw static bar background tracks
    Graphics.draw_rounded_rect(bar_x, bar1_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)
    Graphics.draw_rounded_rect(bar_x, bar2_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)

    engines = [
        ("PRIME SIEVE", Graphics.YELLOW),
        ("MANDELBROT FPU", Graphics.BLUE),
        ("MATRIX MULT (N^3)", Graphics.PURPLE),
        ("SHA-256 CRYPTO", Graphics.GREEN),
    ]

    engine_idx = 0
    frame_count = 0
    total_ops = 0
    memory_pages = []

    last_fill1_w = -1
    last_fill2_w = -1

    # Local bindings for fast path execution
    fill_rect_fn = Graphics.fill_rect
    draw_text_fn = Graphics.draw_text_centered
    draw_rect_fn = Graphics.draw_rounded_rect
    mem_alloc_fn = gc.mem_alloc
    mem_free_fn = gc.mem_free
    collect_fn = gc.collect

    while True:
        # Rotate through benchmark engines every 60 frames (~2-3 seconds per mode)
        if frame_count % 60 == 0:
            engine_idx = (frame_count // 60) % len(engines)
            cur_engine_name, cur_engine_color = engines[engine_idx]
            
            # Update Top Engine Mode Banner
            fill_rect_fn(0, banner_y - 10, Graphics.WIDTH, 22, Graphics.BLACK)
            draw_text_fn(center_x, banner_y, "ACTIVE ENGINE: " + cur_engine_name, cur_engine_color, Graphics.BLACK, scale=1)

        t_start = time.ticks_us()

        # ---------------------------------------------------------------------
        # EXECUTE ACTIVE BENCHMARK ENGINE
        # ---------------------------------------------------------------------
        if engine_idx == 0:
            res = bench_primes(limit=3500)
            metric_label = "PRIMES: {}".format(res)
        elif engine_idx == 1:
            res = bench_mandelbrot(samples=500)
            metric_label = "ESCAPES: {}".format(res)
        elif engine_idx == 2:
            res = bench_matrix_mult(n=16)
            metric_label = "FLOPS: {}".format(res)
        else:
            res = bench_sha256(chunks=10, chunk_size=1024)
            metric_label = "HASHED: {} B".format(res)

        t_elapsed_us = max(1, time.ticks_diff(time.ticks_us(), t_start))
        throughput_kops = (1_000_000.0 / t_elapsed_us)

        total_ops += 1
        frame_count += 1

        # Spool 4 KB heap allocation to exercise memory pipeline toward 120 KB
        memory_pages.append(bytearray(4096))

        # ---------------------------------------------------------------------
        # 1. Main Telemetry Value (Centered 3X Display)
        # ---------------------------------------------------------------------
        rate_str = "{:>6.1f} kOPS".format(throughput_kops)
        fill_rect_fn(0, center_y - 18, Graphics.WIDTH, 48, Graphics.BLACK)
        draw_text_fn(center_x, center_y, rate_str, Graphics.WHITE, Graphics.BLACK, scale=3)

        # ---------------------------------------------------------------------
        # 2. Metric Sub-Badge
        # ---------------------------------------------------------------------
        status_line = "{} | PASS: {}".format(metric_label, total_ops)
        fill_rect_fn(0, counter_y - 2, Graphics.WIDTH, 14, Graphics.BLACK)
        draw_text_fn(center_x, counter_y, status_line, Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)

        # ---------------------------------------------------------------------
        # 3. Dynamic Heap Measurement & 120 KB GC Sweep
        # ---------------------------------------------------------------------
        m_free = mem_free_fn()
        m_alloc = mem_alloc_fn()
        total_heap = m_free + m_alloc

        allocated_delta = max(0, m_alloc - base_alloc)
        gc_progress_pct = int((allocated_delta / GC_TARGET_BYTES) * 100)
        total_ram_used_pct = int((m_alloc / total_heap) * 100) if total_heap else 0

        if allocated_delta >= GC_TARGET_BYTES:
            memory_pages.clear()
            collect_fn()
            base_alloc = mem_alloc_fn()
            allocated_delta = 0
            gc_progress_pct = 0
            m_alloc = base_alloc
            m_free = mem_free_fn()
            total_heap = m_free + m_alloc
            total_ram_used_pct = int((m_alloc / total_heap) * 100) if total_heap else 0

        # ---------------------------------------------------------------------
        # 4. Render Bar 1: 120 KB Spool Buffer
        # ---------------------------------------------------------------------
        spool_text = "SPOOL BUFFER: {} / 120 KB ({}%)".format(
            format_bytes(allocated_delta),
            min(100, gc_progress_pct),
        )
        fill_rect_fn(0, label1_y - 1, Graphics.WIDTH, 11, Graphics.BLACK)
        draw_text_fn(center_x, label1_y, spool_text, Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)

        cur_fill1_w = max(0, min(bar_w, int(bar_w * (min(100, gc_progress_pct) / 100.0))))
        bar1_color = Graphics.BLUE if gc_progress_pct < 75 else (Graphics.ORANGE if gc_progress_pct < 90 else Graphics.RED)

        if cur_fill1_w != last_fill1_w:
            fill_rect_fn(bar_x, bar1_y, bar_w, bar_h, Graphics.SURFACE_ALT)
            if cur_fill1_w > 0:
                draw_rect_fn(bar_x, bar1_y, cur_fill1_w, bar_h, 3, bar1_color)
            last_fill1_w = cur_fill1_w

        # ---------------------------------------------------------------------
        # 5. Render Bar 2: Total Board RAM (PSRAM Pool)
        # ---------------------------------------------------------------------
        total_ram_text = "TOTAL RAM: {} / {} ({}%) | FREE: {}".format(
            format_bytes(m_alloc),
            format_bytes(total_heap),
            total_ram_used_pct,
            format_bytes(m_free),
        )
        fill_rect_fn(0, label2_y - 1, Graphics.WIDTH, 11, Graphics.BLACK)
        draw_text_fn(center_x, label2_y, total_ram_text, Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)

        cur_fill2_w = max(0, min(bar_w, int(bar_w * (min(100, total_ram_used_pct) / 100.0))))
        bar2_color = Graphics.GREEN if total_ram_used_pct < 60 else (Graphics.ORANGE if total_ram_used_pct < 85 else Graphics.RED)

        if cur_fill2_w != last_fill2_w:
            fill_rect_fn(bar_x, bar2_y, bar_w, bar_h, Graphics.SURFACE_ALT)
            if cur_fill2_w > 0:
                draw_rect_fn(bar_x, bar2_y, cur_fill2_w, bar_h, 3, bar2_color)
            last_fill2_w = cur_fill2_w


if __name__ == "__main__":
    run_stress_suite()
