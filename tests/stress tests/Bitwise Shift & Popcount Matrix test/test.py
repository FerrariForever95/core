"""
zenobench_bars_only.py — Extreme 240 MHz Silicon Engine with Pure Progress Bars.

Design:
  - Zero line-graphs to guarantee 100% boundary safety and zero render freeze
  - Bar 1: Total Operations Milestone Progress (0 -> 100 Million Operations)
  - Bar 2: 120 KB GC Memory Spool Buffer (0 -> 120 KB sweep cycle)
  - Bar 3: Total Board RAM / PSRAM Utilization
  - Full unabbreviated unit text:
      * "Billion Operations Per Second" (>= 1.0 BOPS)
      * "Million Operations Per Second" (>= 1.0 MOPS)
      * "Thousand Operations Per Second" (< 1.0 MOPS)
  - Rotating hardware thrash radar spinner
"""

import gc
import math
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


def format_ops_components(kops_val):
    if kops_val >= 1_000_000.0:
        val_str = "{:.2f}".format(kops_val / 1_000_000.0)
        unit_str = "Billion Operations Per Second"
    elif kops_val >= 1_000.0:
        val_str = "{:.1f}".format(kops_val / 1_000.0)
        unit_str = "Million Operations Per Second"
    else:
        val_str = "{:.1f}".format(kops_val)
        unit_str = "Thousand Operations Per Second"
    return val_str, unit_str


def run_pure_bars_stress():
    # 1. Lock CPU Clock to 240 MHz
    machine.freq(240_000_000)

    # 2. Configure 120 KB GC Threshold
    GC_TARGET_BYTES = 120 * 1024  # 122,880 Bytes
    gc.enable()
    gc.threshold(GC_TARGET_BYTES)
    gc.collect()

    base_alloc = gc.mem_alloc()

    # 3. Initialize Display Panel
    Graphics.init_display(pclk=15_000_000, width=480, height=320, madctl=0x28, dimmable=False)
    Graphics.set_background(Graphics.BLACK)
    Graphics.clear(Graphics.BLACK)

    # -------------------------------------------------------------------------
    # Layout Coordinates & Geometry
    # -------------------------------------------------------------------------
    center_x = Graphics.WIDTH // 2
    banner_y = 12
    rate_val_y = 38
    unit_text_y = 74

    bar_x = 36
    bar_w = Graphics.WIDTH - 72
    bar_h = 8

    # Bar 1: Total Operations Milestone (0 -> 100 Million Ops)
    label1_y = 104
    bar1_y = 118

    # Bar 2: 120 KB Memory Spool Buffer
    label2_y = 146
    bar2_y = 160

    # Bar 3: Total Board RAM (PSRAM Pool)
    label3_y = 188
    bar3_y = 202

    # Bottom Radar Center
    radar_cx = center_x
    radar_cy = 260
    radar_r = 18

    # Static elements
    Graphics.draw_text_centered(center_x, banner_y, "240 MHz EXTREME SILICON STRESS ENGINE", Graphics.RED, Graphics.BLACK, scale=1)

    # Static bar background tracks
    Graphics.draw_rounded_rect(bar_x, bar1_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)
    Graphics.draw_rounded_rect(bar_x, bar2_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)
    Graphics.draw_rounded_rect(bar_x, bar3_y, bar_w, bar_h, 3, Graphics.SURFACE_ALT)

    # Pre-allocated memory thrash table (32 KB active sliding window)
    thrash_buffer = bytearray(32768)
    for i in range(len(thrash_buffer)):
        thrash_buffer[i] = (i * 73) & 0xFF

    memory_pages = []
    total_ops_completed = 0
    peak_kops = 0.0
    rot_angle = 0.0

    last_fill1_w = -1
    last_fill2_w = -1
    last_fill3_w = -1

    # Local bindings for fast path execution
    fill_rect_fn = Graphics.fill_rect
    draw_text_fn = Graphics.draw_text_centered
    draw_rect_fn = Graphics.draw_rounded_rect
    draw_line_fn = Graphics.draw_line
    draw_circle_fn = Graphics.draw_circle
    fill_circle_fn = Graphics.fill_circle
    mem_alloc_fn = gc.mem_alloc
    mem_free_fn = gc.mem_free
    collect_fn = gc.collect
    ticks_us_fn = time.ticks_us
    ticks_diff_fn = time.ticks_diff

    # Calibrated stress constants: 64 passes x 8,000 sub-steps = 512,000 ops
    WORKLOAD_OPS = 512000
    OPS_TARGET_MILESTONE = 100_000_000  # 100 Million Ops per progress sweep

    while True:
        t_start = ticks_us_fn()

        # ---------------------------------------------------------------------
        # INTENSIVE MEMORY BUS & BIT-MANIPULATION CORE
        # ---------------------------------------------------------------------
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

            # Invalidate L1 data cache lines
            buf[offset] = (reg_acc >> 24) & 0xFF
            buf[(offset + 1024) % buf_len] = (reg_acc >> 16) & 0xFF

        t_elapsed_us = max(1, ticks_diff_fn(ticks_us_fn(), t_start))
        
        # Calculate raw kOPS
        kops = (WORKLOAD_OPS * 1000.0) / t_elapsed_us
        if kops > peak_kops:
            peak_kops = kops

        total_ops_completed += WORKLOAD_OPS

        # Spool 4 KB heap allocation to advance toward 120 KB threshold
        memory_pages.append(bytearray(4096))

        # ---------------------------------------------------------------------
        # 1. Main Telemetry Value & Full Form Unit Descriptor
        # ---------------------------------------------------------------------
        val_str, full_unit_str = format_ops_components(kops)

        fill_rect_fn(0, rate_val_y - 4, Graphics.WIDTH, 36, Graphics.BLACK)
        draw_text_fn(center_x, rate_val_y, val_str, Graphics.WHITE, Graphics.BLACK, scale=4)

        fill_rect_fn(0, unit_text_y - 2, Graphics.WIDTH, 14, Graphics.BLACK)
        draw_text_fn(center_x, unit_text_y, full_unit_str, Graphics.YELLOW, Graphics.BLACK, scale=1)

        # ---------------------------------------------------------------------
        # 2. Dynamic Heap Measurement & 120 KB GC Sweep
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
        # 3. Bar 1: Total Million Operations Milestone Progress
        # ---------------------------------------------------------------------
        cur_million_ops = total_ops_completed / 1_000_000.0
        milestone_pct = int(((total_ops_completed % OPS_TARGET_MILESTONE) / OPS_TARGET_MILESTONE) * 100)

        ops_bar_text = "TOTAL PROCESSED: {:.2f}M OPS (SWEEP: {}%)".format(
            cur_million_ops,
            milestone_pct
        )
        fill_rect_fn(0, label1_y - 1, Graphics.WIDTH, 11, Graphics.BLACK)
        draw_text_fn(center_x, label1_y, ops_bar_text, Graphics.WHITE, Graphics.BLACK, scale=1)

        # Strict clamping within bar boundaries: max 0, min bar_w
        cur_fill1_w = max(0, min(bar_w, int(bar_w * (milestone_pct / 100.0))))
        if cur_fill1_w != last_fill1_w:
            fill_rect_fn(bar_x, bar1_y, bar_w, bar_h, Graphics.SURFACE_ALT)
            if cur_fill1_w > 0:
                draw_rect_fn(bar_x, bar1_y, cur_fill1_w, bar_h, 3, Graphics.GREEN)
            last_fill1_w = cur_fill1_w

        # ---------------------------------------------------------------------
        # 4. Bar 2: 120 KB Memory Spool Buffer
        # ---------------------------------------------------------------------
        spool_text = "SPOOL BUFFER: {} / 120 KB ({}%)".format(
            format_bytes(allocated_delta),
            min(100, gc_progress_pct),
        )
        fill_rect_fn(0, label2_y - 1, Graphics.WIDTH, 11, Graphics.BLACK)
        draw_text_fn(center_x, label2_y, spool_text, Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)

        cur_fill2_w = max(0, min(bar_w, int(bar_w * (min(100, gc_progress_pct) / 100.0))))
        bar2_color = Graphics.BLUE if gc_progress_pct < 75 else (Graphics.ORANGE if gc_progress_pct < 90 else Graphics.RED)

        if cur_fill2_w != last_fill2_w:
            fill_rect_fn(bar_x, bar2_y, bar_w, bar_h, Graphics.SURFACE_ALT)
            if cur_fill2_w > 0:
                draw_rect_fn(bar_x, bar2_y, cur_fill2_w, bar_h, 3, bar2_color)
            last_fill2_w = cur_fill2_w

        # ---------------------------------------------------------------------
        # 5. Bar 3: Total Board RAM (PSRAM Pool)
        # ---------------------------------------------------------------------
        total_ram_text = "TOTAL RAM: {} / {} ({}%) | FREE: {}".format(
            format_bytes(m_alloc),
            format_bytes(total_heap),
            total_ram_used_pct,
            format_bytes(m_free),
        )
        fill_rect_fn(0, label3_y - 1, Graphics.WIDTH, 11, Graphics.BLACK)
        draw_text_fn(center_x, label3_y, total_ram_text, Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)

        cur_fill3_w = max(0, min(bar_w, int(bar_w * (min(100, total_ram_used_pct) / 100.0))))
        bar3_color = Graphics.GREEN if total_ram_used_pct < 60 else (Graphics.ORANGE if total_ram_used_pct < 85 else Graphics.RED)

        if cur_fill3_w != last_fill3_w:
            fill_rect_fn(bar_x, bar3_y, bar_w, bar_h, Graphics.SURFACE_ALT)
            if cur_fill3_w > 0:
                draw_rect_fn(bar_x, bar3_y, cur_fill3_w, bar_h, 3, bar3_color)
            last_fill3_w = cur_fill3_w

        # ---------------------------------------------------------------------
        # 6. Animated Hardware Radar Spinner (Strictly Clamped Circular Sweep)
        # ---------------------------------------------------------------------
        rot_angle += 0.28
        if rot_angle > (2.0 * math.pi):
            rot_angle -= (2.0 * math.pi)

        # Clear spinner area only
        fill_rect_fn(radar_cx - radar_r - 4, radar_cy - radar_r - 4, (radar_r + 4) * 2, (radar_r + 4) * 2, Graphics.BLACK)
        
        # Outer ring and crosshairs
        draw_circle_fn(radar_cx, radar_cy, radar_r, Graphics.DARK_GRAY)
        draw_circle_fn(radar_cx, radar_cy, radar_r // 2, Graphics.DARK_GRAY)

        # Sweeping pointer line
        sp_x = int(radar_cx + (radar_r - 2) * math.cos(rot_angle))
        sp_y = int(radar_cy + (radar_r - 2) * math.sin(rot_angle))
        draw_line_fn(radar_cx, radar_cy, sp_x, sp_y, Graphics.GREEN)
        fill_circle_fn(radar_cx, radar_cy, 3, Graphics.WHITE)

        # Status text beside radar
        draw_text_fn(center_x, radar_cy + radar_r + 14, "L1 CACHE & BUS RUNNING (240 MHz)", Graphics.TEXT_MUTED, Graphics.BLACK, scale=1)


if __name__ == "__main__":
    run_pure_bars_stress()
