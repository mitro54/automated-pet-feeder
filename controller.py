import os
import time
import shutil
import signal
import subprocess
import threading
import urllib.request
import RPi.GPIO as GPIO
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# --- CONFIGURATION ---
TRIGGER_PIN = 17
STREAM_PORT = 8080
WEB_PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # Use current script directory
STABLE_DIR = os.path.join(BASE_DIR, "stable_run")  # What the website sees
TEMP_DIR = os.path.join(BASE_DIR, "temp_recording") # Where we record to
SNAPSHOT_URL = f"http://localhost:{STREAM_PORT}/?action=snapshot"

# Hardware commands
STREAM_CMD = (
    f'/usr/local/bin/mjpg_streamer '
    f'-i "input_uvc.so -d /dev/video0 -r 640x480 -f 10" '
    f'-o "output_http.so -p {STREAM_PORT}"'
)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle web requests in separate threads to prevent blocking."""
    pass

# - Fix 3: Thread Safety
state_lock = threading.Lock()

class SystemState:
    """Thread-safe state manager."""
    def __init__(self):
        self.manual_mode = False
        self.manual_stop_requested = False
        self.last_run_time = 0
        self.run_interval = 86400  # 24 Hours in Seconds (Production)
        self.auto_enabled = True 
        self.trigger_feed_requested = False # New flag for manual feed trigger

state = SystemState()

class WebHandler(SimpleHTTPRequestHandler):
    """Custom handler for Manual Mode API."""
    def log_message(self, format, *args):
        try:
            request_line = str(args[0])
            status_code = str(args[1])
            if ".jpg" in request_line or "snapshot" in request_line or "favicon.ico" in request_line: return
            if "GET / " in request_line and status_code == "304": return
            print(f"[WEB] {self.client_address[0]} - {format % args}")
        except Exception:
            pass

    def do_GET(self):
        try:
            with state_lock:
                if self.path == '/start_manual':
                    state.manual_mode = True
                    state.manual_stop_requested = False
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.path == '/stop_manual':
                    state.manual_stop_requested = True
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.path == '/toggle_auto':
                    state.auto_enabled = not state.auto_enabled
                    print(f"[SYS] Auto-Recording set to: {state.auto_enabled}")
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.path == '/trigger_feed':
                    state.trigger_feed_requested = True
                    if state.manual_mode:
                        state.manual_stop_requested = True # Force exit from manual loop
                    print(f"[SYS] Manual Feed Requested")
                    self.send_response(200)
                    self.end_headers()
                    return
                
                # Snapshot Status
                if self.path == '/status':
                    import json
                    status_data = {
                        "manual_mode": state.manual_mode,
                        "auto_enabled": state.auto_enabled,
                        "last_run_time": state.last_run_time
                    }
                    response = json.dumps(status_data).encode()
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return

            # Disable caching for images
            if self.path.endswith('.jpg') or self.path.endswith('.html'):
                self.send_response(200)
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                self.end_headers()
            
            super().do_GET()
            
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[ERR] Web Server Error: {e}")

class SurveillanceSystem:
    def __init__(self):
        self.streamer_process = None
        self._setup_gpio()
        self._ensure_dirs()

    def _setup_gpio(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(TRIGGER_PIN, GPIO.OUT)
        GPIO.output(TRIGGER_PIN, GPIO.LOW)

    def _ensure_dirs(self):
        if not os.path.exists(STABLE_DIR):
            os.makedirs(STABLE_DIR)

    def start_streamer(self):
        """Starts mjpg_streamer and waits for it to stabilize."""
        # Optimization: If already running and healthy, don't restart!
        if self.streamer_process and self.streamer_process.poll() is None:
            print("[SYS] Streamer already running. Reusing...")
            return

        self.stop_streamer() 
        time.sleep(2.0) # Increased from 0.5s to ensure /dev/video0 is released
        # Using preexec_fn=os.setsid specifically for Linux process group management
        self.streamer_process = subprocess.Popen(STREAM_CMD, shell=True, preexec_fn=os.setsid)
        time.sleep(3.0) 

    def stop_streamer(self):
        """Kills the streamer process."""
        if self.streamer_process:
            try:
                os.killpg(os.getpgid(self.streamer_process.pid), signal.SIGTERM)
                self.streamer_process.wait(timeout=2.0) # Wait for it to actually die
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self.streamer_process.pid), signal.SIGKILL) # Force kill if stuck
                except:
                    pass
        subprocess.call("pkill -f mjpg_streamer", shell=True)
        self.streamer_process = None # Clear the object
        time.sleep(0.5) # Extra cleanup time

    def trigger_lights(self, on=True):
        GPIO.output(TRIGGER_PIN, GPIO.HIGH if on else GPIO.LOW)

    def record_sequence(self):
        """Records 15s of footage with improved safety."""
        print("[REC] Starting Sequence...")
        
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)
        os.makedirs(TEMP_DIR)

        try:
            self.start_streamer()
            
            target_frames = 150
            frame_delay = 0.09 
            
            print("[REC] Camera Ready. Recording...")
            
            for i in range(1, target_frames + 1):
                if i == 5:
                    self.trigger_lights(on=True)

                filename = os.path.join(TEMP_DIR, f"img_{i:03d}.jpg")
                try:
                    # Fix 2: Add Timeout to prevent hanging forever
                    # urllib doesn't support timeout easily, using socket default or swapping lib
                    # Easiest standard lib way with timeout:
                    with urllib.request.urlopen(SNAPSHOT_URL, timeout=2.0) as response:
                        with open(filename, 'wb') as f:
                            f.write(response.read())
                except Exception as e:
                    # print(f"[REC] Frame Drop: {e}") # Optional debug
                    pass 
                
                time.sleep(frame_delay)

        except Exception as e:
            print(f"[REC] Critical Recording Error: {e}")
        
        finally:
            # Always clean up hardware state
            self.trigger_lights(on=False)
            self.stop_streamer()
        
        # Fix 5: Better Atomic Swap (Move old aside, move new in, del old)
        # This minimizes the time STABLE_DIR acts as 404
        print("[REC] Swapping buffers...")
        TRASH_DIR = os.path.join(BASE_DIR, "trash_bin")
        if os.path.exists(TRASH_DIR): shutil.rmtree(TRASH_DIR)
        
        # 1. Rename current stable to trash (very fast)
        if os.path.exists(STABLE_DIR):
            try:
                os.rename(STABLE_DIR, TRASH_DIR)
            except OSError:
                shutil.rmtree(STABLE_DIR) # Fallback

        # 2. Rename temp to stable (very fast)
        shutil.move(TEMP_DIR, STABLE_DIR)
        
        # 3. Cleanup trash
        if os.path.exists(TRASH_DIR): shutil.rmtree(TRASH_DIR)
        
        print("[REC] Sequence Complete. Buffer Updated.")

    def run_manual_mode(self):
        """Handles manual override."""
        print("[MANUAL] Mode Engaged. Starting Stream...")
        self.start_streamer()
        # self.trigger_lights(on=True) # DISABLED per user request
        
        should_keep_stream_alive = False

        while True:
            with state_lock:
                if state.manual_stop_requested: break
                # Check if we are exiting because of a trigger request
                if state.trigger_feed_requested:
                     should_keep_stream_alive = True
                     break
            time.sleep(0.5)
        
        print(f"[MANUAL] Stopping... (Keep Alive: {should_keep_stream_alive})")
        # self.trigger_lights(on=False) # DISABLED
        
        if not should_keep_stream_alive:
            self.stop_streamer()
            
        with state_lock:
            state.manual_mode = False
            state.manual_stop_requested = False

    def loop(self):
        """Main Logic Loop."""
        print("[SYS] System Online. Waiting...")
        while True:
            try:
                # Fix 1: Global Exception Handler for Main Loop
                should_record = False
                should_manual = False
                should_feed = False
                
                with state_lock:
                    if state.manual_mode:
                        should_manual = True
                    elif state.trigger_feed_requested:
                        should_feed = True
                        state.trigger_feed_requested = False
                    elif state.auto_enabled and (time.time() - state.last_run_time) > state.run_interval:
                        should_record = True

                if should_manual:
                    self.run_manual_mode()
                    with state_lock:
                        state.last_run_time = time.time()
                
                elif should_feed:
                    print("[SYS] Manual Feed Triggered!")
                    self.record_sequence()
                    with state_lock:
                        state.last_run_time = time.time()

                elif should_record:
                    print(f"[SYS] Auto-Schedule Triggered (Interval: {state.run_interval}s)")
                    self.record_sequence()
                    with state_lock:
                        state.last_run_time = time.time()
                
                time.sleep(1)
            except Exception as e:
                print(f"[CRITICAL] Main Loop Crash: {e}")
                time.sleep(5) # Prevent CPU spin if permanent error

# ... rest of file (run_server, main) stays similar ...

def run_server():
    os.chdir(BASE_DIR)
    server = ThreadedHTTPServer(("", WEB_PORT), WebHandler)
    print(f"[WEB] Server running on port {WEB_PORT}")
    server.serve_forever()

if __name__ == "__main__":
    try:
        # Start Web Server Thread
        web_thread = threading.Thread(target=run_server)
        web_thread.daemon = True
        web_thread.start()

        # Start Main Control Loop
        sys = SurveillanceSystem()
        sys.loop()

    except KeyboardInterrupt:
        print("\n[SYS] Shutdown.")
        GPIO.cleanup()
        subprocess.call("pkill -f mjpg_streamer", shell=True)