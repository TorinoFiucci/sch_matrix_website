import os
import uuid
import json
import asyncio
import time
import base64
import random
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
def init_windows():
    global windows, window_states
    all_coords = set()
    for (ax, ay), (px, py) in ANIMATABLE_MAPPING:
        all_coords.add((px, py))
    for px, py in STATIC_PIXELS:
        all_coords.add((px, py))

    visited = set()
    found_windows = []

    for px, py in sorted(all_coords):
        if (px, py) not in visited:
            comp = []
            queue = [(px, py)]
            visited.add((px, py))
            while queue:
                cx, cy = queue.pop(0)
                comp.append((cx, cy))
                for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in all_coords and (nx, ny) not in visited:
                        visited.add((nx, ny))
                        queue.append((nx, ny))
            found_windows.append(comp)

    windows = found_windows
    # Set initial random states: 20-30% of windows turned ON
    window_states = [random.random() < 0.25 for _ in range(len(windows))]
    print(f"Identified {len(windows)} individual facade windows.")

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

# Active websocket clients
active_websockets: List[WebSocket] = []
live_reload_websockets: List[WebSocket] = []

async def broadcast_frame_ws(frame_bytes: bytes):
    if not active_websockets:
        return
    tasks = []
    for ws in list(active_websockets):
        tasks.append(ws.send_bytes(frame_bytes))
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ws, result in zip(list(active_websockets), results):
            if isinstance(result, Exception):
                try:
                    active_websockets.remove(ws)
                except ValueError:
                    pass

# -------------------------------------------------------------
# PLAYBACK ENGINE (BACKGROUND TASK)
# -------------------------------------------------------------
async def playback_loop():
    global current_physical_frame, active_playback, play_queue
    print("Playback engine started.")
    
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
                        
                        audio_url = animations_db[next_id].get("audio_url")
                        active_playback["audio_url"] = audio_url
                        
                        print(f"Started playback of: {active_playback['name']}")
                        # Continue loop to render first frame immediately
                        continue
                
                # Update and sleep for idle mode
                update_idle_frame()
                await broadcast_frame_ws(bytes(current_physical_frame))
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
                await broadcast_frame_ws(bytes(current_physical_frame))
                
                # Sleep for the frame duration
                sleep_seconds = max(0.01, frame_duration_ms / 1000.0)
                await asyncio.sleep(sleep_seconds)
                
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
            
        # Add metadata to DB
        metadata = {
            "id": anim_id,
            "name": parsed["name"] or file.filename[:-4],
            "duration_ms": parsed["duration_ms"],
            "audio_url": audio_url,
            "uploaded_at": time.time()
        }
        
        animations_db[anim_id] = metadata
        save_db()
        
        # Cache the parsed frames
        loaded_animations[anim_id] = {
            "name": parsed["name"],
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
    active_websockets.append(websocket)
    print(f"ESP32 connected via WebSocket: {websocket.client}")
    try:
        # Send the current state immediately upon connection
        await websocket.send_bytes(bytes(current_physical_frame))
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
            active_websockets.remove(websocket)
        except ValueError:
            pass

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
