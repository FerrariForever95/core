from machine import Pin
import time

# ---------------------------
# CONTROL (Initialize FIRST to prevent bus contention)
# ---------------------------
# Enforce value=1 in the constructor. With CS tied LOW, any 
# LOW glitch on WR or RD will trigger a false read/write cycle!
RST = Pin(12, Pin.OUT, value=1)
RS  = Pin(13, Pin.OUT, value=1)
WR  = Pin(14, Pin.OUT, value=1)
RD  = Pin(41, Pin.OUT, value=1)

# ---------------------------
# DATA BUS (Initialize SECOND)
# ---------------------------
D = [
    Pin(16, Pin.OUT, value=0),  # D0
    Pin(15, Pin.OUT, value=0),  # D1
    Pin(11, Pin.OUT, value=0),  # D2
    Pin(10, Pin.OUT, value=0),  # D3
    Pin(9,  Pin.OUT, value=0),  # D4
    Pin(4,  Pin.OUT, value=0),  # D5
    Pin(18, Pin.OUT, value=0),  # D6
    Pin(17, Pin.OUT, value=0),  # D7
]

# ---------------------------
# BACKLIGHT
# ---------------------------
bl = Pin(38, Pin.OUT, value=1)

def write8(v):
    for i in range(8):
        D[i].value((v >> i) & 1)

    time.sleep_us(1)

    WR.value(0)
    time.sleep_us(2)
    WR.value(1) # Data is latched cleanly on this rising edge
    time.sleep_us(2)

def cmd(v):
    RS.value(0)
    write8(v)

def data(v):
    RS.value(1)
    write8(v)

# ---------------------------
# HARD RESET
# ---------------------------
# ILI9488 RESX is Active-LOW. 
# Sequence: HIGH -> LOW (Reset) -> HIGH (Run)
RST.value(1)
time.sleep_ms(1000)
RST.value(0)
time.sleep_ms(1000)
RST.value(1)
time.sleep_ms(150) # Give controller time to boot before Sleep Out

print("RESET DONE")

# ---------------------------
# BASIC INIT
# ---------------------------

cmd(0x01)      # SWRESET
time.sleep_ms(150) # Mandatory 120ms wait after SWRESET

cmd(0x11)      # SLEEPOUT
time.sleep_ms(150)

cmd(0x3A)      # COLMOD
data(0x55)     # RGB565 (If blank, change to 0x66 and use 3-byte writes)

cmd(0x36)      # MADCTL
data(0x48)

cmd(0x29)      # DISPLAY ON
time.sleep_ms(50)

# ---------------------------
# WINDOW
# ---------------------------

cmd(0x2A)
data(0x00)
data(0x00)
data(0x01)
data(0x3F)

cmd(0x2B)
data(0x00)
data(0x00)
data(0x01)
data(0xDF)

# ---------------------------
# COLOR BARS
# ---------------------------

cmd(0x2C)

RS.value(1)

colors = [
    (0xF8, 0x00),  # Red
    (0x07, 0xE0),  # Green
    (0x00, 0x1F),  # Blue
    (0xFF, 0xE0),  # Yellow
    (0xF8, 0x1F),  # Magenta
    (0x07, 0xFF),  # Cyan
    (0xFF, 0xFF),  # White
    (0x00, 0x00),  # Black
]

pixels_per_bar = (320 * 480) // len(colors)

for hi, lo in colors:
    for _ in range(pixels_per_bar):
        write8(hi)
        write8(lo)

print("COLOR BARS DONE")
