import os
import uuid
import json
import asyncio
import time
import base64
import random
import socket
from typing import List, Dict, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.layout import ANIMATABLE_MAPPING, STATIC_PIXELS
from app.parser import parse_q4x_data, MATRIX_WIDTH, MATRIX_HEIGHT

app = FastAPI(title="Schönherz Mátrix Mini Controller")

# Enable CORS for frontend and ESP32
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
AUDIO_DIR = os.path.join(STATIC_DIR, "audio")
DB_FILE = os.path.join(DATA_DIR, "db.json")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

# Global variables for playback state
animations_db: Dict[str, dict] = {}  # id -> metadata
play_queue: List[str] = []           # list of animation IDs in queue
active_playback = {
    "animation_id": None,
    "name": "Idle Simulation",
    "frame_index": 0,
    "elapsed_time_ms": 0,
    "is_playing": False,
    "is_idle": True,
    "audio_url": None
}

# Current physical frame buffer (48 * 96 * 3 bytes)
# Physical grid dimensions: Width 48, Height 96
PHYSICAL_WIDTH = 48
PHYSICAL_HEIGHT = 96
BUFFER_SIZE = PHYSICAL_WIDTH * PHYSICAL_HEIGHT * 3
current_physical_frame = bytearray(BUFFER_SIZE)

# Active loaded animation cache (in-memory) to avoid reloading files continuously
loaded_animations = {}  # id -> parsed_data_dict

# Connected components (windows) for idle animation
windows: List[List[tuple]] = []
window_states: List[bool] = []  # True if window light is ON

# Colors
WARM_LIGHT_COLOR = (255, 190, 70)  # RGB
BLACK_COLOR = (0, 0, 0)            # RGB

# -------------------------------------------------------------
# DATABASE FUNCTIONS
# -------------------------------------------------------------
def load_db():
    global animations_db, play_queue
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                animations_db = data.get("animations", {})
                play_queue = data.get("queue", [])
        except Exception as e:
            print("Failed to load db.json:", e)
            animations_db = {}
            play_queue = []

def save_db():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "animations": animations_db,
                "queue": play_queue
            }, f, indent=4)
    except Exception as e:
        print("Failed to save db.json:", e)

# -------------------------------------------------------------
# FACADE WINDOW GROUPING (BFS)
# -------------------------------------------------------------
# The right half of the facade sits one column left of the left half's grid, so
# the elevator gap is a single column (x=23) all the way up. Mirrors app/layout.py.
RIGHT_HALF_X = 24

def _build_rooms_cols():
    """Column groups of the 8 rooms. Each room is two adjacent windows,
    each window two columns wide."""
    rooms = []
    for r in range(1, 9):
        if r <= 4:
            w1_col = 3 * (2 * (r - 1))
            w2_col = 3 * (2 * (r - 1) + 1)
        else:
            r_prime = r - 5
            w1_col = RIGHT_HALF_X + 3 * (2 * r_prime)
            w2_col = RIGHT_HALF_X + 3 * (2 * r_prime + 1)
        rooms.append([w1_col, w1_col + 1, w2_col, w2_col + 1])
    return rooms

ROOMS_COLS = _build_rooms_cols()

# Levels below the animated floors, as (first_row, height). Mirrors app/layout.py.
STATIC_LEVELS = [(56, 2), (60, 2), (64, 2), (68, 2), (72, 2), (82, 1), (85, 3), (90, 2)]

# The one three-row level is the hall: it lights as a single unit rather than
# being split into rooms.
HALL_HEIGHT = 3

def init_windows():
    global windows, window_states

    found_windows = []

    # Animated floors are row pairs (4,5), (8,9), ... (52,53)
    levels = [(4 + 4 * fo, 2) for fo in range(13)] + STATIC_LEVELS

    for y0, height in levels:
        rows = range(y0, y0 + height)
        if height == HALL_HEIGHT:
            found_windows.append([(x, y) for y in rows
                                  for cols in ROOMS_COLS for x in cols])
        else:
            for cols in ROOMS_COLS:
                found_windows.append([(x, y) for y in rows for x in cols])
        
    windows = found_windows
    window_states = [random.random() < 0.25 for _ in range(len(windows))]
    print(f"Initialized {len(windows)} facade window components (rooms).")

# -------------------------------------------------------------
# IDLE SIMULATION UPDATE
# -------------------------------------------------------------
def update_idle_frame():
    global current_physical_frame
    
    # 5% chance to toggle a window's state to simulate natural activity
    if random.random() < 0.15:
        # Toggle 1 to 3 random windows
        num_to_toggle = random.randint(1, 3)
        for _ in range(num_to_toggle):
            idx = random.randint(0, len(windows) - 1)
            # Maintain total active lights between 15% and 40%
            active_count = sum(window_states)
            active_ratio = active_count / len(windows)
            if active_ratio > 0.40:
                # Force turn off
                window_states[idx] = False
            elif active_ratio < 0.15:
                # Force turn on
                window_states[idx] = True
            else:
                # Normal toggle
                window_states[idx] = not window_states[idx]

    # Render windows to the physical buffer
    new_frame = bytearray(BUFFER_SIZE)
    for win_idx, win_cells in enumerate(windows):
        color = WARM_LIGHT_COLOR if window_states[win_idx] else BLACK_COLOR
        for px, py in win_cells:
            offset = (py * PHYSICAL_WIDTH + px) * 3
            if offset + 2 < BUFFER_SIZE:
                new_frame[offset] = color[0]
                new_frame[offset+1] = color[1]
                new_frame[offset+2] = color[2]
                
    current_physical_frame[:] = new_frame

# Connected frame consumers (ESP32 panels and browser previews)
frame_clients: List["FrameClient"] = []
live_reload_websockets: List[WebSocket] = []

# How much unsent data a client may have queued before we start dropping frames
# for it. Two frames is enough to keep a healthy link saturated without letting
# a slow one build up a backlog.
MAX_BUFFERED_BYTES = 2 * BUFFER_SIZE

# The check above only sees what the kernel has already refused to accept, so a
# large socket send buffer would hide seconds of backlog from us. Capping it
# keeps the in-flight data to a few frames. On a LAN this is far more than the
# bandwidth-delay product needs, so it costs a healthy client nothing.
SOCKET_SEND_BUFFER_BYTES = 64 * 1024


def _find_transport(websocket: WebSocket):
    """Dig out the asyncio transport behind a websocket connection.

    Neither ASGI nor Starlette exposes any flow control, and Starlette wraps
    uvicorn's send callable in a couple of closures, so walking down to the
    transport is the only way to see how much data is actually backed up for a
    client. Returns None on a server that does not work this way, in which case
    the one-slot mailbox below is the only protection.
    """
    seen = set()
    stack = [websocket._send]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))

        owner = getattr(obj, "__self__", None)
        if owner is not None and getattr(owner, "transport", None) is not None:
            return owner.transport

        for cell in getattr(obj, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if getattr(value, "transport", None) is not None:
                return value.transport
            if callable(value):
                stack.append(value)
    return None


class FrameClient:
    """One connected display (ESP32 or browser preview).

    Holds at most one pending frame. If the client has not taken the previous
    frame yet, the new one replaces it instead of queueing behind it, so a slow
    client falls behind in time but never builds up an unbounded backlog --
    it simply shows fewer frames per second, always the most recent ones.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self._pending: Optional[bytes] = None
        self._wakeup = asyncio.Event()
        self.sent = 0
        self.dropped = 0
        self.transport = _find_transport(websocket)
        self._limit_send_buffer()

    def _limit_send_buffer(self):
        """Shrink the kernel send buffer so a backlog shows up where we can see it."""
        try:
            sock = self.transport.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER_BYTES)
        except Exception:
            pass

    def _write_buffer_size(self) -> Optional[int]:
        """Bytes still queued for this connection, or None if we cannot tell."""
        try:
            return self.transport.get_write_buffer_size()
        except Exception:
            return None

    def offer(self, frame_bytes: bytes):
        """Hand the newest frame to this client, discarding any unsent one.

        If the socket is already backed up by more than MAX_BUFFERED_BYTES, the
        frame is dropped outright: a display that cannot keep up should show the
        newest frame late by one frame, not every frame late by a minute.
        """
        queued = self._write_buffer_size()
        if queued is not None and queued > MAX_BUFFERED_BYTES:
            self.dropped += 1
            return
        if self._pending is not None:
            self.dropped += 1
        self._pending = frame_bytes
        self._wakeup.set()

    async def run(self):
        """Keep sending whatever frame is pending until the socket dies."""
        try:
            while True:
                await self._wakeup.wait()
                self._wakeup.clear()
                frame, self._pending = self._pending, None
                if frame is not None:
                    await self.websocket.send_bytes(frame)
                    self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Wake the receive side so the connection gets cleaned up there
            try:
                await self.websocket.close()
            except Exception:
                pass


def broadcast_frame_ws(frame_bytes: bytes):
    """Offer a frame to every connected client. Never blocks on a slow one."""
    for client in frame_clients:
        client.offer(frame_bytes)

# -------------------------------------------------------------
# PLAYBACK ENGINE (BACKGROUND TASK)
# -------------------------------------------------------------
async def playback_loop():
    global current_physical_frame, active_playback, play_queue
    print("Playback engine started.")

    # Deadline-alapu utemezes: a kovetkezo kepkocka abszolut idopontja.
    # None = a kovetkezo kepkockanal ujra kell inditani (idle, start, stop utan).
    next_frame_at = None
    dropped_frames = 0

    while True:
        try:
            anim_id = active_playback["animation_id"]
            
            if anim_id is None:
                # Idle state
                active_playback["is_idle"] = True
                active_playback["name"] = "Idle Simulation"
                active_playback["audio_url"] = None
                active_playback["frame_index"] = 0
                active_playback["elapsed_time_ms"] = 0
                
                # Check if there is something in the queue
                if play_queue:
                    next_id = play_queue.pop(0)
                    save_db()
                    
                    if next_id in animations_db:
                        # Load animation if not cached
                        if next_id not in loaded_animations:
                            file_path = os.path.join(UPLOAD_DIR, f"{next_id}.q4x")
                            if os.path.exists(file_path):
                                try:
                                    with open(file_path, "rb") as f:
                                        loaded_animations[next_id] = parse_q4x_data(f.read())
                                except Exception as e:
                                    print(f"Error loading q4x file {next_id}: {e}")
                                    continue
                            else:
                                print(f"File not found for animation {next_id}")
                                continue
                                
                        active_playback["animation_id"] = next_id
                        active_playback["name"] = animations_db[next_id]["name"]
                        active_playback["frame_index"] = 0
                        active_playback["elapsed_time_ms"] = 0
                        active_playback["is_playing"] = True
                        active_playback["is_idle"] = False
                        next_frame_at = None  # friss utemezes az uj animaciohoz

                        audio_url = animations_db[next_id].get("audio_url")
                        active_playback["audio_url"] = audio_url
                        
                        print(f"Started playback of: {active_playback['name']}")
                        # Continue loop to render first frame immediately
                        continue
                
                # Update and sleep for idle mode
                next_frame_at = None  # az idle 0.5s-os ritmusat ne oroklje a lejatszas
                update_idle_frame()
                broadcast_frame_ws(bytes(current_physical_frame))
                await asyncio.sleep(0.5)  # Update idle simulation twice a second
                
            else:
                # Active animation playing
                anim_data = loaded_animations.get(anim_id)
                if not anim_data:
                    active_playback["animation_id"] = None
                    continue
                
                frames = anim_data["frames"]
                frame_idx = active_playback["frame_index"]
                
                if frame_idx >= len(frames):
                    # Finished playing this animation
                    print(f"Finished playback of: {active_playback['name']}")
                    active_playback["animation_id"] = None
                    active_playback["is_playing"] = False
                    next_frame_at = None
                    continue
                
                # Render current frame
                pixel_data, frame_duration_ms = frames[frame_idx]
                
                # Map 32x26 to 48x96 physical frame
                new_frame = bytearray(BUFFER_SIZE)  # defaults to 0 (black)
                # Static 'x' pixels are off during active animation, only map animatable 'p' pixels
                for (ax, ay), (px, py) in ANIMATABLE_MAPPING:
                    # 32x26 coordinates
                    anim_offset = (ay * MATRIX_WIDTH + ax) * 3
                    # Physical coordinates
                    phys_offset = (py * PHYSICAL_WIDTH + px) * 3
                    
                    if anim_offset + 2 < len(pixel_data) and phys_offset + 2 < BUFFER_SIZE:
                        new_frame[phys_offset] = pixel_data[anim_offset]
                        new_frame[phys_offset+1] = pixel_data[anim_offset+1]
                        new_frame[phys_offset+2] = pixel_data[anim_offset+2]
                
                current_physical_frame[:] = new_frame
                broadcast_frame_ws(bytes(current_physical_frame))
                
                # Sleep until the next frame's deadline. Absolute scheduling, so the
                # render + broadcast time does not add onto the frame period.
                frame_seconds = max(0.001, frame_duration_ms / 1000.0)
                if next_frame_at is None:
                    next_frame_at = time.monotonic()
                next_frame_at += frame_seconds

                delay = next_frame_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # Behind schedule. Don't let the debt accumulate: if we are more
                    # than a full frame late, resync the deadline to now.
                    if -delay > frame_seconds:
                        dropped_frames += 1
                        if dropped_frames % 25 == 1:
                            print(f"Playback behind schedule by {-delay * 1000:.0f} ms "
                                  f"({dropped_frames} late frames so far)")
                        next_frame_at = time.monotonic()
                    await asyncio.sleep(0)  # yield to the event loop

                # Advance frame index
                if active_playback["is_playing"]:
                    active_playback["frame_index"] += 1
                    active_playback["elapsed_time_ms"] += frame_duration_ms

        except Exception as e:
            print("Error in playback loop:", e)
            await asyncio.sleep(1.0)

async def live_reload_watcher():
    frontend_dir = "/workspace/frontend"
    print(f"Live reload watcher started for directory: {frontend_dir}")
    file_mtimes = {}
    
    # Initial scan
    if os.path.exists(frontend_dir):
        for root, dirs, files in os.walk(frontend_dir):
            for file in files:
                if file.endswith((".html", ".css", ".js")):
                    path = os.path.join(root, file)
                    try:
                        file_mtimes[path] = os.path.getmtime(path)
                    except Exception:
                        pass

    while True:
        await asyncio.sleep(0.5)
        if not live_reload_websockets:
            continue
            
        if not os.path.exists(frontend_dir):
            continue
            
        changed = False
        current_files = set()
        
        for root, dirs, files in os.walk(frontend_dir):
            for file in files:
                if file.endswith((".html", ".css", ".js")):
                    path = os.path.join(root, file)
                    current_files.add(path)
                    try:
                        mtime = os.path.getmtime(path)
                        if path not in file_mtimes or file_mtimes[path] < mtime:
                            file_mtimes[path] = mtime
                            changed = True
                    except Exception:
                        pass
                        
        # Check if files were deleted
        deleted_files = set(file_mtimes.keys()) - current_files
        if deleted_files:
            for path in deleted_files:
                del file_mtimes[path]
            changed = True
            
        if changed:
            print("Frontend changes detected, triggering live reload...")
            # Broadcast to all live reload sockets
            tasks = []
            for ws in list(live_reload_websockets):
                tasks.append(ws.send_text("reload"))
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for ws, result in zip(list(live_reload_websockets), results):
                    if isinstance(result, Exception):
                        try:
                            live_reload_websockets.remove(ws)
                        except ValueError:
                            pass

# -------------------------------------------------------------
# LIFECYCLE HOOKS
# -------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    load_db()
    init_windows()
    # Start the playback manager in the background
    asyncio.create_task(playback_loop())
    # Start the live reload file watcher
    asyncio.create_task(live_reload_watcher())

# -------------------------------------------------------------
# REST API ENDPOINTS
# -------------------------------------------------------------
class QueueAddRequest(BaseModel):
    animation_id: str

@app.get("/api/playback/status")
def get_playback_status():
    queue_info = []
    for q_id in play_queue:
        if q_id in animations_db:
            queue_info.append(animations_db[q_id])
            
    return {
        "status": active_playback,
        "queue": queue_info
    }

@app.post("/api/playback/skip")
def skip_playback():
    global active_playback
    if active_playback["animation_id"] is not None:
        print(f"Skipping active animation: {active_playback['name']}")
        active_playback["animation_id"] = None
        active_playback["frame_index"] = 0
        active_playback["elapsed_time_ms"] = 0
        active_playback["is_playing"] = False
        return {"status": "ok", "message": "Animation skipped"}
    return {"status": "error", "message": "No active animation to skip"}

@app.post("/api/playback/toggle")
def toggle_playback():
    global active_playback
    if active_playback["animation_id"] is not None:
        active_playback["is_playing"] = not active_playback["is_playing"]
        state = "playing" if active_playback["is_playing"] else "paused"
        return {"status": "ok", "message": f"Playback is now {state}"}
    return {"status": "error", "message": "No active animation to play/pause"}

@app.get("/api/animations")
def get_animations():
    return list(animations_db.values())

@app.post("/api/upload")
async def upload_animation(file: UploadFile = File(...)):
    if not file.filename.endswith(".q4x"):
        raise HTTPException(status_code=400, detail="Only .q4x files are allowed.")
        
    try:
        content = await file.read()
        parsed = parse_q4x_data(content)
        
        # Save Q4X file
        anim_id = str(uuid.uuid4())
        q4x_filename = f"{anim_id}.q4x"
        q4x_path = os.path.join(UPLOAD_DIR, q4x_filename)
        with open(q4x_path, "wb") as f:
            f.write(content)
            
        # Save Audio if present
        audio_url = None
        if parsed["audio_bytes"]:
            audio_filename = f"{anim_id}.{parsed['audio_ext']}"
            audio_path = os.path.join(AUDIO_DIR, audio_filename)
            with open(audio_path, "wb") as f:
                f.write(parsed["audio_bytes"])
            # Serves via FastAPI static directory
            audio_url = f"/static/audio/{audio_filename}"
            
        display_name = file.filename[:-4] if file.filename else (parsed["name"] or "Névtelen")
        
        metadata = {
            "id": anim_id,
            "name": display_name,
            "duration_ms": parsed["duration_ms"],
            "audio_url": audio_url,
            "uploaded_at": time.time()
        }
        
        animations_db[anim_id] = metadata
        save_db()
        
        loaded_animations[anim_id] = {
            "name": display_name,
            "duration_ms": parsed["duration_ms"],
            "frames": parsed["frames"]
        }
        
        return {"status": "ok", "animation": metadata}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to parse Q4X file: {str(e)}")

@app.post("/api/queue/add")
def add_to_queue(req: QueueAddRequest):
    if req.animation_id not in animations_db:
        raise HTTPException(status_code=404, detail="Animation not found")
    
    play_queue.append(req.animation_id)
    save_db()
    return {"status": "ok", "queue": play_queue}

@app.post("/api/queue/remove")
def remove_from_queue(req: QueueAddRequest):
    global play_queue
    if req.animation_id in play_queue:
        play_queue.remove(req.animation_id)
        save_db()
    return {"status": "ok", "queue": play_queue}

@app.delete("/api/animations/{animation_id}")
def delete_animation(animation_id: str):
    global play_queue
    if animation_id not in animations_db:
        raise HTTPException(status_code=404, detail="Animation not found")
        
    # Remove from queue if present
    if animation_id in play_queue:
        play_queue = [q for q in play_queue if q != animation_id]
        
    # Remove from active playback if running
    if active_playback["animation_id"] == animation_id:
        active_playback["animation_id"] = None
        active_playback["is_playing"] = False
        
    # Remove files
    q4x_path = os.path.join(UPLOAD_DIR, f"{animation_id}.q4x")
    if os.path.exists(q4x_path):
        os.remove(q4x_path)
        
    meta = animations_db[animation_id]
    if meta.get("audio_url"):
        audio_filename = meta["audio_url"].split("/")[-1]
        audio_path = os.path.join(AUDIO_DIR, audio_filename)
        if os.path.exists(audio_path):
            os.remove(audio_path)
            
    # Delete from DB and Cache
    del animations_db[animation_id]
    if animation_id in loaded_animations:
        del loaded_animations[animation_id]
        
    save_db()
    return {"status": "ok", "message": "Animation deleted"}

# -------------------------------------------------------------
# ESP32 & WEB PREVIEW STREAMING ENDPOINTS
# -------------------------------------------------------------

# 1. ESP32 endpoint: returns the 48x96 mapped physical frame as RAW binary bytes
@app.get("/api/esp/current-frame")
def get_esp_current_frame():
    from fastapi.responses import Response
    # Returns 13,824 bytes (48 * 96 * 3) of raw RGB values
    return Response(content=bytes(current_physical_frame), media_type="application/octet-stream")

# 3. ESP32 WebSocket endpoint: streams raw binary frame (13,824 bytes) to connected client
@app.websocket("/api/esp/ws")
async def esp_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = FrameClient(websocket)
    frame_clients.append(client)
    print(f"ESP32 connected via WebSocket: {websocket.client}")
    sender = asyncio.create_task(client.run())
    try:
        # Send the current state immediately upon connection
        client.offer(bytes(current_physical_frame))
        while True:
            # Keep connection open. If client sends data, just discard it.
            # If client disconnects, receive_bytes() raises WebSocketDisconnect.
            await websocket.receive_bytes()
    except WebSocketDisconnect:
        print(f"ESP32 disconnected from WebSocket: {websocket.client}")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        try:
            frame_clients.remove(client)
        except ValueError:
            pass
        sender.cancel()
        print(f"Client {websocket.client}: {client.sent} frames sent, "
              f"{client.dropped} stale frames dropped")

# 4. Live Reload WebSocket endpoint: notifies clients when frontend source files change
@app.websocket("/api/live-reload/ws")
async def live_reload_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    live_reload_websockets.append(websocket)
    try:
        while True:
            # Keep connection open. If client sends data, just discard it.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            live_reload_websockets.remove(websocket)
        except ValueError:
            pass

# 2. Web Preview endpoint: returns JSON with frame info and base64-encoded frame pixels
@app.get("/api/esp/current-frame/json")
def get_esp_current_frame_json():
    # Encode binary frame to base64 for fast transport and easy client consumption
    b64_pixels = base64.b64encode(current_physical_frame).decode("ascii")
    
    return {
        "status": active_playback,
        "width": PHYSICAL_WIDTH,
        "height": PHYSICAL_HEIGHT,
        "pixels": b64_pixels
    }

# Mount static files (audio, etc.)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
