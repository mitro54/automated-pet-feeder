"""Automated pet feeder controller.

Drives a 28BYJ-48 stepper motor (via ULN2003) and an LED indicator
directly from the Raspberry Pi GPIO.  Also serves a web UI for live
camera streaming, recording playback, and manual feed control.
"""

import json
import os
import signal
import shutil
import subprocess
import threading
import time
import urllib.request

import RPi.GPIO as GPIO
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────

# Stepper motor – 28BYJ-48 via ULN2003 driver board
MOTOR_PINS = [4, 17, 27, 22]       # IN1, IN2, IN3, IN4

# LED indicator – transistor base via 1 kΩ resistor
LED_PIN = 18                        # GPIO 18

# Networking
STREAM_PORT = 8080
WEB_PORT = 8000

# File system
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STABLE_DIR = os.path.join(BASE_DIR, "stable_run")
TEMP_DIR = os.path.join(BASE_DIR, "temp_recording")
SNAPSHOT_URL = f"http://localhost:{STREAM_PORT}/?action=snapshot"

# Hardware commands
STREAM_CMD = (
    f'/usr/local/bin/mjpg_streamer '
    f'-i "input_uvc.so -d /dev/video0 -r 640x480 -f 10" '
    f'-o "output_http.so -p {STREAM_PORT}"'
)

# ──────────────────────────────────────────────────────────────────────
# STEPPER MOTOR DRIVER
# ──────────────────────────────────────────────────────────────────────

# Half-step sequence for the 28BYJ-48 (8 phases per electrical cycle).
# 4096 half-steps = one full revolution (5.625° per step).
HALF_STEP_SEQUENCE = [
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1],
]

STEPS_PER_REVOLUTION = 4096
STEP_DELAY = 0.001  # 1 ms – safe for 5 V operation


class StepperMotor:
    """Low-level driver for a 28BYJ-48 stepper motor via ULN2003.

    All public methods are thread-safe through an internal lock.
    """

    def __init__(self, pins: list[int]) -> None:
        if len(pins) != 4:
            raise ValueError("StepperMotor requires exactly 4 GPIO pins.")
        self._pins = pins
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------

    def rotate(self, degrees: float) -> None:
        """Rotate the motor clockwise by *degrees* (positive only)."""
        if degrees <= 0:
            return
        steps = int(STEPS_PER_REVOLUTION * degrees / 360.0)
        with self._lock:
            self._step(steps)
            self._release()

    def release(self) -> None:
        """De-energise all coils (prevents overheating when idle)."""
        with self._lock:
            self._release()

    # -- internals -----------------------------------------------------

    def _step(self, count: int) -> None:
        seq_len = len(HALF_STEP_SEQUENCE)
        for i in range(count):
            phase = HALF_STEP_SEQUENCE[i % seq_len]
            for pin_index, pin in enumerate(self._pins):
                GPIO.output(pin, phase[pin_index])
            time.sleep(STEP_DELAY)

    def _release(self) -> None:
        for pin in self._pins:
            GPIO.output(pin, GPIO.LOW)


# ──────────────────────────────────────────────────────────────────────
# THREAD-SAFE APPLICATION STATE
# ──────────────────────────────────────────────────────────────────────

state_lock = threading.Lock()


class SystemState:
    """Centralised, thread-safe state for the application."""

    def __init__(self) -> None:
        self.manual_mode: bool = False
        self.manual_stop_requested: bool = False
        self.last_run_time: float = 0.0
        self.run_interval: int = 86400          # 24 h (production)
        self.auto_enabled: bool = True
        self.trigger_feed_requested: bool = False
        self.busy: bool = False


state = SystemState()

# ──────────────────────────────────────────────────────────────────────
# WEB SERVER
# ──────────────────────────────────────────────────────────────────────


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle web requests in separate threads to prevent blocking."""

    daemon_threads = True


class WebHandler(SimpleHTTPRequestHandler):
    """Custom handler for the control-panel API and static files."""

    # -- logging -------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        try:
            request_line = str(args[0])
            status_code = str(args[1])
            if any(t in request_line for t in (".jpg", "snapshot", "favicon.ico")):
                return
            if "GET / " in request_line and status_code == "304":
                return
            print(f"[WEB] {self.client_address[0]} - {format % args}")
        except Exception:
            pass

    # -- request routing -----------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            with state_lock:
                # Gate action endpoints behind the busy flag.
                # Allow /stop_manual and /trigger_feed through during
                # manual mode — they are interrupt signals, not new
                # actions.
                is_manual_interrupt = (
                    state.manual_mode
                    and self.path in ("/stop_manual", "/trigger_feed")
                )
                if (
                    state.busy
                    and not is_manual_interrupt
                    and self.path in (
                        "/start_manual", "/stop_manual",
                        "/toggle_auto", "/trigger_feed",
                    )
                ):
                    self._conflict()
                    return

                if self.path == "/start_manual":
                    state.manual_mode = True
                    state.manual_stop_requested = False
                    self._ok()
                    return

                if self.path == "/stop_manual":
                    state.manual_stop_requested = True
                    self._ok()
                    return

                if self.path == "/toggle_auto":
                    state.auto_enabled = not state.auto_enabled
                    print(
                        f"[SYS] Auto-Recording set to: {state.auto_enabled}"
                    )
                    self._ok()
                    return

                if self.path == "/trigger_feed":
                    state.trigger_feed_requested = True
                    if state.manual_mode:
                        state.manual_stop_requested = True
                    print("[SYS] Manual Feed Requested")
                    self._ok()
                    return

                if self.path == "/status":
                    self._send_status()
                    return

            # Disable caching for images and the main page.
            if self.path.endswith((".jpg", ".html")):
                self.send_response(200)
                self.send_header(
                    "Cache-Control",
                    "no-store, no-cache, must-revalidate",
                )
                self.end_headers()

            super().do_GET()

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"[ERR] Web Server Error: {exc}")

    # -- helpers -------------------------------------------------------

    def _ok(self) -> None:
        self.send_response(200)
        self.end_headers()

    def _conflict(self) -> None:
        """Return 409 Conflict when the system is busy."""
        self.send_response(409)
        self.end_headers()

    def _send_status(self) -> None:
        payload = json.dumps({
            "manual_mode": state.manual_mode,
            "auto_enabled": state.auto_enabled,
            "last_run_time": state.last_run_time,
            "busy": state.busy,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ──────────────────────────────────────────────────────────────────────
# MAIN SURVEILLANCE / FEEDER SYSTEM
# ──────────────────────────────────────────────────────────────────────


class SurveillanceSystem:
    """Orchestrates camera recording, lighting, and food dispensing."""

    def __init__(self) -> None:
        self.streamer_process: subprocess.Popen | None = None
        self._motor = StepperMotor(MOTOR_PINS)
        self._setup_gpio()
        self._ensure_dirs()

    # -- GPIO ----------------------------------------------------------

    def _setup_gpio(self) -> None:
        GPIO.setmode(GPIO.BCM)
        for pin in MOTOR_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)

    @staticmethod
    def cleanup_gpio() -> None:
        """Release all GPIO resources (call once at shutdown)."""
        GPIO.cleanup()

    # -- LED control ---------------------------------------------------

    @staticmethod
    def set_led(on: bool) -> None:
        """Turn the indicator LED on or off."""
        GPIO.output(LED_PIN, GPIO.HIGH if on else GPIO.LOW)

    # -- food dispensing -----------------------------------------------

    def dispense_food(self) -> None:
        """Rotate the feeder mechanism 90°.

        LED control is handled separately by the caller so that the
        light can stay on longer than the motor movement itself.
        """
        print("[FEED] Dispensing food …")
        self._motor.rotate(90)
        print("[FEED] Dispensing complete.")

    # -- streamer management -------------------------------------------

    def start_streamer(self) -> None:
        """Start *mjpg_streamer* and wait for it to stabilise."""
        if self.streamer_process and self.streamer_process.poll() is None:
            print("[SYS] Streamer already running. Reusing …")
            return

        self.stop_streamer()
        time.sleep(2.0)
        self.streamer_process = subprocess.Popen(
            STREAM_CMD,
            shell=True,
            preexec_fn=os.setsid,
        )
        time.sleep(3.0)

    def stop_streamer(self) -> None:
        """Kill the streamer process and all children."""
        if self.streamer_process:
            try:
                os.killpg(
                    os.getpgid(self.streamer_process.pid), signal.SIGTERM
                )
                self.streamer_process.wait(timeout=2.0)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(
                        os.getpgid(self.streamer_process.pid), signal.SIGKILL
                    )
                except ProcessLookupError:
                    pass
        subprocess.call(
            ["pkill", "-f", "mjpg_streamer"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.streamer_process = None
        time.sleep(0.5)

    # -- directory management ------------------------------------------

    @staticmethod
    def _ensure_dirs() -> None:
        os.makedirs(STABLE_DIR, exist_ok=True)

    # -- recording -----------------------------------------------------

    # Frame at which the LED turns on and the motor fires.
    _FEED_FRAME = 5
    # Extra seconds the LED stays on after the motor finishes.
    _LED_LINGER_SECS = 2.0
    # Minimum fraction of frames required to accept a recording.
    _MIN_FRAME_RATIO = 0.5

    def _check_streamer_health(self) -> None:
        """Restart the streamer if it has crashed (e.g. segfault)."""
        if (
            self.streamer_process is not None
            and self.streamer_process.poll() is not None
        ):
            print("[REC] Streamer died mid-recording. Restarting …")
            self.start_streamer()

    def record_sequence(self) -> None:
        """Record ~15 s of footage and dispense food mid-sequence.

        The motor runs in a background thread so the camera keeps
        capturing frames during the rotation (~1 s).  If the streamer
        crashes (e.g. browser disconnect causes a segfault) it is
        automatically restarted so capture can continue.

        The buffer swap only proceeds when at least 50 % of the
        expected frames were captured; otherwise the previous
        recording is preserved.

        Timeline:
          frames 1-4   : camera rolling, LED off (pre-action footage)
          frame  5     : LED on -> motor starts rotating (background)
          frames 5-N   : frames captured *while* motor is still turning
          frames N-N+22: LED lingers ~2 s after motor thread completes
          frame  N+23  : LED off
          frames ..150 : remaining post-action footage
        """
        print("[REC] Starting Sequence …")

        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)
        os.makedirs(TEMP_DIR)

        led_off_frame = None
        motor_thread = None
        captured_frames = 0
        target_frames = 150
        frame_delay = 0.09

        try:
            self.start_streamer()

            print("[REC] Camera Ready. Recording …")

            for i in range(1, target_frames + 1):
                # --- Streamer health check ----------------------------
                self._check_streamer_health()

                # --- LED on + motor (non-blocking) at the feed frame --
                if i == self._FEED_FRAME:
                    self.set_led(on=True)
                    motor_thread = threading.Thread(
                        target=self.dispense_food, daemon=True
                    )
                    motor_thread.start()

                # --- Once motor finishes, start the linger countdown --
                if (
                    motor_thread is not None
                    and not motor_thread.is_alive()
                    and led_off_frame is None
                ):
                    led_off_frame = i + int(
                        self._LED_LINGER_SECS / frame_delay
                    )

                # --- LED off after linger period ----------------------
                if led_off_frame is not None and i >= led_off_frame:
                    self.set_led(on=False)
                    led_off_frame = None

                filename = os.path.join(TEMP_DIR, f"img_{i:03d}.jpg")
                try:
                    with urllib.request.urlopen(
                        SNAPSHOT_URL, timeout=2.0
                    ) as resp:
                        with open(filename, "wb") as fh:
                            fh.write(resp.read())
                    captured_frames += 1
                except Exception:
                    pass

                time.sleep(frame_delay)

        except Exception as exc:
            print(f"[REC] Critical Recording Error: {exc}")

        finally:
            self.set_led(on=False)
            self.stop_streamer()

        # Only swap if we captured enough frames to be useful.
        min_frames = int(target_frames * self._MIN_FRAME_RATIO)
        if captured_frames < min_frames:
            print(
                f"[REC] Too few frames captured ({captured_frames}/"
                f"{target_frames}). Keeping previous recording."
            )
            self._safe_rmtree(TEMP_DIR)
            return

        # Atomic buffer swap - retry to wait out web-server file locks.
        print("[REC] Swapping buffers …")
        trash_dir = os.path.join(BASE_DIR, "trash_bin")
        self._safe_rmtree(trash_dir)

        if os.path.exists(STABLE_DIR):
            try:
                os.rename(STABLE_DIR, trash_dir)
            except OSError:
                self._safe_rmtree(STABLE_DIR)

        shutil.move(TEMP_DIR, STABLE_DIR)
        self._safe_rmtree(trash_dir)

        print("[REC] Sequence Complete. Buffer Updated.")

    @staticmethod
    def _safe_rmtree(path: str, retries: int = 5) -> None:
        """Remove a directory tree, retrying on OS-level errors.

        The web-server thread may hold transient file handles on images
        being served; a short back-off is enough to let them close.
        """
        for attempt in range(retries):
            if not os.path.exists(path):
                return
            try:
                shutil.rmtree(path)
                return
            except OSError:
                if attempt < retries - 1:
                    time.sleep(1.0)
                else:
                    print(f"[WARN] Could not remove {path} after "
                          f"{retries} attempts, skipping.")

    # -- manual mode ---------------------------------------------------

    def run_manual_mode(self) -> None:
        """Handle manual-override live-stream mode."""
        print("[MANUAL] Mode Engaged. Starting Stream …")
        self.start_streamer()

        should_keep_stream_alive = False

        while True:
            with state_lock:
                if state.manual_stop_requested:
                    break
                if state.trigger_feed_requested:
                    should_keep_stream_alive = True
                    break
            time.sleep(0.5)

        print(
            f"[MANUAL] Stopping … (Keep Alive: {should_keep_stream_alive})"
        )

        if not should_keep_stream_alive:
            self.stop_streamer()

        with state_lock:
            state.manual_mode = False
            state.manual_stop_requested = False

    # -- main loop -----------------------------------------------------

    @staticmethod
    def _drain_requests() -> None:
        """Clear any requests that piled up while the system was busy."""
        with state_lock:
            state.trigger_feed_requested = False
            state.manual_mode = False
            state.manual_stop_requested = False

    def loop(self) -> None:
        """Main logic loop - polls state and dispatches actions."""
        print("[SYS] System Online. Waiting …")

        while True:
            try:
                should_record = False
                should_manual = False
                should_feed = False

                with state_lock:
                    if state.manual_mode:
                        should_manual = True
                    elif state.trigger_feed_requested:
                        should_feed = True
                        state.trigger_feed_requested = False
                    elif (
                        state.auto_enabled
                        and (time.time() - state.last_run_time)
                        > state.run_interval
                    ):
                        should_record = True

                if should_manual:
                    with state_lock:
                        state.busy = True
                    self.run_manual_mode()
                    with state_lock:
                        state.last_run_time = time.time()
                        state.busy = False
                    # Do NOT drain here — trigger_feed_requested may
                    # have been set to transition into a feed sequence.

                elif should_feed:
                    print("[SYS] Manual Feed Triggered!")
                    with state_lock:
                        state.busy = True
                        state.last_run_time = time.time()
                    self.record_sequence()
                    with state_lock:
                        state.busy = False
                    self._drain_requests()

                elif should_record:
                    print(
                        f"[SYS] Auto-Schedule Triggered "
                        f"(Interval: {state.run_interval}s)"
                    )
                    with state_lock:
                        state.busy = True
                        state.last_run_time = time.time()
                    self.record_sequence()
                    with state_lock:
                        state.busy = False
                    self._drain_requests()

                time.sleep(1)

            except Exception as exc:
                print(f"[CRITICAL] Main Loop Crash: {exc}")
                with state_lock:
                    state.busy = False
                time.sleep(5)


# ──────────────────────────────────────────────────────────────────────
# SERVER & ENTRY POINT
# ──────────────────────────────────────────────────────────────────────


def run_server() -> None:
    """Start the threaded HTTP server for the web UI."""
    os.chdir(BASE_DIR)
    server = ThreadedHTTPServer(("", WEB_PORT), WebHandler)
    print(f"[WEB] Server running on port {WEB_PORT}")
    server.serve_forever()


def _shutdown(system: SurveillanceSystem) -> None:
    """Graceful shutdown handler."""
    print("\n[SYS] Shutdown.")
    system.stop_streamer()
    system.cleanup_gpio()
    subprocess.call(
        ["pkill", "-f", "mjpg_streamer"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    """Application entry point."""
    system = SurveillanceSystem()

    # Register SIGTERM for graceful daemon shutdown.
    signal.signal(
        signal.SIGTERM,
        lambda signum, frame: _shutdown(system),
    )

    web_thread = threading.Thread(target=run_server, daemon=True)
    web_thread.start()

    try:
        system.loop()
    except KeyboardInterrupt:
        _shutdown(system)


if __name__ == "__main__":
    main()