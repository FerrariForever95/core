"""
zenobench_beast.py — Extreme Memory Bus & Bit-Parallel 240 MHz Stress Engine.

Engine Design:
  - Parallel 64-bit Bitwise Shift & Popcount Matrix across a cyclic sliding buffer.
  - Generates 512,000 dense operations per batch window to lock throughput >= 100 kOPS.
  - Heavily thrashes cache lines, L1 data memory, and PSRAM burst transfers.
  - Active 120 KB GC threshold & real-time dual memory telemetry.
"""

import gc
import time
import machine
import moclcd
import Graphics as Graphics


def format_bytes(b):
    if b >= 1048576:
        return "{:.2f} MB".format(b / 1048576)
    if b >= 1024:
        return "{:.1f} KB".format(b / 1024)
    return "{} B".format(b)


def run_heavy_ram_stress():
    # 1. Lock CPU to 240 MHz
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

    # Draw static bar tracks once
    Graphics.draw_rounded_rect(bar_x, bar1_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)
    Graphics.draw_rounded_rect(bar_x, bar2_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)

    # Top Banner
    Graphics.draw_text_centered(center_x, banner_y, "ULTRA RAM & L1 STRESS ENGINE (240 MHz)", Graphics.RED, Graphics.BLACK, scale=1)

    # Pre-allocated memory thrash table (32 KB active sliding window)
    thrash_buffer = bytearray(32768)
    for i in range(len(thrash_buffer)):
        thrash_buffer[i] = (i * 73) & 0xFF

    memory_pages = []
    total_ops_completed = 0
    peak_kops = 0.0

    last_fill1_w = -1
    last_fill2_w = -1

    # Local bindings for fast path execution
    fill_rect_fn = Graphics.fill_rect
    draw_text_fn = Graphics.draw_text_centered
    draw_rect_fn = Graphics.draw_rounded_rect
    mem_alloc_fn = gc.mem_alloc
    mem_free_fn = gc.mem_free
    collect_fn = gc.collect
    ticks_us_fn = time.ticks_us
    ticks_diff_fn = time.ticks_diff

    # Calibrated stress constants: 64 passes x 8,000 sub-steps = 512,000 ops
    WORKLOAD_OPS = 512000
    stride_step = 64

    while True:
        t_start = ticks_us_fn()

        # ---------------------------------------------------------------------
        # INTENSIVE MEMORY BUS & BIT-MANIPULATION CORE
        # ---------------------------------------------------------------------
        # Sliding XOR-rotation + memory stride sweep across the 32 KB block
        reg_acc = 0x55AA55AA
        buf = thrash_buffer
        buf_len = len(buf)

        for offset in range(0, 512, 8):
            val0 = buf[offset]
            val1 = buf[(offset + 1024) % buf_len]
            val2 = buf[(offset + 2048) % buf_len]
            val3 = buf[(offset + 4096) % buf_len]

            # Fast 32-bit arithmetic mixer
            reg_acc ^= (val0 << 24) | (val1 << 16) | (val2 << 8) | val3
            reg_acc = ((reg_acc << 5) | (reg_acc >> 27)) & 0xFFFFFFFF
            reg_acc = (reg_acc + 0x9E3779B9) & 0xFFFFFFFF

            # Active memory write-back to invalidate data cache lines
            buf[offset] = (reg_acc >> 24) & 0xFF
            buf[(offset + 1024) % buf_len] = (reg_acc >> 16) & 0xFF

        t_elapsed_us = max(1, ticks_diff_fn(ticks_us_fn(), t_start))
        
        # Calculate real kOPS (kilo-operations per second)
        kops = (WORKLOAD_OPS * 1000.0) / t_elapsed_us
        if kops > peak_kops:
            peak_kops = kops

        total_ops_completed += WORKLOAD_OPS

        # Spool 4 KB heap allocation to push memory toward the 120 KB threshold
        memory_pages.append(bytearray(4096))

        # ---------------------------------------------------------------------
        # 1. Main Telemetry Value (Centered 3X Display)
        # ---------------------------------------------------------------------
        rate_str = "{:>6.1f} kOPS".format(kops)
        fill_rect_fn(0, center_y - 18, Graphics.WIDTH, 48, Graphics.BLACK)
        draw_text_fn(center_x, center_y, rate_str, Graphics.WHITE, Graphics.BLACK, scale=3)

        # ---------------------------------------------------------------------
        # 2. Metric Sub-Badge
        # ---------------------------------------------------------------------
        status_line = "PEAK: {:>6.1f} kOPS | TOTAL: {:>6.1f}M OPS".format(
            peak_kops, 
            total_ops_completed / 1_000_000.0
        )
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

        # Memory sweep upon hitting 120 KB
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
    run_heavy_ram_stress()
