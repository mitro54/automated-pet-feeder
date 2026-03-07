"""Automated pet feeder controller.

Drives a 28BYJ-48 stepper motor (via ULN2003) and an LED indicator
directly from the Raspberry Pi GPIO.  Also serves a web UI for live
camera streaming, recording playback, and manual feed control.
"""

import hashlib
import json
import os
import secrets
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

# Authentication
PIN_FILE = os.path.join(BASE_DIR, ".pin")
SESSION_COOKIE = "feeder_session"

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
STEP_DELAY = 0.002 # 2 ms


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
        self.trigger_feed_requested: bool = False
        self.busy: bool = False


state = SystemState()

# ──────────────────────────────────────────────────────────────────────
# AUTHENTICATION
# ──────────────────────────────────────────────────────────────────────

_pin_hash: str = ""
_valid_sessions: set = set()
_auth_lock = threading.Lock()
_login_attempts: dict = {}  # {ip: [fail_count, lockout_timestamp]}
RATE_LIMIT_MAX = 5
RATE_LIMIT_COOLDOWN = 600  # 10 minutes


def _load_pin() -> None:
    """Load the PIN from .pin file and store its hash."""
    global _pin_hash
    if not os.path.exists(PIN_FILE):
        print("[AUTH] No .pin file found - access control DISABLED.")
        return
    with open(PIN_FILE) as f:
        pin = f.read().strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 6):
        print("[AUTH] PIN must be 4-6 digits. Access control DISABLED.")
        return
    _pin_hash = hashlib.sha256(pin.encode()).hexdigest()
    print("[AUTH] PIN loaded. Access control enabled.")


LOGIN_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pi Guard - Login</title>
    <style>
        body {
            background: #0d0d0d;
            color: #fff;
            font-family: sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .login-box {
            text-align: center;
            padding: 40px;
            background: #1a1a1a;
            border-radius: 10px;
            border: 1px solid #333;
            min-width: 280px;
        }
        h1 { margin: 0 0 8px; font-size: 28px; }
        .subtitle { color: #888; margin: 0 0 24px; font-size: 14px; }
        input {
            padding: 15px;
            font-size: 24px;
            text-align: center;
            letter-spacing: 8px;
            background: #0d0d0d;
            border: 2px solid #333;
            border-radius: 5px;
            color: #fff;
            width: 180px;
            display: block;
            margin: 0 auto;
        }
        input:focus { border-color: #1976d2; outline: none; }
        button {
            display: block;
            margin: 20px auto 0;
            padding: 12px 40px;
            background: #1976d2;
            border: none;
            border-radius: 5px;
            color: #fff;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
        }
        button:hover { background: #1565c0; }
        .error {
            color: #d32f2f;
            margin-top: 12px;
            font-size: 14px;
            display: none;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Pi Guard</h1>
        <p class="subtitle">Enter PIN to continue</p>
        <form id="f">
            <input type="password" id="p" maxlength="6"
                   pattern="[0-9]*" inputmode="numeric"
                   placeholder="----" autofocus>
            <button type="submit">Enter</button>
        </form>
        <p class="error" id="e">Wrong PIN</p>
    </div>
    <script>
        document.getElementById('f').onsubmit = async (e) => {
            e.preventDefault();
            const el = document.getElementById('e');
            const inp = document.getElementById('p');
            const r = await fetch('/auth', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({pin: inp.value})
            });
            if (r.ok) { location.reload(); }
            else if (r.status === 429) {
                el.textContent = 'Too many attempts. Try again in 10 minutes.';
                el.style.display = 'block';
                inp.value = '';
                inp.disabled = true;
                document.querySelector('button').disabled = true;
            } else {
                el.textContent = 'Wrong PIN';
                el.style.display = 'block';
                inp.value = '';
                inp.focus();
            }
        };
    </script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────────────
# WEB SERVER
# ──────────────────────────────────────────────────────────────────────


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle web requests in separate threads to prevent blocking."""

    daemon_threads = True


class WebHandler(SimpleHTTPRequestHandler):
    """Custom handler for the control-panel API and static files."""

    # -- authentication ------------------------------------------------

    def _is_authenticated(self) -> bool:
        """Check for a valid session cookie.  Localhost is always allowed."""
        if not _pin_hash:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith(f"{SESSION_COOKIE}="):
                token = part[len(SESSION_COOKIE) + 1:]
                return token in _valid_sessions
        return False

    def _serve_login_page(self) -> None:
        """Send the PIN entry page."""
        body = LOGIN_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def end_headers(self) -> None:
        """Inject no-cache headers for images and the main page."""
        if self.path.endswith((".jpg", ".html")):
            self.send_header(
                "Cache-Control",
                "no-store, no-cache, must-revalidate",
            )
        super().end_headers()

    # -- request routing -----------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests (authentication only)."""
        try:
            if self.path == "/auth":
                ip = self.client_address[0]
                now = time.time()

                # --- Rate limiting ------------------------------------
                with _auth_lock:
                    if ip in _login_attempts:
                        fails, lockout = _login_attempts[ip]
                        if fails >= RATE_LIMIT_MAX and now < lockout:
                            self.send_response(429)
                            self.end_headers()
                            print(f"[AUTH] Rate limited: {ip}")
                            return
                        if now >= lockout and fails >= RATE_LIMIT_MAX:
                            del _login_attempts[ip]

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                data = json.loads(body)
                pin = data.get("pin", "")
                submitted = hashlib.sha256(pin.encode()).hexdigest()

                if submitted == _pin_hash:
                    token = secrets.token_hex(16)
                    _valid_sessions.add(token)
                    with _auth_lock:
                        _login_attempts.pop(ip, None)
                    self.send_response(200)
                    self.send_header(
                        "Set-Cookie",
                        f"{SESSION_COOKIE}={token}; Path=/;"
                        f" HttpOnly; SameSite=Strict",
                    )
                    self.end_headers()
                    print(f"[AUTH] Login OK from {ip}")
                else:
                    with _auth_lock:
                        fails, _ = _login_attempts.get(ip, (0, 0))
                        fails += 1
                        lockout = now + RATE_LIMIT_COOLDOWN if fails >= RATE_LIMIT_MAX else 0
                        _login_attempts[ip] = (fails, lockout)
                    self.send_response(401)
                    self.end_headers()
                    print(
                        f"[AUTH] Failed login from {ip}"
                        f" ({fails}/{RATE_LIMIT_MAX})"
                    )
                return
            self.send_response(404)
            self.end_headers()
        except Exception as exc:
            print(f"[ERR] POST Error: {exc}")

    def do_GET(self) -> None:  # noqa: N802
        try:
            # --- Authentication gate ----------------------------------
            if not self._is_authenticated():
                if self.path == "/auth":
                    pass  # handled by do_POST
                else:
                    self._serve_login_page()
                    return

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
                        "/trigger_feed",
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

            # --- Stream proxy (authenticated) -------------------------
            if self.path == "/stream":
                self._proxy_stream()
                return

            if self.path.startswith("/snapshot"):
                self._proxy_snapshot()
                return

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
            "busy": state.busy,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- stream proxy --------------------------------------------------

    def _proxy_stream(self) -> None:
        """Proxy the MJPEG stream from the local streamer."""
        stream_url = f"http://localhost:{STREAM_PORT}/?action=stream"
        upstream = None
        try:
            upstream = urllib.request.urlopen(stream_url, timeout=5)
            content_type = upstream.headers.get(
                "Content-Type", "multipart/x-mixed-replace"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        except Exception:
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass
        finally:
            if upstream:
                upstream.close()

    def _proxy_snapshot(self) -> None:
        """Proxy a single snapshot from the local streamer."""
        try:
            with urllib.request.urlopen(SNAPSHOT_URL, timeout=2) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(502)
            self.end_headers()


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

        # Write frame count so the flipbook player knows the real total.
        info_path = os.path.join(TEMP_DIR, "info.json")
        with open(info_path, "w") as fh:
            json.dump({"frames": captured_frames, "total": target_frames}, fh)

        # Buffer swap - move old recording out, new recording in.
        print("[REC] Swapping buffers ...")
        trash_dir = os.path.join(BASE_DIR, "trash_bin")
        self._safe_rmtree(trash_dir)

        if os.path.exists(STABLE_DIR):
            try:
                os.rename(STABLE_DIR, trash_dir)
            except OSError:
                # rename failed (web server holding a file handle).
                # Try to delete instead.
                self._safe_rmtree(STABLE_DIR)
                if os.path.exists(STABLE_DIR):
                    # Still couldn't remove it - abort swap, keep temp
                    # for the next attempt rather than losing both.
                    print("[WARN] Buffer swap aborted - stable_run locked.")
                    return

        try:
            shutil.move(TEMP_DIR, STABLE_DIR)
        except OSError as exc:
            print(f"[WARN] Buffer swap move failed: {exc}")
            return

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
        """Main logic loop - polls state and dispatches actions.

        Scheduled feeding is handled externally by cron hitting the
        /trigger_feed endpoint.  This loop only reacts to manual-mode
        and trigger-feed requests.
        """
        print("[SYS] System Online. Waiting for commands …")

        while True:
            try:
                should_manual = False
                should_feed = False

                with state_lock:
                    if state.manual_mode:
                        should_manual = True
                    elif state.trigger_feed_requested:
                        should_feed = True
                        state.trigger_feed_requested = False

                if should_manual:
                    with state_lock:
                        state.busy = True
                    self.run_manual_mode()
                    with state_lock:
                        state.busy = False
                    # Do NOT drain here — trigger_feed_requested may
                    # have been set to transition into a feed sequence.

                elif should_feed:
                    print("[SYS] Feed Triggered!")
                    with state_lock:
                        state.busy = True
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
    _load_pin()
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