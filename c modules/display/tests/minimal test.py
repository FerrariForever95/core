# smoke_test.py -- absolute minimum test. Run this FIRST, before test_moclcd.py.
# If this doesn't turn the screen red, don't bother running the full test yet --
# fix bring-up first (check wiring against the PIN MAP in modlcd.c's header,
# check backlight is actually wired to GPIO 38, check RST is not stuck low).
#working smoke test for working display code.c in /stable

import moclcd

print("moclcd version:", moclcd.version, "-", moclcd.build_status)

moclcd.init()          # defaults: pclk=10MHz, width=480, height=320, madctl=0x28
moclcd.panel_init()    # sends the MCUFRIEND-derived ILI9488 register sequence
moclcd.backlight(1)    # turn on backlight only after panel_init() completes
moclcd.fill_screen(0xF800)  # RGB565 red

print("If the screen is now solid red, Stage 1 bring-up works.")
print("If it's still blank/white, see the troubleshooting notes in")
print("test_moclcd.py / the modlcd.c header comment block.")
