"""
ili9488.py — thin driver class on top of lcd_min (init/reset/cmd/data).

Speed notes (read this before assuming the bus is slow):

  1. Batch rows into one data() call instead of one-call-per-row.
     Each lcd_min.data() call has fixed Python + esp_lcd transaction
     overhead on top of the actual byte transfer; sending 32 rows in
     one call instead of 1 row x 32 calls cuts that overhead ~32x.

  2. Reusing a bytearray across multiple data() calls is only SAFE if
     its contents don't change between calls. tx_color() is async DMA
     and does not copy the buffer -- overwriting it before a previous
     transfer finishes can corrupt what's on screen. fill_rect() below
     exploits safe reuse (same color -> same bytes, reused many times).
     blit() takes a caller-supplied buffer as-is and does NOT try to
     recycle it, for the same reason.

  3. Per-pixel Python loops (true 2D patterns like a diagonal gradient)
     are usually the actual bottleneck, not the LCD bus. Precompute
     whatever doesn't depend on both x and y outside the inner loop,
     or use @micropython.native / @micropython.viper for hot loops.
"""

import lcd_min


class ILI9488:
    def __init__(self, data, dc, wr, rd=None, reset=None,
                 width=320, height=480, madctl=0x48, colmod=0x55,
                 pclk=20_000_000):
        self.width = width
        self.height = height

        lcd_min.init()
        lcd_min.reset()

        lcd_min.cmd(0x01)   # SWRESET
        _delay(150)
        lcd_min.cmd(0x11)   # SLPOUT
        _delay(150)
        lcd_min.cmd(0x3A, bytes([colmod]))
        lcd_min.cmd(0x36, bytes([madctl]))
        lcd_min.cmd(0x29)   # DISPON
        _delay(50)

    # ------------------------------------------------------------
    def set_window(self, x0, y0, x1, y1):
        """x1/y1 are inclusive end coordinates (last pixel), matching CASET/PASET."""
        lcd_min.cmd(0x2A, bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        lcd_min.cmd(0x2B, bytes([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))

    # ------------------------------------------------------------
    def fill_rect(self, x0, y0, x1, y1, color, chunk_rows=32):
        """
        Fill [x0,y0)-[x1,y1) (exclusive end) with one solid RGB565 color.
        Sends `chunk_rows` rows per data() call instead of one row at a
        time. Safe to reuse the chunk buffer across calls: its bytes
        never change (same color) for the duration of this fill.
        """
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0:
            return

        self.set_window(x0, y0, x1 - 1, y1 - 1)
        lcd_min.cmd(0x2C)   # RAMWR

        hi = (color >> 8) & 0xFF
        lo = color & 0xFF
        rows = min(chunk_rows, h)
        # bytes multiplication (repeat) works fine on MicroPython; it's
        # step-slice ASSIGNMENT (arr[0::2] = ...) that isn't supported.
        chunk = bytes([hi, lo]) * (w * rows)

        full_chunks = h // rows
        remainder = h - full_chunks * rows

        for _ in range(full_chunks):
            lcd_min.data(chunk)
        if remainder:
            lcd_min.data(chunk[:w * 2 * remainder])

    def fill(self, color, chunk_rows=32):
        self.fill_rect(0, 0, self.width, self.height, color, chunk_rows)

    # ------------------------------------------------------------
    def blit(self, x0, y0, x1, y1, buf):
        """
        Push a caller-owned pixel buffer into [x0,y0)-[x1,y1). `buf`'s
        content is whatever the caller wants (varies per call in
        general), so it is sent as-is with no internal reuse/recycling.
        """
        self.set_window(x0, y0, x1 - 1, y1 - 1)
        lcd_min.cmd(0x2C)
        lcd_min.data(buf)

    def continue_write(self, buf):
        """Stream more data into a window opened by blit()/fill_rect()'s
        RAMWR, without resending CASET/RASET/RAMWR."""
        lcd_min.data(buf)

    # ------------------------------------------------------------
    def hline(self, x, y, w, color):
        self.fill_rect(x, y, x + w, y + 1, color, chunk_rows=1)

    def vline(self, x, y, h, color):
        self.fill_rect(x, y, x + 1, y + h, color, chunk_rows=h)

    def pixel(self, x, y, color):
        self.fill_rect(x, y, x + 1, y + 1, color, chunk_rows=1)

    @staticmethod
    def rgb565(r, g, b):
        return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _delay(ms):
    import time
    time.sleep_ms(ms)
