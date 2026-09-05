# =====================================================================================
#  test_moclcd.py — Fast Bring-up Test (No Blocking I/O During Hardware Init)
# =====================================================================================

import moclcd
import time

BLACK   = 0x0000
WHITE   = 0xFFFF
RED     = 0xF800
GREEN   = 0x07E0
BLUE    = 0x001F
YELLOW  = 0xFFE0
CYAN    = 0x07FF
MAGENTA = 0xF81F

def main():
    # -------------------------------------------------------------------
    # 1. Hardware Initialization (Atomic & Fast)
    # -------------------------------------------------------------------
    print("moclcd version:", moclcd.version, "-", moclcd.build_status)

    moclcd.init()          # defaults: pclk=10MHz, width=480, height=320, madctl=0x28
    moclcd.panel_init()    # sends the MCUFRIEND-derived ILI9488 register sequence
    moclcd.backlight(1)    # turn on backlight only after panel_init() completes
    moclcd.fill_screen(0xF800)  # RGB565 red

    # -------------------------------------------------------------------
    # 2. Color Cycle
    # -------------------------------------------------------------------
    for color in (GREEN, BLUE, WHITE, BLACK):
        moclcd.fill_screen(color)
        time.sleep_ms(400)

    # -------------------------------------------------------------------
    # 3. Geometry Primitives
    # -------------------------------------------------------------------
    moclcd.fill_screen(BLACK)

    # Outlines & Solids
    moclcd.draw_rect(20, 20, 100, 60, YELLOW)
    moclcd.fill_rect(140, 20, 100, 60, CYAN)

    # Lines
    moclcd.hline(20, 100, 220, WHITE)
    moclcd.vline(20, 110, 80, WHITE)
    moclcd.draw_line(20, 110, 240, 190, MAGENTA)

    # Circles
    moclcd.draw_circle(340, 60, 40, GREEN)
    moclcd.fill_circle(340, 150, 40, BLUE)

    time.sleep(1)

    # -------------------------------------------------------------------
    # 4. Text Rendering
    # -------------------------------------------------------------------
    moclcd.fill_rect(0, 220, 480, 40, BLACK)
    moclcd.draw_text(10, 230, "moclcd Stage 1 OK - " + moclcd.version, WHITE, BLACK)

    # -------------------------------------------------------------------
    # 5. Direct Buffer Blit (Checkerboard)
    # -------------------------------------------------------------------
    w, h = 32, 32
    buf = bytearray(w * h * 2)
    be_white = WHITE.to_bytes(2, "big")
    be_black = BLACK.to_bytes(2, "big")
    for y in range(h):
        for x in range(w):
            idx = (y * w + x) * 2
            square = ((x // 4) + (y // 4)) % 2
            buf[idx:idx + 2] = be_white if square else be_black
    moclcd.blit(400, 220, w, h, buf)

    time.sleep(1)

    # -------------------------------------------------------------------
    # 6. Display Inversion Check
    # -------------------------------------------------------------------
    moclcd.invert_display(True)
    time.sleep_ms(600)
    moclcd.invert_display(False)

    # -------------------------------------------------------------------
    # 7. Print Diagnostics ONLY After Render Completes
    # -------------------------------------------------------------------
    print("\n=== moclcd Test Execution Summary ===")
    print("Version     :", moclcd.version)
    print("Build Status:", moclcd.build_status)
    print("Test Sequence Completed Successfully.")

if __name__ == "__main__":
    main()
