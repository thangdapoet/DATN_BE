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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_FACES_DIR = os.path.join(BASE_DIR, "known_faces")
from pydantic import BaseModel
CONFIG_FILE = os.path.join(BASE_DIR, "web_config.json")
from app.database import Base, engine
from app.routers import history
import backend
from pydantic import BaseModel
import glob
import json

# Định nghĩa mật khẩu để truy cập thư mục người dùng trên Web
def get_web_admin_password():
    # Đọc mật khẩu từ file, nếu file chưa có thì mặc định là "admin"
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return config.get("password", "admin")
    return "admin"

def set_web_admin_password(new_password):
    # Lưu mật khẩu mới đè vào file
    with open(CONFIG_FILE, "w") as f:
        json.dump({"password": new_password}, f)
class AdminAuth(BaseModel):
    password: str
class ChangePassAuth(BaseModel):
    old_password: str
    new_password: str
class AdminAuth(BaseModel):
    password: str
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

RTSP_URL = "rtsp://thangdapoet:camera1511@192.168.1.50:554/stream1"
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

@app.get("/api/users")
def get_users():
    users = []
    # Quét toàn bộ ảnh trong thư mục known_faces
    images = glob.glob(os.path.join(KNOWN_FACES_DIR, "*.jpg"))
    for img_path in images:
        filename = os.path.basename(img_path)
        uid = filename.split('.')[0]
        
        # Chỉ lấy file ảnh gốc (UID.jpg), bỏ qua các file tự học (UID_timestamp.jpg)
        if "_" not in uid:
            users.append({
                "uid": uid,
                "image_url": f"known_faces/{filename}"
            })
    return {"users": users}
@app.post("/api/verify-admin")
def verify_admin(data: AdminAuth):
    if data.password == get_web_admin_password():
        return {"status": "success"}
    return {"status": "error", "message": "Sai mật khẩu"}
@app.post("/api/change-admin-password")
def change_admin_password(data: ChangePassAuth):
    current_pass = get_web_admin_password()
    
    if data.old_password != current_pass:
        return {"status": "error", "message": "Mật khẩu cũ không chính xác"}
    
    if len(data.new_password) < 4:
         return {"status": "error", "message": "Mật khẩu mới phải từ 4 ký tự trở lên"}
         
    set_web_admin_password(data.new_password)
    return {"status": "success", "message": "Đổi mật khẩu thành công"}