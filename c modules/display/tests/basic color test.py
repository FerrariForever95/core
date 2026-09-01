

import time
import moclcd

# Pack RGB888 into RGB565 integer
def c565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

RED   = c565(255, 0, 0)
GREEN = c565(0, 255, 0)
BLUE  = c565(0, 0, 255)
WHITE = c565(255, 255, 255)
BLACK = c565(0, 0, 0)

# Check driver version
print("Driver version:", moclcd.version)

# Initialize display pipeline (480x320 landscape)
moclcd.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
moclcd.reset()
moclcd.panel_init()

# Color cycle loop
colors = [
    ("RED", RED),
    ("GREEN", GREEN),
    ("BLUE", BLUE),
    ("WHITE", WHITE),
    ("BLACK", BLACK)
]

for name, color in colors:
    print("Flushing:", name)
    moclcd.fill_screen(color)
    time.sleep_ms(800)

print("Test complete.")
