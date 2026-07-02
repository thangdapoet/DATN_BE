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
    """Hàm callback để backend MQTT gọi khi muốn gửi tín hiệu về React UI"""
    if shared_loop and shared_loop.is_running():
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(event_data), shared_loop)

RTSP_URL = "rtsp://thangdapoet:camera1511@192.168.1.12:554/stream1"
latest_jpeg = None

def capture_camera():
    """Luồng ngầm liên tục đọc camera Tapo"""
    global latest_jpeg
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    
    while True:
        success, frame = cap.read()
        if success:
            # Mã hóa khung hình sang JPEG để phát lên giao diện React
            ret, buffer = cv2.imencode('.jpg', frame)
            latest_jpeg = buffer.tobytes()
            
            # Cập nhật khung hình thô sang module backend để AI chộp lấy khi cần
            backend.current_frame = frame
        else:
            print("⚠️ [CAMERA] Mất luồng RTSP. Đang kết nối lại...")
            cap.release()
            time.sleep(2)
            cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)

def generate_video():
    """Hàm yield ảnh liên tục cho trình duyệt (MJPEG format)"""
    global latest_jpeg
    while True:
        if latest_jpeg is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + latest_jpeg + b'\r\n')
        time.sleep(0.05) # Hạn chế tốc độ để tránh quá tải trình duyệt (~20 FPS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_loop
    shared_loop = asyncio.get_running_loop()

    print("🌐 [FASTAPI] Đang khởi tạo server và các dịch vụ ngầm...")
    
    # Cấu hình callback cho backend MQTT
    backend.send_event_callback = send_ws_event
    
    # Bật luồng chạy ngầm cho Camera Stream
    camera_thread = threading.Thread(target=capture_camera, daemon=True)
    camera_thread.start()
    
    # Bật luồng chạy ngầm cho MQTT
    mqtt_thread = threading.Thread(target=backend.start_mqtt_background, daemon=True)
    mqtt_thread.start()
    
    yield # Trả quyền điều khiển lại cho FastAPI để nhận API request
    
    print("🛑 [FASTAPI] Đang tắt server...")

app = FastAPI(title="FastAPI + SQL Server Demo", lifespan=lifespan)

app.include_router(history.router)

# Cấu hình thư mục lưu ảnh upload
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Đảm bảo các thư mục tồn tại trước khi mount (tránh lỗi nếu lỡ tay xóa)
for folder in ["known_faces", "accepted_access", "security_warnings", "temp_captures"]:
    os.makedirs(os.path.join(BASE_DIR, folder), exist_ok=True)

# Public độc lập từng thư mục trực tiếp từ BASE_DIR ra URL
app.mount("/known_faces", StaticFiles(directory=os.path.join(BASE_DIR, "known_faces")), name="known_faces")
app.mount("/accepted_access", StaticFiles(directory=os.path.join(BASE_DIR, "accepted_access")), name="accepted_access")
app.mount("/security_warnings", StaticFiles(directory=os.path.join(BASE_DIR, "security_warnings")), name="security_warnings")
app.mount("/temp_captures", StaticFiles(directory=os.path.join(BASE_DIR, "temp_captures")), name="temp_captures")
os.makedirs(BASE_DIR, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI + SQL Server!"}

@app.get("/video_feed")
def video_feed():
    """Endpoint trả về luồng video trực tiếp cho React UI"""
    return StreamingResponse(generate_video(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket để giao tiếp thời gian thực với React UI"""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Lắng nghe tin nhắn từ client (ping/pong) giữ kết nối luôn mở
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

        