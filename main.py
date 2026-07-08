from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import threading
import asyncio
import cv2
import time
import os

from app.database import Base, engine
from app.routers import history
import backend

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

ws_manager = ConnectionManager()
shared_loop = None

def send_ws_event(event_data: dict):
    if shared_loop and shared_loop.is_running():
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(event_data), shared_loop)

RTSP_URL = "rtsp://thangdapoet:camera1511@192.168.1.12:554/stream1"
latest_jpeg = None

def capture_camera():
    global latest_jpeg
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    
    while True:
        success, frame = cap.read()
        if success:
            _, buffer = cv2.imencode('.jpg', frame)
            latest_jpeg = buffer.tobytes()
            backend.current_frame = frame
        else:
            cap.release()
            time.sleep(2)
            cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)

def generate_video():
    global latest_jpeg
    while True:
        if latest_jpeg is not None:
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + latest_jpeg + b'\r\n'
            )
        time.sleep(0.05)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_loop
    shared_loop = asyncio.get_running_loop()

    backend.send_event_callback = send_ws_event
    
    threading.Thread(target=capture_camera, daemon=True).start()
    threading.Thread(target=backend.start_mqtt_background, daemon=True).start()
    
    yield 

app = FastAPI(title="SmartLock API", lifespan=lifespan)
app.include_router(history.router)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dirs = ["known_faces", "accepted_access", "security_warnings", "temp_captures"]

for folder in static_dirs:
    folder_path = os.path.join(BASE_DIR, folder)
    os.makedirs(folder_path, exist_ok=True)
    app.mount(f"/{folder}", StaticFiles(directory=folder_path), name=folder)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "ok"}

@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        generate_video(), 
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.websocket("/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)