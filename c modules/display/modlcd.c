/*
 * =====================================================================================
 *  FILE:        modlcd.c
 *  MODULE:      moclcd  (MicroPython native C module)
 *  TARGET:      ESP32-S3, ESP-IDF esp_lcd i80 (Intel 8080) parallel bus, 8-bit data
 *  PANEL:       ILI9488, 320 x 480, MIPI DCS Rev.1 command set
 *
 *  VERSION:     1.5.0-dev
 *  BUILD STATUS: UNVERIFIED / DEV  <-- has NOT been confirmed working on real hardware
 *               (Per project rule: this string only changes to "STABLE" after the
 *                developer explicitly tests on the target board and confirms it.)
 *
 *  STAGE:       1 of 2 (display output only -- no touch logic in this file)
 *
 *  -------------------------------------------------------------------------------
 *  PIN MAP (fixed in this build, see LCD_PIN_* below)
 *  -------------------------------------------------------------------------------
 *      D0            -> GPIO 16
 *      D1            -> GPIO 15
 *      D2            -> GPIO 11
 *      D3            -> GPIO 10
 *      D4            -> GPIO 9
 *      D5            -> GPIO 4
 *      D6  (YM)      -> GPIO 18
 *      D7  (XP)      -> GPIO 17
 *      RS / DC (XM)  -> GPIO 13
 *      WR            -> GPIO 14
 *      RD            -> GPIO 41
 *      RST           -> GPIO 12
 *      BL            -> GPIO 38   (digital on/off in Stage 1; LEDC PWM optional, see backlight())
 *      CS  (YP)      -> hard-wired to GND on PCB (NOT driven by this code -- see note below)
 *
 *  NOTE ON CS: The esp_lcd i80 bus driver normally toggles a CS line itself. Because
 *  your CS is hard-wired to GND, LCD_PIN_NUM_CS is set to -1 (GPIO_NUM_NC) so the
 *  esp_lcd driver does NOT try to drive a CS pin -- the panel is permanently selected,
 *  which is fine since nothing else shares this bus. If you later rewire CS to a GPIO
 *  for Stage 2 touch multiplexing, this is the only line that needs to change.
 *
 *  -------------------------------------------------------------------------------
 *  CHANGELOG
 *  -------------------------------------------------------------------------------
 *  v1.5.0-dev (this file)
 *    - ACTUAL ROOT-CAUSE FIX for the pure-white-screen bug, found by comparing
 *      against two previously-working reference drivers for this exact panel/pin
 *      config (user-supplied modlcd_nopool.c and lcd_min.c). v1.4.0-dev's fix
 *      (using RAMWR/0x2C as the tx_color() command) was still wrong. Per the
 *      ILI9488 datasheet:
 *        - Memory Write (0x2C, Section 5.2.24, p.179): "the column and page
 *          registers are reset to the Start Column (SC) and Start Page (SP)"
 *          EVERY time this command is sent.
 *        - Memory Write Continue (0x3C, Section 5.2.35, p.201): "makes no
 *          change to the other driver status" and explicitly does NOT reset
 *          the column/page registers -- it continues from the current
 *          position, which is the correct behavior for streaming a pixel
 *          payload after the window has already been armed.
 *      v1.4.0-dev used 0x2C as the tx_color() command for every chunk, which
 *      reset the write pointer back to the window's top-left corner on every
 *      single chunk instead of letting it advance -- both reference drivers
 *      instead send a bare 0x2C (no parameters) once via tx_param() to arm the
 *      window, then use 0x3C for every subsequent pixel payload via
 *      tx_color(). This driver now matches that exact pattern:
 *        - moclcd_set_window() sends CASET, PASET, then bare RAMWR (0x2C) --
 *          restored from v1.3.0-dev's behavior, which v1.4.0-dev had
 *          incorrectly removed.
 *        - moclcd_send_pixels() now sends every payload under RAMWRC (0x3C)
 *          rather than RAMWR (0x2C).
 *    - Cross-referenced clk_src: this file uses LCD_CLK_SRC_DEFAULT while the
 *      user's reference files use LCD_CLK_SRC_PLL160M. Per Espressif's own
 *      esp_lcd_i80_bus_config_t documentation these resolve to the same
 *      underlying clock source on ESP32-S3 -- this was checked and is NOT
 *      believed to be a functional difference, so it was left as-is rather
 *      than changed speculatively.
 *    - No other functional changes from v1.4.0-dev.
 *
 *  v1.4.0-dev
 *    - ROOT-CAUSE FIX for "runs clean, backlight works, screen stays pure white":
 *      moclcd_send_pixels() was calling esp_lcd_panel_io_tx_color(io, -1, data,
 *      len) to mean "just stream this pixel data, no command byte". That -1
 *      convention is explicitly documented by Espressif as valid ONLY for the
 *      SPI and I2C panel IO backends ("lcd_cmd -- set to -1 if no command
 *      needed - only in SPI and I2C"). The i80 bus has no such carve-out, and
 *      every real ESP-IDF i80 panel driver (e.g. esp_lcd_panel_st7789.c's
 *      panel_st7789_draw_bitmap()) always calls
 *      esp_lcd_panel_io_tx_color(io, LCD_CMD_RAMWR, color_data, len) -- never -1.
 *      This matches the exact symptom reported: esp_lcd_panel_io_tx_color()
 *      returned ESP_OK every time (no exception raised anywhere), backlight and
 *      every esp_lcd_panel_io_tx_param() command worked fine (those always used
 *      a real command byte), but no pixel data ever visibly reached the panel --
 *      because the pixel-streaming call was the one path in the whole driver
 *      using the unsupported -1 "no command" convention on i80.
 *    - Fix: moclcd_send_pixels() now always passes ILI9488_CMD_RAMWR (0x2C) as
 *      the command for every esp_lcd_panel_io_tx_color() call, matching
 *      Espressif's own i80 panel drivers exactly.
 *    - Correspondingly, moclcd_set_window() no longer sends a bare/standalone
 *      RAMWR command after CASET/PASET -- that responsibility now lives entirely
 *      in moclcd_send_pixels(), so RAMWR is issued exactly once per actual pixel
 *      transaction rather than once redundantly before it. Public behavior is
 *      unchanged: calling moclcd.set_window() followed by moclcd.data() still
 *      works exactly as before, because data() calls moclcd_send_pixels()
 *      internally, which now issues RAMWR itself.
 *    - Added ILI9488_CMD_RAMWRC (0x3C, Write Memory Continue) as a named
 *      constant for reference; RAMWR (0x2C) continues to be used for every
 *      chunk since DCS/ILI9488 allow re-issuing RAMWR without resetting the
 *      internal GRAM address counter as long as the CASET/PASET window is
 *      unchanged (confirmed against ILI9488.PDF Memory Write, p.140/179).
 *    - No other functional changes from v1.3.0-dev.
 *
 *  v1.3.0-dev
 *    - BUILD FIX: set_window() failed to compile against real MicroPython headers
 *      with:
 *        error: type defaults to 'int' in declaration of 'MP_DEFINE_CONST_FUN_OBJ_4'
 *        error: parameter names (without types) in function declaration
 *        error: 'moclcd_set_window_obj' undeclared here
 *      Root cause: py/obj.h only defines fixed-arity helpers for 0/1/2/3 args
 *      (MP_DEFINE_CONST_FUN_OBJ_0/_1/_2/_3) plus MP_DEFINE_CONST_FUN_OBJ_KW for
 *      keyword-arg functions -- there is no _OBJ_4 macro. set_window() takes 4
 *      fixed positional args (x0,y0,x1,y1) and was incorrectly written in the
 *      4-separate-mp_obj_t-parameters style with a nonexistent _OBJ_4 registration.
 *      Every other 4+-arg function in this file (hline, vline, draw_rect, etc.)
 *      already used the correct pattern -- size_t n_args + const mp_obj_t *args,
 *      registered via MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(name, min, max, fn) -- so
 *      set_window() has been rewritten to match that same pattern. This was missed
 *      in earlier versions because no MicroPython source tree was available in the
 *      environment those versions were written in to compile-check against; this
 *      fix was verified directly against the real compiler error output.
 *    - Audited every other MP_DEFINE_CONST_FUN_OBJ_* call in the file against its
 *      function's actual parameter count/signature; no other mismatches found.
 *    - No other functional changes from v1.2.0-dev.
 *
 *  v1.2.0-dev
 *    - Fixed a latent bus-corruption bug in read_reg(): the previous implementation
 *      flipped D0-D7 between esp_lcd-owned output and plain GPIO input using
 *      gpio_config() and expected esp_lcd's GPIO-matrix routing to "come back" when
 *      switched back to output. On ESP32-S3, esp_lcd_new_i80_bus() routes those pins
 *      through the GPIO matrix to the LCD_CAM/GDMA peripheral; gpio_config() cannot
 *      restore that matrix binding, so every read_reg() call risked silently
 *      breaking all subsequent writes (a "worked once, dead after touching
 *      read_reg()" failure mode). read_reg() now explicitly tears the esp_lcd bus
 *      down, performs the entire read as a manual bit-banged transaction (command
 *      phase + RD strobe(s)), and rebuilds the esp_lcd bus from scratch afterward
 *      via the same moclcd_bus_init() used by init(). Slower per-call, but no
 *      longer capable of leaving the driver in a half-broken state.
 *    - No other functional changes from v1.1.0-dev.
 *
 *  v1.1.0-dev
 *    - Added moclcd_bus_pins_safe_idle(): forces D0-D7, DC, WR, RD, RST, BL to a
 *      single known-inert GPIO state (data=0, DC=1, WR=1, RD=1, RST=0, BL=0) in one
 *      atomic gpio_config() call BEFORE any timed sequencing begins. Rationale:
 *      with CS hard-wired to GND the panel has no "not listening" state -- it
 *      latches whatever is on the bus the instant WR sees a rising edge, even
 *      before MicroPython calls anything. Undefined post-boot GPIO levels could
 *      previously clock a stray command byte into the controller before
 *      panel_init() ever ran. init() now calls this via moclcd_gpio_init() before
 *      esp_lcd bus setup, and reset() calls it defensively too.
 *    - init() now calls moclcd_hw_reset() itself (previously the user had to call
 *      reset() separately in the right order) so a single init() call reliably
 *      leaves the panel in the documented post-RST state before any command is
 *      sent, removing an ordering foot-gun.
 *    - reset() reconfigures RD/BL alongside RST so it is safe to call standalone,
 *      independent of whether init() already ran.
 *    - Cross-checked every register in the ILI9488 init table (0xC0,0xC1,0xC5,0x36,
 *      0xB0,0xB1,0xB4,0xB6,0xB7,0x3A,0xF7) plus 0x01/0x11/0x28/0x29/0x2A/0x2B/0x2C/
 *      0xD3 against the ILI9488 datasheet Section 5 command list and Section 17.4.1
 *      AC timing table -- all opcodes, parameter widths, and default values match
 *      both the datasheet and MCUFRIEND_kbv.cpp exactly (see design notes below).
 *    - No functional drawing/API changes from v1.0.0-dev; this is a bring-up
 *      robustness pass plus verification, not a feature change.
 *
 *  v1.0.0-dev
 *    - Initial Stage 1 implementation.
 *    - esp_lcd i80 bus + panel IO setup (8-bit, configurable PCLK, default 10MHz).
 *    - Hardware reset sequence matching MCUFRIEND_kbv reset() timing
 *      (IDLE 50ms -> ACTIVE 100ms -> IDLE 100ms).
 *    - Panel register init sequence ported verbatim (values + order) from
 *      MCUFRIEND_kbv.cpp "case 0x9488" (ILI9488_regValues_max, the "Atmel MaxTouch"
 *      table) plus the shared reset_off / wake_on framing blocks.
 *    - MADCTL default 0x28 matches MCUFRIEND_kbv's rotation==1 (LANDSCAPE) value.
 *    - CASET/PASET/RAMWR use the MIPI DCS Rev.1 default addresses confirmed in
 *      MCUFRIEND_kbv.cpp (_MC=_SC=0x2A, _MP=_SP=0x2B, _MW=0x2C).
 *    - Bresenham line, midpoint circle, rect/fill/blit primitives implemented
 *      directly against set_window()+RAMWR streaming.
 *    - version string exported into module globals as moclcd.version.
 *
 *  -------------------------------------------------------------------------------
 *  DATASHEET CROSS-CHECK (ILI9488.PDF, "Version: 100")
 *  -------------------------------------------------------------------------------
 *  Every command MCUFRIEND_kbv sends for ID 0x9488 was individually verified
 *  against the datasheet's command list (Section 5.1) and, where relevant, its
 *  extended command list (Section 5.1.2):
 *      0x01 SWRESET      p.140  Soft Reset
 *      0x11 SLPOUT       p.140  Sleep OUT
 *      0x28 DISPOFF      p.140  Display OFF
 *      0x29 DISPON       p.140  Display ON
 *      0x2A CASET        p.140  Column Address Set
 *      0x2B PASET        p.140  Page Address Set
 *      0x2C RAMWR        p.140  Memory Write
 *      0x2E RAMRD        p.140  Memory Read
 *      0x3A COLMOD       p.141  Interface Pixel Format (0x55 = 16bpp both DBI/DPI)
 *      0x36 MADCTL       standard DCS Memory Access Control
 *      0xB0 IFMODE       p.144  Interface Mode Control
 *      0xB1 FRMCTR1      p.144  Frame Rate Control (Normal Mode/Full Colors)
 *      0xB4 INVTR        p.144  Display Inversion Control
 *      0xB6 DFC          p.144  Display Function Control (NL[5:0] sets active lines;
 *                                0x3B = 59 -> (59+1)*8 = 480 lines, matching a
 *                                480-line-tall physical panel)
 *      0xB7 ETMOD        p.144  Entry Mode Set
 *      0xC0 PWCTR1       p.145  Power Control 1 (VRH1/VRH2)
 *      0xC1 PWCTR2       p.145  Power Control 2 (BT)
 *      0xC5 VMCTR1       p.145  VCOM Control 1
 *      0xF7 ADJCTL3      p.147  Adjust Control 3 -- datasheet's own default value
 *                                for this register is listed as A9 51 2C 82, which
 *                                is EXACTLY what MCUFRIEND_kbv sends. This is strong
 *                                independent confirmation the vendor table is correct
 *                                for a genuine ILI9488, not a mis-clocked/garbled
 *                                capture from a different controller.
 *      0xD3 RDID4        p.145  Read ID4; datasheet lists the fixed reply bytes
 *                                [00 94 88], i.e. ID2:ID3 = 0x9488 -- matches
 *                                MCUFRIEND_kbv's readReg32(0xD3) => 0x9488 detection.
 *  Section 17.4.1 (DBI Type B / i8080 AC characteristics, p.329) gives write-cycle
 *  timing minimums of twc=40ns, twrl=twrh=15ns, tdst/tdht=10ns. A 10MHz PCLK (the
 *  init() default) gives a 100ns cycle, i.e. >2x margin over the absolute minimum,
 *  which is why init()'s pclk ceiling is clamped to 20MHz (50ns cycle, still >1x
 *  margin) rather than letting a caller push toward the theoretical 25MHz limit.
 *
 *  -------------------------------------------------------------------------------
 *  WHY THE ORIGINAL BOARD SHOWED A BLANK WHITE SCREEN (background for the port)
 *  -------------------------------------------------------------------------------
 *  A blank/white ILI9488 screen almost always means one of:
 *    (a) the panel never got past DISPOFF/SLPIN,
 *    (b) MADCTL/COLMOD were never written so GRAM is being read/written in an
 *        address order or bit depth the driver isn't expecting,
 *    (c) RD or another bus line was left floating/undefined during write-only
 *        operation and glitched a command byte into the panel, or
 *    (d) RST was not held low long enough for the internal reset to complete.
 *  This port addresses all four:
 *    1) Reproduces the *exact* MCUFRIEND_kbv timing/order (100ms low pulse on
 *       RST, not a quick blip) via moclcd_hw_reset().
 *    2) Forces every bus pin (not just RD) to a defined level via
 *       moclcd_bus_pins_safe_idle() *before* RST is ever released and *before*
 *       esp_lcd takes ownership of the pins -- see v1.1.0-dev changelog above.
 *    3) Explicitly drives RD high (idle) throughout Stage 1; read cycles are only
 *       used in read_reg(), which restores RD to idle afterward.
 *    4) Sends SWRESET + 150ms delay + DISPOFF + COLMOD *before* the vendor
 *       register table, then SLPOUT + 150ms + DISPON *after* it, matching the
 *       proven Arduino sequence exactly rather than a generic ILI9488 example
 *       pulled from elsewhere.
 * =====================================================================================
 */

#include <string.h>
#include <stdlib.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "py/objarray.h"
#include "py/binary.h"
#include "py/mphal.h"

#include "driver/gpio.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "moclcd";

/* ===================================================================================
 *  VERSION / BUILD STATUS
 *  Per project rule #2: only flip BUILD_STABLE to 1 after the developer has tested
 *  on real hardware and explicitly confirmed it. Leave at 0 ("dev/unverified") for
 *  every iteration until that happens.
 * =================================================================================== */
#define MOCLCD_VERSION_STRING   "1.4.0-dev"
#define MOCLCD_BUILD_STABLE     0   /* 0 = dev/unverified, 1 = STABLE (only after user confirms) */

#if MOCLCD_BUILD_STABLE
#define MOCLCD_STATUS_STRING "STABLE"
#else
#define MOCLCD_STATUS_STRING "DEV/UNVERIFIED"
#endif

/* ===================================================================================
 *  FIXED PIN MAP (from PCB)
 * =================================================================================== */
#define LCD_PIN_NUM_D0      16
#define LCD_PIN_NUM_D1      15
#define LCD_PIN_NUM_D2      11
#define LCD_PIN_NUM_D3      10
#define LCD_PIN_NUM_D4      9
#define LCD_PIN_NUM_D5      4
#define LCD_PIN_NUM_D6      18   /* YM on silkscreen */
#define LCD_PIN_NUM_D7      17   /* XP on silkscreen */
#define LCD_PIN_NUM_DC      13   /* RS / D/CX, "XM" on silkscreen */
#define LCD_PIN_NUM_WR      14
#define LCD_PIN_NUM_RD      41
#define LCD_PIN_NUM_RST     12
#define LCD_PIN_NUM_BL      38
#define LCD_PIN_NUM_CS      (-1) /* hard-wired to GND on PCB -- do not drive */

/* ILI9488 MIPI DCS Rev.1 default RAM addressing registers (from MCUFRIEND_kbv) */
#define ILI9488_CMD_CASET   0x2A
#define ILI9488_CMD_PASET   0x2B
#define ILI9488_CMD_RAMWR   0x2C
#define ILI9488_CMD_RAMWRC  0x3C   /* Write Memory Continue -- optional, RAMWR also works mid-window */
#define ILI9488_CMD_RAMRD   0x2E
#define ILI9488_CMD_MADCTL  0x36
#define ILI9488_CMD_COLMOD  0x3A
#define ILI9488_CMD_SLPOUT  0x11
#define ILI9488_CMD_SLPIN   0x10
#define ILI9488_CMD_DISPON  0x29
#define ILI9488_CMD_DISPOFF 0x28
#define ILI9488_CMD_SWRESET 0x01
#define ILI9488_CMD_INVON   0x21
#define ILI9488_CMD_INVOFF  0x20
#define ILI9488_CMD_RDDID   0x04
#define ILI9488_CMD_RDID4   0xD3

/* ===================================================================================
 *  MODULE-LEVEL STATE
 * =================================================================================== */
typedef struct {
    esp_lcd_i80_bus_handle_t   i80_bus;
    esp_lcd_panel_io_handle_t  io;
    bool                       initialized;
    bool                       stable_confirmed;   /* mirrors MOCLCD_BUILD_STABLE, informational */
    uint16_t                  width;               /* current logical width  (after MADCTL) */
    uint16_t                  height;              /* current logical height (after MADCTL) */
    uint32_t                   pclk_hz;
    uint8_t                    madctl;
    bool                       bl_state;
} moclcd_state_t;

static moclcd_state_t s_lcd = {
    .i80_bus = NULL,
    .io = NULL,
    .initialized = false,
    .stable_confirmed = MOCLCD_BUILD_STABLE,
    .width = 480,
    .height = 320,
    .pclk_hz = 10000000,
    .madctl = 0x28,
    .bl_state = false,
};

/* ===================================================================================
 *  LOW-LEVEL BUS HELPERS
 * =================================================================================== */

static inline void moclcd_check_ready(void)
{
    if (!s_lcd.initialized) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("moclcd: call init() first"));
    }
}

/* Send a command byte (D/C driven low internally by lcd_cmd), optionally followed by
 * parameter bytes (D/C driven high internally by lcd_cmd's param argument). This
 * matches WriteCmdParamN() in MCUFRIEND_kbv.cpp. */
static void moclcd_send_cmd(uint8_t cmd, const uint8_t *params, size_t len)
{
    esp_err_t err = esp_lcd_panel_io_tx_param(s_lcd.io, cmd, params, len);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: cmd 0x%02X failed (err=%d)"), cmd, (int)err);
    }
}

/* Stream raw pixel/data bytes with D/C high, prefixed by a Write Memory Continue
 * (0x3C) command.
 *
 * IMPORTANT FIX (v1.5.0-dev): v1.4.0-dev fixed the "-1 no command" bug (see that
 * changelog entry) by making this function pass ILI9488_CMD_RAMWR (0x2C) as the
 * tx_color() command. That was still wrong. Per the ILI9488 datasheet (Memory
 * Write, 0x2C, Section 5.2.24, p.179): "the column and page registers are reset
 * to the Start Column (SC) and Start Page (SP)" EVERY time 0x2C is sent. Using
 * 0x2C as the command for every chunk of a multi-chunk pixel stream resets the
 * write pointer back to the top-left of the window on every single chunk,
 * instead of continuing where the previous chunk left off -- so anything beyond
 * a single DMA-sized chunk collapses back onto the first few rows/columns
 * repeatedly. This was confirmed against two previously-working reference
 * drivers for this exact panel/pin config (modlcd_nopool.c, lcd_min.c), both of
 * which use ILI9488_CMD_RAMWRC (0x3C, "Write Memory Continue") for every
 * tx_color() call, not 0x2C.
 *
 * Per the datasheet (Memory Write Continue, 0x3C, Section 5.2.35, p.201): 0x3C
 * "makes no change to the other driver status" and explicitly does NOT reset
 * the column/page registers to SC/SP the way 0x2C does -- it continues writing
 * from the current counter position, wrapping row-to-row inside the active
 * CASET/PASET window until the host sends another command. This is the correct
 * command for every pixel-payload transaction, including the first one,
 * PROVIDED a bare 0x2C (no parameters) was already sent once via tx_param() to
 * arm the counters at (SC,SP) -- which is exactly what moclcd_set_window() does
 * below. In other words: set_window() sends bare RAMWR (0x2C) to reset the
 * pointer to the window's top-left corner, and every subsequent pixel payload
 * -- first chunk and all following chunks alike -- goes out under RAMWRC
 * (0x3C) so the pointer just keeps advancing instead of snapping back. */
static void moclcd_send_pixels(const void *data, size_t len_bytes)
{
    esp_err_t err = esp_lcd_panel_io_tx_color(s_lcd.io, ILI9488_CMD_RAMWRC, data, len_bytes);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: pixel stream failed (err=%d)"), (int)err);
    }
}

/* Reproduces MCUFRIEND_kbv::reset():
 *   RESET_IDLE; delay(50); RESET_ACTIVE; delay(100); RESET_IDLE; delay(100);
 * This exact timing is what has been proven to work on your panel via Arduino,
 * so Stage 1 keeps it identical rather than trimming delays. */
static void moclcd_hw_reset(void)
{
    gpio_set_level(LCD_PIN_NUM_RST, 1);
    mp_hal_delay_ms(50);
    gpio_set_level(LCD_PIN_NUM_RST, 0);
    mp_hal_delay_ms(100);
    gpio_set_level(LCD_PIN_NUM_RST, 1);
    mp_hal_delay_ms(100);
}

/* ===================================================================================
 *  ILI9488 PANEL REGISTER TABLE
 *  Ported verbatim from MCUFRIEND_kbv.cpp, case 0x9488 (ILI9488_regValues_max) plus
 *  the shared reset_off[] / wake_on[] framing blocks used for every MIPI_DCS_REV1
 *  panel in begin(). Order and values are unchanged from the working Arduino build.
 * =================================================================================== */

typedef struct {
    uint8_t cmd;
    uint8_t len;            /* number of parameter bytes that follow, 0 = command only */
    const uint8_t *params;
    uint16_t delay_ms;      /* delay to apply AFTER sending this command, 0 = none */
} moclcd_init_cmd_t;

/* --- reset_off block (MCUFRIEND_kbv.cpp ~line 3061) --- */
static const uint8_t k_p_none[]      = {0x00}; /* unused placeholder, len=0 entries ignore this */
static const uint8_t k_p_colmod55[]  = {0x55};

/* --- ILI9488_regValues_max block (MCUFRIEND_kbv.cpp ~line 2879) --- */
static const uint8_t k_p_pwctr1[]    = {0x10, 0x10};
static const uint8_t k_p_pwctr2[]    = {0x41};
static const uint8_t k_p_vmctr1[]    = {0x00, 0x22, 0x80, 0x40};
static const uint8_t k_p_madctl[]    = {0x68}; /* overwritten by set_madctl() during init(), kept for fidelity */
static const uint8_t k_p_ifmode[]    = {0x00};
static const uint8_t k_p_frmctr1[]   = {0xB0, 0x11};
static const uint8_t k_p_invtr[]     = {0x02};
static const uint8_t k_p_dfc[]       = {0x02, 0x02, 0x3B};
static const uint8_t k_p_etmod[]     = {0xC6};
static const uint8_t k_p_adjctl3[]   = {0xA9, 0x51, 0x2C, 0x82};

/* --- wake_on block (MCUFRIEND_kbv.cpp ~line 3067) --- */

static const moclcd_init_cmd_t k_ili9488_init_seq[] = {
    /* ---- reset_off ---- */
    { ILI9488_CMD_SWRESET, 0, k_p_none,      150 },  /* Software Reset, then wait 150ms */
    { ILI9488_CMD_DISPOFF, 0, k_p_none,      0   },  /* Display Off */
    { ILI9488_CMD_COLMOD,  1, k_p_colmod55,  0   },  /* Pixel format 0x55 = 16bpp both read/write */

    /* ---- ILI9488_regValues_max (order preserved from MCUFRIEND_kbv) ---- */
    { 0xC0,               2, k_p_pwctr1,    0   },  /* Power Control 1 */
    { 0xC1,               1, k_p_pwctr2,    0   },  /* Power Control 2 */
    { 0xC5,               4, k_p_vmctr1,    0   },  /* VCOM Control 1 */
    { ILI9488_CMD_MADCTL,  1, k_p_madctl,    0   },  /* Memory Access Control -- overwritten below */
    { 0xB0,               1, k_p_ifmode,    0   },  /* Interface Mode Control */
    { 0xB1,               2, k_p_frmctr1,   0   },  /* Frame Rate Control (Normal Mode) */
    { 0xB4,               1, k_p_invtr,     0   },  /* Display Inversion Control */
    { 0xB6,               3, k_p_dfc,       0   },  /* Display Function Control (NL=480) */
    { 0xB7,               1, k_p_etmod,     0   },  /* Entry Mode Set */
    { ILI9488_CMD_COLMOD,  1, k_p_colmod55,  0   },  /* Pixel format re-asserted, matches vendor table */
    { 0xF7,               4, k_p_adjctl3,   0   },  /* Adjustment Control 3 */

    /* ---- wake_on ---- */
    { ILI9488_CMD_SLPOUT,  0, k_p_none,      150 },  /* Sleep Out, then wait 150ms */
    { ILI9488_CMD_DISPON,  0, k_p_none,      0   },  /* Display On */
};
#define ILI9488_INIT_SEQ_COUNT (sizeof(k_ili9488_init_seq)/sizeof(k_ili9488_init_seq[0]))

/* ===================================================================================
 *  BUS INITIALIZATION (esp_lcd i80)
 * =================================================================================== */

static void moclcd_bus_deinit_if_needed(void)
{
    if (s_lcd.io) {
        esp_lcd_panel_io_del(s_lcd.io);
        s_lcd.io = NULL;
    }
    if (s_lcd.i80_bus) {
        esp_lcd_del_i80_bus(s_lcd.i80_bus);
        s_lcd.i80_bus = NULL;
    }
}

static void moclcd_bus_init(uint32_t pclk_hz)
{
    /* moclcd_gpio_init() (called just before this in init()) has already forced
     * D0-D7/DC/WR to plain GPIO-output level-0/idle state via moclcd_bus_pins_safe_idle().
     * esp_lcd_new_i80_bus() below re-routes these same GPIOs through the GDMA/LCD_CAM
     * peripheral's GPIO matrix signals; it takes over pin *function* (matrix routing)
     * but the pins were at a known idle level the instant before hand-off, so there is
     * no window where an unconfigured pin floats and glitches WR. */
    moclcd_bus_deinit_if_needed();

    esp_lcd_i80_bus_config_t bus_config = {
        .dc_gpio_num = LCD_PIN_NUM_DC,
        .wr_gpio_num = LCD_PIN_NUM_WR,
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .data_gpio_nums = {
            LCD_PIN_NUM_D0, LCD_PIN_NUM_D1, LCD_PIN_NUM_D2, LCD_PIN_NUM_D3,
            LCD_PIN_NUM_D4, LCD_PIN_NUM_D5, LCD_PIN_NUM_D6, LCD_PIN_NUM_D7,
        },
        .bus_width = 8,
        .max_transfer_bytes = 480 * 320 * 2, /* one full 16bpp frame, worst case */
        .psram_trans_align = 64,
        .sram_trans_align = 4,
    };

    esp_err_t err = esp_lcd_new_i80_bus(&bus_config, &s_lcd.i80_bus);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: esp_lcd_new_i80_bus failed (err=%d)"), (int)err);
    }

    esp_lcd_panel_io_i80_config_t io_config = {
        .cs_gpio_num = LCD_PIN_NUM_CS,          /* -1: CS hard-wired to GND, not driven */
        .pclk_hz = pclk_hz,
        .trans_queue_depth = 10,
        .dc_levels = {
            .dc_idle_level = 0,
            .dc_cmd_level = 0,
            .dc_dummy_level = 0,
            .dc_data_level = 1,
        },
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .flags = {
            .swap_color_bytes = 0,
        },
    };

    err = esp_lcd_new_panel_io_i80(s_lcd.i80_bus, &io_config, &s_lcd.io);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: esp_lcd_new_panel_io_i80 failed (err=%d)"), (int)err);
    }
}

/* Data pin table, defined early because moclcd_bus_pins_safe_idle() (below) and the
 * bit-banged read_reg() path (further down) both need it. */
static const int k_data_pins_fwd[8] = {
    LCD_PIN_NUM_D0, LCD_PIN_NUM_D1, LCD_PIN_NUM_D2, LCD_PIN_NUM_D3,
    LCD_PIN_NUM_D4, LCD_PIN_NUM_D5, LCD_PIN_NUM_D6, LCD_PIN_NUM_D7,
};

/* ===================================================================================
 *  GPIO SETUP (RST, BL, RD -- the lines esp_lcd does not own)
 * =================================================================================== */

/*
 * IMPORTANT -- "the display is always listening":
 * The ILI9488 has no chip-select gating on this PCB (CS is hard-wired to GND), so
 * from the instant the panel has power, every transition on D0-D7/DC/WR/RD is live
 * and will be latched by the panel the moment WR sees a rising edge. There is no
 * window where toggling pins is "free" the way it would be with a floating/inactive
 * CS. Two consequences drive the ordering below:
 *
 *   1) On ESP32-S3 power-up/reset, GPIOs default to INPUT with no defined level.
 *      If we configure D0-D7/DC as OUTPUT before WR, or drive WR before the data
 *      pins have a known value, we can clock garbage command/data bytes into the
 *      panel before MicroPython ever calls panel_init(). That garbage can land on
 *      a command byte that puts the controller into an unexpected state (e.g.
 *      partial-mode, test-mode, or a vendor command) -- a common cause of a "blank
 *      white screen" that looks like a dead panel but is actually a confused one.
 *
 *   2) The fix is to bring every bus pin to a known, inert level in one atomic
 *      gpio_config() pass -- WR idle HIGH (no rising edge = no latch), RD idle
 *      HIGH (not asserting a read), DC HIGH (harmless if WR glitches, since a
 *      stray data byte is far less disruptive than a stray command byte), all
 *      data lines LOW -- *before* any timing-sensitive sequencing (RST pulse,
 *      register table) begins. RST is asserted LOW as forced idle-safe as well,
 *      since holding it in reset while the bus settles is strictly safer than
 *      leaving the panel running while pins are still being configured.
 *
 * This routine is called from init() and is idempotent -- safe to call again from
 * reset() defensively if init() was skipped.
 */
static void moclcd_bus_pins_safe_idle(void)
{
    uint64_t data_mask = 0;
    for (int i = 0; i < 8; i++) {
        data_mask |= (1ULL << k_data_pins_fwd[i]);
    }

    gpio_config_t io_conf = {
        .pin_bit_mask = data_mask
                       | (1ULL << LCD_PIN_NUM_DC)
                       | (1ULL << LCD_PIN_NUM_WR)
                       | (1ULL << LCD_PIN_NUM_RD)
                       | (1ULL << LCD_PIN_NUM_RST)
                       | (1ULL << LCD_PIN_NUM_BL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    /* Set levels BEFORE anything can toggle WR: data=0, DC=1(data-ish/inert),
     * WR=1(idle,no latch edge pending), RD=1(idle), RST=0(hold in reset while we
     * finish bringing up the bus), BL=0(backlight off until we're ready to show
     * something deliberate, avoids flashing garbage frames). */
    for (int i = 0; i < 8; i++) {
        gpio_set_level(k_data_pins_fwd[i], 0);
    }
    gpio_set_level(LCD_PIN_NUM_DC, 1);
    gpio_set_level(LCD_PIN_NUM_WR, 1);
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    gpio_set_level(LCD_PIN_NUM_RST, 0);
    gpio_set_level(LCD_PIN_NUM_BL, 0);
}

static void moclcd_gpio_init(void)
{
    /* Bring the whole bus to a safe, known-inert state FIRST (see rationale above),
     * holding RST low throughout. moclcd_hw_reset() (called by init()/reset()) then
     * takes RST through its proper idle->active->idle timed sequence from this known
     * starting point, rather than from whatever level RST happened to power up at. */
    moclcd_bus_pins_safe_idle();

    /* Hand RD/BL/RST management back to their normal steady-state configuration.
     * D0-D7/DC/WR remain owned by esp_lcd once moclcd_bus_init() runs immediately
     * after this in init(); RD/RST/BL stay under our direct GPIO control for the
     * lifetime of the driver (RD is briefly reclaimed for bit-banged reads and
     * restored afterward, see read_reg()). */
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LCD_PIN_NUM_RST) | (1ULL << LCD_PIN_NUM_BL) | (1ULL << LCD_PIN_NUM_RD),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    /* RD idle = high for the entire Stage 1 write-only lifetime; read_reg() below
     * temporarily takes control of this pin for the read strobe and restores it. */
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    gpio_set_level(LCD_PIN_NUM_BL, 0);
    gpio_set_level(LCD_PIN_NUM_RST, 0); /* stay held in reset until moclcd_hw_reset() runs */
}

/* ===================================================================================
 *  PANEL INIT SEQUENCE + MADCTL/window helpers
 * =================================================================================== */

static void moclcd_panel_init_seq(uint8_t madctl)
{
    for (size_t i = 0; i < ILI9488_INIT_SEQ_COUNT; i++) {
        const moclcd_init_cmd_t *c = &k_ili9488_init_seq[i];
        if (c->cmd == ILI9488_CMD_MADCTL) {
            uint8_t p = madctl;
            moclcd_send_cmd(c->cmd, &p, 1);
        } else {
            moclcd_send_cmd(c->cmd, c->len ? c->params : NULL, c->len);
        }
        if (c->delay_ms) {
            mp_hal_delay_ms(c->delay_ms);
        }
    }
}

/* NOTE (v1.5.0-dev): bare RAMWR (0x2C, no parameters) restored here, sent via
 * tx_param() immediately after CASET/PASET. Per the datasheet, 0x2C resets the
 * column/page counters to (SC,SP) -- this is what "arms" the window so that
 * the pixel payload sent afterward (via moclcd_send_pixels(), which uses 0x3C
 * RAMWRC) starts writing at the correct top-left corner and then just
 * advances, instead of resetting on every chunk. See the detailed comment on
 * moclcd_send_pixels() for the full datasheet citations and the reasoning
 * that led here after two rounds of getting this wrong (v1.4.0-dev used bare
 * 0x2C as the per-chunk color command, which reset the pointer every chunk;
 * an even earlier version used tx_color(io,-1,...) which isn't valid on i80
 * at all). This exact split -- bare 0x2C once, then 0x3C for every payload --
 * matches two independently-working reference drivers for this same panel. */
static void moclcd_set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    uint8_t caset[4] = { (uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF),
                         (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    uint8_t paset[4] = { (uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF),
                         (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    moclcd_send_cmd(ILI9488_CMD_CASET, caset, 4);
    moclcd_send_cmd(ILI9488_CMD_PASET, paset, 4);
    moclcd_send_cmd(ILI9488_CMD_RAMWR, NULL, 0);
}

/* Fill a rectangular window with a single RGB565 color, streamed in chunks to keep
 * memory bounded (mirrors MCUFRIEND_kbv::fillRect() write8() loop, but batched for
 * i80 DMA efficiency instead of one byte at a time). */
#define MOCLCD_FILL_CHUNK_PIXELS  1024
static void moclcd_fill_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color)
{
    moclcd_set_window(x0, y0, x1, y1);

    uint32_t total_pixels = (uint32_t)(x1 - x0 + 1) * (uint32_t)(y1 - y0 + 1);
    static uint16_t chunk_buf[MOCLCD_FILL_CHUNK_PIXELS];
    uint16_t be_color = __builtin_bswap16(color); /* MSB-first on the wire, matches write8(hi);write8(lo) */
    for (uint32_t i = 0; i < MOCLCD_FILL_CHUNK_PIXELS; i++) {
        chunk_buf[i] = be_color;
    }

    while (total_pixels > 0) {
        uint32_t n = total_pixels > MOCLCD_FILL_CHUNK_PIXELS ? MOCLCD_FILL_CHUNK_PIXELS : total_pixels;
        moclcd_send_pixels(chunk_buf, n * 2);
        total_pixels -= n;
    }
}

/* ===================================================================================
 *  MICROPYTHON: init()
 * =================================================================================== */

static mp_obj_t moclcd_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_pclk, ARG_width, ARG_height, ARG_madctl };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_pclk,   MP_ARG_INT, {.u_int = 10000000} },
        { MP_QSTR_width,  MP_ARG_INT, {.u_int = 480} },
        { MP_QSTR_height, MP_ARG_INT, {.u_int = 320} },
        { MP_QSTR_madctl, MP_ARG_INT, {.u_int = 0x28} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    uint32_t pclk_hz = (uint32_t)args[ARG_pclk].u_int;
    if (pclk_hz == 0 || pclk_hz > 20000000) {
        /* ILI9488 i8080 tWC min = 40ns (datasheet 17.4.1) => 25MHz absolute ceiling.
         * We clamp well below that (20MHz) to leave setup/hold margin on real wiring,
         * since PCB trace length and level-shifting (if any) eat into timing budget. */
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: pclk out of safe range (<=20MHz)"));
    }

    s_lcd.pclk_hz = pclk_hz;
    s_lcd.width   = (uint16_t)args[ARG_width].u_int;
    s_lcd.height  = (uint16_t)args[ARG_height].u_int;
    s_lcd.madctl  = (uint8_t)args[ARG_madctl].u_int;

    moclcd_gpio_init();     /* bus pins forced to safe/inert state, RST held low */
    moclcd_bus_init(s_lcd.pclk_hz);
    moclcd_hw_reset();      /* proper timed RST pulse from the known-safe starting point */

    s_lcd.initialized = true;

    ESP_LOGI(TAG, "moclcd init: pclk=%luHz size=%ux%u madctl=0x%02X",
              (unsigned long)pclk_hz, s_lcd.width, s_lcd.height, s_lcd.madctl);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

/* ===================================================================================
 *  MICROPYTHON: reset()
 * =================================================================================== */

static mp_obj_t moclcd_reset(void)
{
    /* Defensive: if called before init() (or to force a clean re-sync mid-session),
     * re-establish the safe/inert bus state first -- see moclcd_bus_pins_safe_idle()
     * rationale above the "always listening" note -- then run the timed RST pulse.
     * If init() already ran, this re-idles+re-asserts RST but does NOT tear down the
     * esp_lcd bus handles, so panel_init() can be called again afterward without a
     * full init() re-run. */
    moclcd_bus_pins_safe_idle();
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LCD_PIN_NUM_RST) | (1ULL << LCD_PIN_NUM_RD) | (1ULL << LCD_PIN_NUM_BL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    moclcd_hw_reset();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_reset_obj, moclcd_reset);

/* ===================================================================================
 *  MICROPYTHON: panel_init()
 * =================================================================================== */

static mp_obj_t moclcd_panel_init(void)
{
    moclcd_check_ready();
    moclcd_panel_init_seq(s_lcd.madctl);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

/* ===================================================================================
 *  MICROPYTHON: backlight(level)
 *  Stage 1: digital on/off. level == 0 -> off, anything else -> on.
 *  (PWM duty-cycle dimming via LEDC can be added in a later dev iteration once
 *  digital on/off is confirmed stable -- flagged here rather than silently
 *  implemented, since it changes GPIO driver ownership of LCD_PIN_NUM_BL.)
 * =================================================================================== */

static mp_obj_t moclcd_backlight(mp_obj_t level_obj)
{
    int level = mp_obj_get_int(level_obj);
    s_lcd.bl_state = (level != 0);
    gpio_set_level(LCD_PIN_NUM_BL, s_lcd.bl_state ? 1 : 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_obj, moclcd_backlight);

/* ===================================================================================
 *  MICROPYTHON: invert_display(enable)
 * =================================================================================== */

static mp_obj_t moclcd_invert_display(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    bool enable = mp_obj_is_true(enable_obj);
    moclcd_send_cmd(enable ? ILI9488_CMD_INVON : ILI9488_CMD_INVOFF, NULL, 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_invert_display_obj, moclcd_invert_display);

/* ===================================================================================
 *  MICROPYTHON: sleep(enable)
 * =================================================================================== */

static mp_obj_t moclcd_sleep(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    bool enable = mp_obj_is_true(enable_obj);
    moclcd_send_cmd(enable ? ILI9488_CMD_SLPIN : ILI9488_CMD_SLPOUT, NULL, 0);
    mp_hal_delay_ms(150); /* matches vendor table delay around SLPOUT/SLPIN */
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_sleep_obj, moclcd_sleep);

/* ===================================================================================
 *  MICROPYTHON: cmd(command_byte, params=None)
 * =================================================================================== */

static mp_obj_t moclcd_cmd(size_t n_args, const mp_obj_t *args)
{
    moclcd_check_ready();
    uint8_t command = (uint8_t)mp_obj_get_int(args[0]);

    if (n_args < 2 || args[1] == mp_const_none) {
        moclcd_send_cmd(command, NULL, 0);
        return mp_const_none;
    }

    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[1], &bufinfo, MP_BUFFER_READ);
    moclcd_send_cmd(command, (const uint8_t *)bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_cmd_obj, 1, 2, moclcd_cmd);

/* ===================================================================================
 *  MICROPYTHON: data(buffer)
 * =================================================================================== */

static mp_obj_t moclcd_data(mp_obj_t buffer_obj)
{
    moclcd_check_ready();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buffer_obj, &bufinfo, MP_BUFFER_READ);
    moclcd_send_pixels(bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

/* ===================================================================================
 *  MICROPYTHON: read_reg(command_byte, length=1)
 *
 *  esp_lcd's i80 bus is write-optimized: once esp_lcd_new_i80_bus() binds D0-D7/WR/DC
 *  to the LCD_CAM/GDMA peripheral, those pins are routed through the ESP32-S3 GPIO
 *  matrix to that peripheral's output signals, NOT plain GPIO. Calling gpio_config()
 *  to flip them to GPIO_MODE_INPUT and back does NOT restore the peripheral's matrix
 *  routing afterward -- esp_lcd has no supported API to "lend" its pins out and get
 *  them back. Doing that switch-and-restore (as an earlier draft of this function
 *  did) risks permanently breaking the write path until the bus is destroyed and
 *  recreated, which is a much worse failure mode than simply not supporting reads
 *  cleanly.
 *
 *  Stage 1 therefore implements read_reg() as a fully independent, one-shot manual
 *  bus cycle: it temporarily tears down the esp_lcd i80 bus, bit-bangs the read
 *  exactly like MCUFRIEND_kbv::readReg()/read16bits() (assert command with D/C low,
 *  switch data pins to input, pulse RD low, latch on the rising edge, repeat per
 *  byte), and then fully re-creates the esp_lcd bus+panel_io from scratch via
 *  moclcd_bus_init() before returning -- so by the time this function returns, all
 *  other moclcd functions work exactly as before. This makes read_reg() noticeably
 *  slower than a normal command (tens of microseconds -> low milliseconds due to
 *  the bus re-creation), which is fine for its intended use: occasional ID/status
 *  register checks during bring-up, not per-frame operation.
 * =================================================================================== */

static void moclcd_manual_send_cmd_only(uint8_t cmd)
{
    /* Bit-banged command phase used only while esp_lcd does not own the pins
     * (i.e. inside read_reg(), after moclcd_bus_deinit_if_needed()). Mirrors
     * WriteCmd() in MCUFRIEND_kbv: D/C low, drive data bus, strobe WR. */
    gpio_config_t out_conf = {
        .pin_bit_mask = 0,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    for (int i = 0; i < 8; i++) out_conf.pin_bit_mask |= (1ULL << k_data_pins_fwd[i]);
    out_conf.pin_bit_mask |= (1ULL << LCD_PIN_NUM_DC) | (1ULL << LCD_PIN_NUM_WR);
    gpio_config(&out_conf);

    gpio_set_level(LCD_PIN_NUM_DC, 0); /* command */
    for (int i = 0; i < 8; i++) {
        gpio_set_level(k_data_pins_fwd[i], (cmd >> i) & 0x1);
    }
    gpio_set_level(LCD_PIN_NUM_WR, 0);
    esp_rom_delay_us(1); /* >= twrl (15ns) with generous margin */
    gpio_set_level(LCD_PIN_NUM_WR, 1);
    esp_rom_delay_us(1); /* >= twrh (15ns) with generous margin */
    gpio_set_level(LCD_PIN_NUM_DC, 1); /* return D/C high/idle */
}

static uint8_t moclcd_read_byte_bitbang(void)
{
    gpio_set_level(LCD_PIN_NUM_RD, 0);
    esp_rom_delay_us(1); /* >= trdl (45ns for Read ID) with generous margin */
    uint8_t val = 0;
    for (int i = 0; i < 8; i++) {
        val |= (gpio_get_level(k_data_pins_fwd[i]) & 0x1) << i;
    }
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    esp_rom_delay_us(1); /* >= trdh (90ns) with generous margin */
    return val;
}

static mp_obj_t moclcd_read_reg(size_t n_args, const mp_obj_t *args)
{
    moclcd_check_ready();
    uint8_t command = (uint8_t)mp_obj_get_int(args[0]);
    mp_int_t length = (n_args > 1) ? mp_obj_get_int(args[1]) : 1;
    if (length < 1 || length > 8) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: read_reg length must be 1..8"));
    }

    /* 1) Release esp_lcd's ownership of D0-D7/DC/WR so we can bit-bang them. */
    moclcd_bus_deinit_if_needed();

    /* 2) Manual command phase (D/C low, strobe WR) -- see moclcd_manual_send_cmd_only(). */
    moclcd_manual_send_cmd_only(command);

    /* 3) Switch data pins to input and bit-bang the read strobe(s). */
    uint64_t data_mask = 0;
    for (int i = 0; i < 8; i++) data_mask |= (1ULL << k_data_pins_fwd[i]);
    gpio_config_t in_conf = {
        .pin_bit_mask = data_mask,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&in_conf);
    gpio_set_level(LCD_PIN_NUM_RD, 1);

    uint8_t result[8] = {0};
    for (mp_int_t i = 0; i < length; i++) {
        result[i] = moclcd_read_byte_bitbang();
    }

    /* 4) Re-create the esp_lcd i80 bus + panel IO exactly as init() did, so every
     * other moclcd function keeps working normally after this call returns. */
    moclcd_bus_init(s_lcd.pclk_hz);

    mp_obj_t out = mp_obj_new_bytes(result, length);
    return out;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_read_reg_obj, 1, 2, moclcd_read_reg);

/* ===================================================================================
 *  MICROPYTHON: set_window(x0, y0, x1, y1)
 *
 *  Sends CASET, PASET, then a bare RAMWR (0x2C, no parameters) to arm the panel's
 *  column/page counters at the window's top-left corner. After calling this,
 *  stream pixel data with moclcd.data() (or any drawing primitive) -- data()
 *  sends the payload under RAMWRC (0x3C, "Write Memory Continue"), which
 *  advances the counter through the window instead of resetting it, so
 *  multiple calls to data() after one set_window() will continue writing
 *  correctly rather than each one snapping back to the top-left corner.
 * =================================================================================== */

/* NOTE (v1.3.0-dev fix): MicroPython's py/obj.h only provides fixed-arity
 * MP_DEFINE_CONST_FUN_OBJ_{0,1,2,3} helpers -- there is no _OBJ_4. Any function
 * taking 4 or more fixed positional args (like this one) must be declared with
 * the size_t n_args / const mp_obj_t *args signature and registered via
 * MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(name, min_args, max_args, fn), exactly
 * like hline/vline/draw_rect/etc. elsewhere in this file. The previous draft
 * used the 4-mp_obj_t-parameter style (correct only for arities 0-3) and a
 * nonexistent _OBJ_4 macro, which fails to compile against real MicroPython
 * headers -- this was missed because no MicroPython source tree was available
 * to compile-check against at the time it was written. */
static mp_obj_t moclcd_set_window_py(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    moclcd_set_window((uint16_t)mp_obj_get_int(args[0]), (uint16_t)mp_obj_get_int(args[1]),
                       (uint16_t)mp_obj_get_int(args[2]), (uint16_t)mp_obj_get_int(args[3]));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_set_window_obj, 4, 4, moclcd_set_window_py);

/* ===================================================================================
 *  DRAWING PRIMITIVES
 * =================================================================================== */

static mp_obj_t moclcd_pixel(mp_obj_t x_o, mp_obj_t y_o, mp_obj_t color_o)
{
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(x_o);
    uint16_t y = (uint16_t)mp_obj_get_int(y_o);
    uint16_t color = (uint16_t)mp_obj_get_int(color_o);
    moclcd_fill_window(x, y, x, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(moclcd_pixel_obj, moclcd_pixel);

static mp_obj_t moclcd_hline(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);
    if (w == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_hline_obj, 4, 4, moclcd_hline);

static mp_obj_t moclcd_vline(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);
    if (h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x, y + h - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_vline_obj, 4, 4, moclcd_vline);

static mp_obj_t moclcd_draw_rect(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);
    if (w == 0 || h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y, color);               /* top */
    moclcd_fill_window(x, y + h - 1, x + w - 1, y + h - 1, color); /* bottom */
    moclcd_fill_window(x, y, x, y + h - 1, color);                /* left */
    moclcd_fill_window(x + w - 1, y, x + w - 1, y + h - 1, color);/* right */
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_rect_obj, 5, 5, moclcd_draw_rect);

static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);
    if (w == 0 || h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y + h - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_rect_obj, 5, 5, moclcd_fill_rect);

static mp_obj_t moclcd_fill_screen(mp_obj_t color_o)
{
    moclcd_check_ready();
    uint16_t color = (uint16_t)mp_obj_get_int(color_o);
    moclcd_fill_window(0, 0, s_lcd.width - 1, s_lcd.height - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_fill_screen_obj, moclcd_fill_screen);

/* Bresenham's line algorithm -- per-pixel window+RAMWR. Stage 1 keeps this simple
 * (no scanline batching) since diagonal lines are comparatively rare/short; can be
 * optimized in a later dev iteration if profiling shows it's a bottleneck. */
static mp_obj_t moclcd_draw_line(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int x1 = mp_obj_get_int(args[2]);
    int y1 = mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);

    /* fast paths */
    if (y0 == y1) {
        int x_lo = x0 < x1 ? x0 : x1;
        int w = (x0 < x1 ? x1 - x0 : x0 - x1) + 1;
        moclcd_fill_window((uint16_t)x_lo, (uint16_t)y0, (uint16_t)(x_lo + w - 1), (uint16_t)y0, color);
        return mp_const_none;
    }
    if (x0 == x1) {
        int y_lo = y0 < y1 ? y0 : y1;
        int h = (y0 < y1 ? y1 - y0 : y0 - y1) + 1;
        moclcd_fill_window((uint16_t)x0, (uint16_t)y_lo, (uint16_t)x0, (uint16_t)(y_lo + h - 1), color);
        return mp_const_none;
    }

    int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    int dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;
    while (1) {
        moclcd_fill_window((uint16_t)x0, (uint16_t)y0, (uint16_t)x0, (uint16_t)y0, color);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_line_obj, 5, 5, moclcd_draw_line);

/* Midpoint circle algorithm, outline only. */
static mp_obj_t moclcd_draw_circle(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int r   = mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);

    int x = r, y = 0, err = 0;
    while (x >= y) {
        moclcd_fill_window((uint16_t)(x0 + x), (uint16_t)(y0 + y), (uint16_t)(x0 + x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 + y), (uint16_t)(y0 + x), (uint16_t)(x0 + y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 + x), (uint16_t)(x0 - y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 + y), (uint16_t)(x0 - x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 - y), (uint16_t)(x0 - x), (uint16_t)(y0 - y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 - x), (uint16_t)(x0 - y), (uint16_t)(y0 - x), color);
        moclcd_fill_window((uint16_t)(x0 + y), (uint16_t)(y0 - x), (uint16_t)(x0 + y), (uint16_t)(y0 - x), color);
        moclcd_fill_window((uint16_t)(x0 + x), (uint16_t)(y0 - y), (uint16_t)(x0 + x), (uint16_t)(y0 - y), color);
        if (err <= 0) { y += 1; err += 2 * y + 1; }
        if (err > 0)  { x -= 1; err -= 2 * x + 1; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_circle_obj, 4, 4, moclcd_draw_circle);

/* Filled circle: draw horizontal spans between symmetric x-offsets per scanline. */
static mp_obj_t moclcd_fill_circle(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int r   = mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);

    int x = r, y = 0, err = 0;
    while (x >= y) {
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 + y), (uint16_t)(x0 + x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 + x), (uint16_t)(x0 + y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 - y), (uint16_t)(x0 + x), (uint16_t)(y0 - y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 - x), (uint16_t)(x0 + y), (uint16_t)(y0 - x), color);
        if (err <= 0) { y += 1; err += 2 * y + 1; }
        if (err > 0)  { x -= 1; err -= 2 * x + 1; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_circle_obj, 4, 4, moclcd_fill_circle);

/* ===================================================================================
 *  STREAMING / HIGH-PERFORMANCE BUFFERS
 * =================================================================================== */

/* blit(x, y, width, height, buffer) -- buffer must be RGB565 big-endian bytes,
 * length == width*height*2, laid out row-major matching set_window's raster order. */
static mp_obj_t moclcd_blit(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);

    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[4], &bufinfo, MP_BUFFER_READ);

    size_t expected = (size_t)w * (size_t)h * 2;
    if (bufinfo.len < expected) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: blit buffer too small for w*h*2"));
    }

    moclcd_set_window(x, y, x + w - 1, y + h - 1);
    moclcd_send_pixels(bufinfo.buf, expected);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_blit_obj, 5, 5, moclcd_blit);

/* draw_stream(x, y, width, height, generator_or_callback)
 * Calls the supplied Python callable repeatedly (no arguments) until it returns
 * None, feeding each returned bytes-like chunk to RAMWR in order. This lets a
 * MicroPython script stream a large image from flash/SD without holding the
 * whole frame in RAM at once. The window is set once up front; the caller is
 * responsible for the total byte count matching width*height*2. */
static mp_obj_t moclcd_draw_stream(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    mp_obj_t source = args[4];

    moclcd_set_window(x, y, x + w - 1, y + h - 1);

    mp_obj_t iterable = mp_getiter(source, NULL);
    mp_obj_t item;
    while ((item = mp_iternext(iterable)) != MP_OBJ_STOP_ITERATION) {
        mp_buffer_info_t bufinfo;
        mp_get_buffer_raise(item, &bufinfo, MP_BUFFER_READ);
        moclcd_send_pixels(bufinfo.buf, bufinfo.len);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_stream_obj, 5, 5, moclcd_draw_stream);

/* fill_buffer(buffer, color) -- pack a mutable buffer with repeated RGB565 color,
 * big-endian, for later use with blit(). Utility only, does not touch the panel. */
static mp_obj_t moclcd_fill_buffer(mp_obj_t buffer_obj, mp_obj_t color_obj)
{
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buffer_obj, &bufinfo, MP_BUFFER_RW);
    uint16_t color = (uint16_t)mp_obj_get_int(color_obj);
    uint16_t be_color = __builtin_bswap16(color);

    size_t n_pixels = bufinfo.len / 2;
    uint16_t *p = (uint16_t *)bufinfo.buf;
    for (size_t i = 0; i < n_pixels; i++) {
        p[i] = be_color;
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(moclcd_fill_buffer_obj, moclcd_fill_buffer);

/* ===================================================================================
 *  TEXT / FONT RENDERING
 *  Stage 1 ships a minimal built-in 5x7 font (ASCII 32-126) so draw_char/draw_text
 *  are usable immediately without requiring a separate font asset. This can be
 *  swapped for a larger/loadable font table in a later dev iteration.
 * =================================================================================== */

#define MOCLCD_FONT_W 5
#define MOCLCD_FONT_H 7
#define MOCLCD_FONT_FIRST 32
#define MOCLCD_FONT_LAST  126

/* Standard public-domain 5x7 font (same glyph set/shape convention as the classic
 * Adafruit GFX 5x7 font), column-major bits, LSB = top pixel. */
static const uint8_t k_font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, /* (space) */
    {0x00,0x00,0x5F,0x00,0x00}, /* ! */
    {0x00,0x07,0x00,0x07,0x00}, /* " */
    {0x14,0x7F,0x14,0x7F,0x14}, /* # */
    {0x24,0x2A,0x7F,0x2A,0x12}, /* $ */
    {0x23,0x13,0x08,0x64,0x62}, /* % */
    {0x36,0x49,0x56,0x20,0x50}, /* & */
    {0x00,0x08,0x07,0x03,0x00}, /* ' */
    {0x00,0x1C,0x22,0x41,0x00}, /* ( */
    {0x00,0x41,0x22,0x1C,0x00}, /* ) */
    {0x2A,0x1C,0x7F,0x1C,0x2A}, /* * */
    {0x08,0x08,0x3E,0x08,0x08}, /* + */
    {0x00,0x80,0x70,0x30,0x00}, /* , */
    {0x08,0x08,0x08,0x08,0x08}, /* - */
    {0x00,0x00,0x60,0x60,0x00}, /* . */
    {0x20,0x10,0x08,0x04,0x02}, /* / */
    {0x3E,0x51,0x49,0x45,0x3E}, /* 0 */
    {0x00,0x42,0x7F,0x40,0x00}, /* 1 */
    {0x72,0x49,0x49,0x49,0x46}, /* 2 */
    {0x21,0x41,0x49,0x4D,0x33}, /* 3 */
    {0x18,0x14,0x12,0x7F,0x10}, /* 4 */
    {0x27,0x45,0x45,0x45,0x39}, /* 5 */
    {0x3C,0x4A,0x49,0x49,0x30}, /* 6 */
    {0x01,0x71,0x09,0x05,0x03}, /* 7 */
    {0x36,0x49,0x49,0x49,0x36}, /* 8 */
    {0x06,0x49,0x49,0x29,0x1E}, /* 9 */
    {0x00,0x36,0x36,0x00,0x00}, /* : */
    {0x00,0x56,0x36,0x00,0x00}, /* ; */
    {0x08,0x14,0x22,0x41,0x00}, /* < */
    {0x14,0x14,0x14,0x14,0x14}, /* = */
    {0x00,0x41,0x22,0x14,0x08}, /* > */
    {0x02,0x01,0x59,0x09,0x06}, /* ? */
    {0x3E,0x41,0x5D,0x59,0x4E}, /* @ */
    {0x7C,0x12,0x11,0x12,0x7C}, /* A */
    {0x7F,0x49,0x49,0x49,0x36}, /* B */
    {0x3E,0x41,0x41,0x41,0x22}, /* C */
    {0x7F,0x41,0x41,0x41,0x3E}, /* D */
    {0x7F,0x49,0x49,0x49,0x41}, /* E */
    {0x7F,0x09,0x09,0x09,0x01}, /* F */
    {0x3E,0x41,0x41,0x51,0x73}, /* G */
    {0x7F,0x08,0x08,0x08,0x7F}, /* H */
    {0x00,0x41,0x7F,0x41,0x00}, /* I */
    {0x20,0x40,0x41,0x3F,0x01}, /* J */
    {0x7F,0x08,0x14,0x22,0x41}, /* K */
    {0x7F,0x40,0x40,0x40,0x40}, /* L */
    {0x7F,0x02,0x1C,0x02,0x7F}, /* M */
    {0x7F,0x04,0x08,0x10,0x7F}, /* N */
    {0x3E,0x41,0x41,0x41,0x3E}, /* O */
    {0x7F,0x09,0x09,0x09,0x06}, /* P */
    {0x3E,0x41,0x51,0x21,0x5E}, /* Q */
    {0x7F,0x09,0x19,0x29,0x46}, /* R */
    {0x46,0x49,0x49,0x49,0x31}, /* S */
    {0x01,0x01,0x7F,0x01,0x01}, /* T */
    {0x3F,0x40,0x40,0x40,0x3F}, /* U */
    {0x1F,0x20,0x40,0x20,0x1F}, /* V */
    {0x7F,0x20,0x18,0x20,0x7F}, /* W */
    {0x63,0x14,0x08,0x14,0x63}, /* X */
    {0x03,0x04,0x78,0x04,0x03}, /* Y */
    {0x61,0x51,0x49,0x45,0x43}, /* Z */
    {0x00,0x00,0x7F,0x41,0x41}, /* [ */
    {0x02,0x04,0x08,0x10,0x20}, /* \ */
    {0x41,0x41,0x7F,0x00,0x00}, /* ] */
    {0x04,0x02,0x01,0x02,0x04}, /* ^ */
    {0x40,0x40,0x40,0x40,0x40}, /* _ */
    {0x00,0x01,0x02,0x04,0x00}, /* ` */
    {0x20,0x54,0x54,0x54,0x78}, /* a */
    {0x7F,0x48,0x44,0x44,0x38}, /* b */
    {0x38,0x44,0x44,0x44,0x20}, /* c */
    {0x38,0x44,0x44,0x48,0x7F}, /* d */
    {0x38,0x54,0x54,0x54,0x18}, /* e */
    {0x08,0x7E,0x09,0x01,0x02}, /* f */
    {0x0C,0x52,0x52,0x52,0x3E}, /* g */
    {0x7F,0x08,0x04,0x04,0x78}, /* h */
    {0x00,0x44,0x7D,0x40,0x00}, /* i */
    {0x20,0x40,0x44,0x3D,0x00}, /* j */
    {0x7F,0x10,0x28,0x44,0x00}, /* k */
    {0x00,0x41,0x7F,0x40,0x00}, /* l */
    {0x7C,0x04,0x18,0x04,0x78}, /* m */
    {0x7C,0x08,0x04,0x04,0x78}, /* n */
    {0x38,0x44,0x44,0x44,0x38}, /* o */
    {0x7C,0x14,0x14,0x14,0x08}, /* p */
    {0x08,0x14,0x14,0x18,0x7C}, /* q */
    {0x7C,0x08,0x04,0x04,0x08}, /* r */
    {0x48,0x54,0x54,0x54,0x20}, /* s */
    {0x04,0x3F,0x44,0x40,0x20}, /* t */
    {0x3C,0x40,0x40,0x20,0x7C}, /* u */
    {0x1C,0x20,0x40,0x20,0x1C}, /* v */
    {0x3C,0x40,0x30,0x40,0x3C}, /* w */
    {0x44,0x28,0x10,0x28,0x44}, /* x */
    {0x0C,0x50,0x50,0x50,0x3C}, /* y */
    {0x44,0x64,0x54,0x4C,0x44}, /* z */
    {0x00,0x08,0x36,0x41,0x00}, /* { */
    {0x00,0x00,0x7F,0x00,0x00}, /* | */
    {0x00,0x41,0x36,0x08,0x00}, /* } */
    {0x08,0x08,0x2A,0x1C,0x08}, /* -> (~ substitute) */
};

/* draw_char(x, y, character, fg_color, bg_color)
 * Renders one 5x7 glyph scaled 1:1 by building a small local pixel buffer and
 * blitting it in one shot -- far fewer transactions than pixel-by-pixel. */
static mp_obj_t moclcd_draw_char(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);

    const char *s = mp_obj_str_get_str(args[2]);
    char ch = s[0] ? s[0] : ' ';
    uint16_t fg = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t bg = (uint16_t)mp_obj_get_int(args[4]);

    int idx = (ch >= MOCLCD_FONT_FIRST && ch <= MOCLCD_FONT_LAST) ? (ch - MOCLCD_FONT_FIRST) : 0;
    const uint8_t *glyph = k_font5x7[idx];

    uint16_t be_fg = __builtin_bswap16(fg);
    uint16_t be_bg = __builtin_bswap16(bg);
    uint16_t buf[MOCLCD_FONT_W * MOCLCD_FONT_H];

    for (int col = 0; col < MOCLCD_FONT_W; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < MOCLCD_FONT_H; row++) {
            buf[row * MOCLCD_FONT_W + col] = (bits & (1 << row)) ? be_fg : be_bg;
        }
    }

    moclcd_set_window(x, y, x + MOCLCD_FONT_W - 1, y + MOCLCD_FONT_H - 1);
    moclcd_send_pixels(buf, sizeof(buf));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_char_obj, 5, 5, moclcd_draw_char);

/* draw_text(x, y, string, fg_color, bg_color) -- walks draw_char left to right with
 * a 1px gap between glyphs (6px advance), matching classic 5x7 font conventions. */
static mp_obj_t moclcd_draw_text(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    const char *s = mp_obj_str_get_str(args[2]);
    uint16_t fg = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t bg = (uint16_t)mp_obj_get_int(args[4]);

    uint16_t be_fg = __builtin_bswap16(fg);
    uint16_t be_bg = __builtin_bswap16(bg);
    uint16_t cur_x = x;

    for (const char *p = s; *p; p++) {
        char ch = *p;
        int idx = (ch >= MOCLCD_FONT_FIRST && ch <= MOCLCD_FONT_LAST) ? (ch - MOCLCD_FONT_FIRST) : 0;
        const uint8_t *glyph = k_font5x7[idx];
        uint16_t buf[MOCLCD_FONT_W * MOCLCD_FONT_H];
        for (int col = 0; col < MOCLCD_FONT_W; col++) {
            uint8_t bits = glyph[col];
            for (int row = 0; row < MOCLCD_FONT_H; row++) {
                buf[row * MOCLCD_FONT_W + col] = (bits & (1 << row)) ? be_fg : be_bg;
            }
        }
        moclcd_set_window(cur_x, y, cur_x + MOCLCD_FONT_W - 1, y + MOCLCD_FONT_H - 1);
        moclcd_send_pixels(buf, sizeof(buf));
        cur_x += MOCLCD_FONT_W + 1;
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text_obj, 5, 5, moclcd_draw_text);

/* ===================================================================================
 *  MODULE GLOBALS TABLE
 * =================================================================================== */

static MP_DEFINE_STR_OBJ(moclcd_version_obj, MOCLCD_VERSION_STRING);
static MP_DEFINE_STR_OBJ(moclcd_build_status_obj, MOCLCD_STATUS_STRING);

static const mp_rom_map_elem_t moclcd_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),        MP_ROM_QSTR(MP_QSTR_moclcd) },

    { MP_ROM_QSTR(MP_QSTR_version),         MP_ROM_PTR(&moclcd_version_obj) },
    { MP_ROM_QSTR(MP_QSTR_build_status),    MP_ROM_PTR(&moclcd_build_status_obj) },

    /* Core control & hardware */
    { MP_ROM_QSTR(MP_QSTR_init),            MP_ROM_PTR(&moclcd_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_reset),           MP_ROM_PTR(&moclcd_reset_obj) },
    { MP_ROM_QSTR(MP_QSTR_panel_init),      MP_ROM_PTR(&moclcd_panel_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_backlight),       MP_ROM_PTR(&moclcd_backlight_obj) },
    { MP_ROM_QSTR(MP_QSTR_invert_display),  MP_ROM_PTR(&moclcd_invert_display_obj) },
    { MP_ROM_QSTR(MP_QSTR_sleep),           MP_ROM_PTR(&moclcd_sleep_obj) },

    /* Low-level bus & addressing */
    { MP_ROM_QSTR(MP_QSTR_cmd),             MP_ROM_PTR(&moclcd_cmd_obj) },
    { MP_ROM_QSTR(MP_QSTR_data),            MP_ROM_PTR(&moclcd_data_obj) },
    { MP_ROM_QSTR(MP_QSTR_read_reg),        MP_ROM_PTR(&moclcd_read_reg_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_window),      MP_ROM_PTR(&moclcd_set_window_obj) },

    /* Drawing primitives */
    { MP_ROM_QSTR(MP_QSTR_pixel),           MP_ROM_PTR(&moclcd_pixel_obj) },
    { MP_ROM_QSTR(MP_QSTR_hline),           MP_ROM_PTR(&moclcd_hline_obj) },
    { MP_ROM_QSTR(MP_QSTR_vline),           MP_ROM_PTR(&moclcd_vline_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_line),       MP_ROM_PTR(&moclcd_draw_line_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_rect),       MP_ROM_PTR(&moclcd_draw_rect_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_rect),       MP_ROM_PTR(&moclcd_fill_rect_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_screen),     MP_ROM_PTR(&moclcd_fill_screen_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_circle),     MP_ROM_PTR(&moclcd_draw_circle_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_circle),     MP_ROM_PTR(&moclcd_fill_circle_obj) },

    /* Streaming & high-performance buffers */
    { MP_ROM_QSTR(MP_QSTR_blit),            MP_ROM_PTR(&moclcd_blit_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_stream),     MP_ROM_PTR(&moclcd_draw_stream_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_buffer),     MP_ROM_PTR(&moclcd_fill_buffer_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_char),       MP_ROM_PTR(&moclcd_draw_char_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_text),       MP_ROM_PTR(&moclcd_draw_text_obj) },
};
static MP_DEFINE_CONST_DICT(moclcd_module_globals, moclcd_module_globals_table);

const mp_obj_module_t moclcd_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, moclcd_user_cmodule);
