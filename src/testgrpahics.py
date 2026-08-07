import time
import Graphics as gfx

def run_loading_test():
    # 1. Initialize display (480x320 landscape by default)[cite: 1]
    gfx.init_display()

    # 2. Setup screen header and dark background surface[cite: 1]
    screen = gfx.UIScreen(
        background=gfx.SURFACE,
        taskbarcolor=gfx.SURFACE_ALT,
        taskbar_text="Zeno OS — System Update",
        taskbar_text_color=gfx.WHITE
    )
    screen.start(genie=False)

    # 3. Draw a card container for the UI widgets[cite: 1]
    card = gfx.UICard(x=40, y=50, w=400, h=220, title="OTA Firmware Update", bg=gfx.SURFACE_ALT, radius=12)
    card.draw()

    # 4. Instantiate Loading Spinner & Progress Bar[cite: 1]
    spinner = gfx.UISpinner(cx=240, cy=120, radius=18, dots=8, color=gfx.BLUE, bg=gfx.SURFACE_ALT)
    progress_bar = gfx.UIProgressBar(x=70, y=200, w=340, h=16, value=0, bg=gfx.DARK_GRAY, fg=gfx.GREEN, radius=6)

    # 5. Instantiate Status Text Labels[cite: 1]
    status_text = gfx.UIText(x=70, y=175, text="Initializing transfer...", fg=gfx.TEXT_MUTED, bg=gfx.SURFACE_ALT)
    percent_text = gfx.UIText(x=360, y=175, text="  0%", fg=gfx.WHITE, bg=gfx.SURFACE_ALT)

    # Initial draw pass
    status_text.draw()
    percent_text.draw()
    progress_bar.draw()

    # Trigger initial toast notification[cite: 1]
    gfx.Toast.push("Connected to update server", kind=gfx.Toast.INFO, duration_ms=2500)

    # 6. Animation / Progress Loop[cite: 1]
    for progress in range(101):
        # Step the spinner animation phase[cite: 1]
        spinner.draw()

        # Update the progress bar fill[cite: 1]
        progress_bar.set(progress)

        # Update percentage string
        percent_text.text = "{:3d}%".format(progress)
        percent_text.draw()

        # Update status labels and send toast alerts at progress milestones[cite: 1]
        if progress == 20:
            status_text.text = "Downloading payload..."
            status_text.draw()
            gfx.Toast.push("Downloading update package...", kind=gfx.Toast.INFO)
            
        elif progress == 60:
            status_text.text = "Writing flash memory..."
            status_text.draw()
            gfx.Toast.push("Writing flash sector...", kind=gfx.Toast.WARNING)
            
        elif progress == 90:
            status_text.text = "Verifying checksum..."
            status_text.draw()

        # Process active toast timers and draw toasts[cite: 1]
        gfx.Toast.update()

        time.sleep(0.03)

    # 7. Complete State[cite: 1]
    spinner.stop()
    status_text.text = "Update successful!"
    status_text.fg = gfx.GREEN
    status_text.draw()

    gfx.Toast.push("System up to date! Ready to reboot.", kind=gfx.Toast.SUCCESS, duration_ms=4000)

    # Continue event loop briefly to let final toasts expire[cite: 1]
    for _ in range(120):
        gfx.Toast.update()
        time.sleep(0.03)

if __name__ == "__main__":
    run_loading_test()
