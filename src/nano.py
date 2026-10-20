# ===========================================================================
# Zeno UI App: ZNU nano Text Editor (Integrated Exit & File Bridging)
# Uses zeno.ui and from Graphics import Graphics as gfx exclusively
# ===========================================================================

import os
import sys
import time
import zeno

if "/bin/Graphics" not in sys.path:
    sys.path.append("/bin/Graphics")

from Graphics import Graphics as gfx

gfx.bind_lcd(zeno.ui)

color565        = gfx.color565
UIWindow        = gfx.UIWindow
Keyboard        = gfx.Keyboard

FONT_8X8        = gfx.FONT_8X8
CHAR_W, CHAR_H  = 8, 8

APP_NAME    = "ZNU nano"
APP_AUTHOR  = "Phoenix"
APP_VERSION = "2.5.0"

NANO_BG         = color565(16, 16, 16)
HEADER_BG       = color565(200, 200, 200)
HEADER_FG       = color565(0, 0, 0)
TEXT_FG         = color565(220, 220, 220)
STATUS_BG       = color565(200, 200, 200)
STATUS_FG       = color565(0, 0, 0)
FOOTER_KEY_FG   = color565(0, 200, 255)
FOOTER_DESC_FG  = color565(255, 255, 255)
BORDER_COL      = color565(60, 60, 60)

screen = None
kb = None
current_file = "/Home/notes.txt"
file_content = ""
status_message = " Ready"
status_timer = 0
keyboard_active = False
last_kb_state = False


def set_status(msg):
    global status_message, status_timer
    status_message = f" {msg}"
    status_timer = time.ticks_ms()
    draw_status_bar()


def get_target_file():
    """Reads the target filename passed by ZenCMD via bridge file."""
    try:
        with open("/._nano_target.tmp", "r") as f:
            p = f.read().strip()
            if p:
                return p
    except Exception:
        pass
    return "/Home/notes.txt"


def load_file(path):
    global current_file, file_content
    current_file = path
    try:
        with open(path, "r") as f:
            file_content = f.read()
        set_status(f"Loaded {path}")
    except Exception:
        file_content = f"# ZNU nano Editor\n# File: {path}\n\nType your notes here...\n"
        try:
            with open(path, "w") as f:
                f.write(file_content)
            set_status(f"Created {path}")
        except Exception as e:
            set_status(f"Error: {e}")


def save_file():
    global file_content
    try:
        with open(current_file, "w") as f:
            f.write(file_content)
        set_status(f"Saved changes to {current_file}")
    except Exception as e:
        set_status(f"Save failed: {e}")


def draw_full_editor():
    w = gfx.WIDTH
    h = gfx.HEIGHT

    # Top Header Bar
    gfx.fill_rect(0, 0, w, 20, HEADER_BG)
    title_text = f"  ZNU nano 2.5                        File: {current_file}  "
    gfx.draw_text(4, 6, title_text, HEADER_FG, HEADER_BG, FONT_8X8, 1)

    # Text Editing Area Box
    edit_y = 22
    edit_h = h - 60
    gfx.fill_rect(4, edit_y, w - 8, edit_h, NANO_BG)
    gfx.draw_rect(4, edit_y, w - 8, edit_h, BORDER_COL, fill=False)

    draw_text_content()
    draw_status_bar()


def draw_text_content():
    w = gfx.WIDTH
    edit_y = 22
    edit_h = (gfx.HEIGHT - 176) if keyboard_active else (gfx.HEIGHT - 60)
    
    gfx.fill_rect(6, edit_y + 2, w - 12, edit_h - 4, NANO_BG)

    ty = edit_y + 4
    max_lines = edit_h // CHAR_H
    max_cols = (w - 16) // CHAR_W

    lines = file_content.splitlines() or [""]
    for line in lines[:max_lines]:
        display_line = line[:max_cols]
        gfx.draw_text(8, ty, display_line, TEXT_FG, NANO_BG, FONT_8X8, 1)
        ty += CHAR_H

    if keyboard_active and kb and kb.buffer:
        gfx.draw_text(8, ty, kb.buffer + "_", color565(255, 255, 0), NANO_BG, FONT_8X8, 1)


def draw_status_bar():
    w = gfx.WIDTH
    h = gfx.HEIGHT

    if keyboard_active:
        status_y = h - 156
        gfx.fill_rect(0, status_y, w, 18, STATUS_BG)
        msg = status_message
        if status_timer and time.ticks_diff(time.ticks_ms(), status_timer) > 4000:
            msg = " [ Type text | Hit OK to save line & continue ]"
        gfx.draw_text(4, status_y + 5, msg, STATUS_FG, STATUS_BG, FONT_8X8, 1)
    else:
        status_y = h - 36
        gfx.fill_rect(0, status_y, w, 18, STATUS_BG)
        msg = status_message
        if status_timer and time.ticks_diff(time.ticks_ms(), status_timer) > 4000:
            msg = " [ Touch editor to type | Hit ^O to save ]"
        gfx.draw_text(4, status_y + 5, msg, STATUS_FG, STATUS_BG, FONT_8X8, 1)

        footer_y = h - 18
        gfx.fill_rect(0, footer_y, w, 18, NANO_BG)
        shortcuts = [("^G", "Help"), ("^O", "Save"), ("^X", "Exit")]
        col_w = w // 3
        for i, (key, desc) in enumerate(shortcuts):
            cx = i * col_w + 12
            gfx.draw_text(cx, footer_y + 3, key, FOOTER_KEY_FG, NANO_BG, FONT_8X8, 1)
            gfx.draw_text(cx + 24, footer_y + 3, desc, FOOTER_DESC_FG, NANO_BG, FONT_8X8, 1)


def on_exit():
    """Cleanly exits Nano and returns back to ZenCMD."""
    # Clean up bridge file
    try:
        os.remove("/._nano_target.tmp")
    except Exception:
        pass
    raise SystemExit(0)


def on_kb_submit(buf):
    global file_content
    if buf:
        file_content += buf + "\n"
        save_file()
    kb.clear_buffer()
    draw_text_content()
    draw_status_bar()


def main():
    global screen, kb, keyboard_active, last_kb_state

    gfx.bind_lcd(zeno.ui)

    screen = UIWindow(
        zeno.ui,
        title="ZNU nano",
        background=NANO_BG,
        on_exit=on_exit,
        taskbar_color=color565(40, 40, 42),
        title_color=color565(255, 255, 255),
        taskbar_height=22,
        exit_button=True
    )

    screen.start(animate=False)
    gfx.pin.on()

    kb = Keyboard(zeno.ui, height=130, ok_action=on_kb_submit)
    kb.enabled = False

    # Load file requested by ZenCMD
    target = get_target_file()
    load_file(target)
    draw_full_editor()

    last_buf = ""

    while True:
        try:
            screen.poll()

            pt = gfx.get_touch()
            if pt is not None:
                tx, ty = pt
                if 22 <= ty <= 175:
                    if not keyboard_active:
                        keyboard_active = True
                        kb.enabled = True

            if keyboard_active != last_kb_state:
                last_kb_state = keyboard_active
                if keyboard_active:
                    kb.render()
                    draw_status_bar()
                    draw_text_content()
                else:
                    gfx.fill_rect(0, gfx.HEIGHT - 150, gfx.WIDTH, 150, NANO_BG)
                    draw_full_editor()

            if keyboard_active and getattr(kb, 'enabled', True):
                kb.check_touch()
                if kb.buffer != last_buf:
                    last_buf = kb.buffer
                    draw_text_content()

            time.sleep_ms(25)
        except SystemExit:
            break


main()
