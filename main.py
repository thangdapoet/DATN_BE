import os
import time
import glob
import json
import random
import threading
import asyncio
import cv2

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from starlette.concurrency import run_in_threadpool
from onvif import ONVIFCamera

from app.routers import history
import backend

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_FACES_DIR = os.path.join(BASE_DIR, "known_faces")
CONFIG_FILE = os.path.join(BASE_DIR, "web_config.json")

TAPO_IP = os.getenv("TAPO_IP", "192.168.1.50")
ONVIF_PORT = 2020
TAPO_LOCAL_USER = os.getenv("TAPO_LOCAL_USER", "thangdapoet")
TAPO_LOCAL_PASS = os.getenv("TAPO_LOCAL_PASS", "15112004")
RTSP_URL = f"rtsp://{TAPO_LOCAL_USER}:{TAPO_LOCAL_PASS}@{TAPO_IP}:554/stream1"


class DoorPassRequest(BaseModel):
    new_password: str

class AdminAuth(BaseModel):
    password: str

class ChangePassAuth(BaseModel):
    old_password: str
    new_password: str


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

latest_jpeg = None

def _onvif_action(direction: str, action: str):
    cam = ONVIFCamera(TAPO_IP, ONVIF_PORT, TAPO_LOCAL_USER, TAPO_LOCAL_PASS)
    media = cam.create_media_service()
    ptz = cam.create_ptz_service()
    
    profiles = media.GetProfiles()
    token = profiles[0].token

    if action == "stop":
        ptz.Stop({'ProfileToken': token})
        return "stopped"
        
    request = ptz.create_type('ContinuousMove')
    request.ProfileToken = token
    
    speed = 0.5 
    move_map = {
        "up": {'x': 0.0, 'y': speed},
        "down": {'x': 0.0, 'y': -speed},
        "left": {'x': -speed, 'y': 0.0},
        "right": {'x': speed, 'y': 0.0},
    }
    
    if direction in move_map:
        request.Velocity = {'PanTilt': move_map[direction]}
        ptz.ContinuousMove(request)
        return f"moving_{direction}"
    
    return "invalid_direction"

def capture_camera():
    global latest_jpeg
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
    
    while True:
        success, frame = cap.read()
        if success:
            _, buffer = cv2.imencode('.jpg', frame)
            latest_jpeg = buffer.tobytes()
            backend.current_frame = frame
        else:
            cap.release()
            time.sleep(2)
            cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])

def generate_video():
    global latest_jpeg
    while True:
        if latest_jpeg is not None:
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + latest_jpeg + b'\r\n'
            )
        time.sleep(0.05)


def get_web_admin_password():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return config.get("password", "admin")
    return "admin"

def set_web_admin_password(new_password):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"password": new_password}, f)


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
    images = glob.glob(os.path.join(KNOWN_FACES_DIR, "*.jpg"))
    for img_path in images:
        filename = os.path.basename(img_path)
        uid = filename.split('.')[0]
        if "_" not in uid:
            mod_time = int(os.path.getmtime(img_path))
            users.append({
                "uid": uid,
                "image_url": f"known_faces/{filename}?v={mod_time}"
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

@app.delete("/api/users/{uid}")
def delete_user(uid: str):
    target_img_path = os.path.join(KNOWN_FACES_DIR, f"{uid}.jpg")
    if os.path.exists(target_img_path):
        os.remove(target_img_path)
        
    dynamic_imgs = glob.glob(os.path.join(KNOWN_FACES_DIR, f"{uid}_*.jpg"))
    for p in dynamic_imgs:
        try: os.remove(p)
        except: pass
        
    backend.clear_face_cache()
    backend.mqtt_client.publish(backend.MQTT_TOPIC_CMD, f"WEB_DELETE_CARD: {uid}")
    backend.create_history_record(uid, "WEB_ADMIN_DELETED", None)
 
    if backend.send_event_callback:
        backend.send_event_callback({
            "status": "ok", 
            "id": uid, 
            "message": f"Đã thu hồi hồ sơ và thẻ {uid} từ Web"
        })

    return {"status": "success", "message": f"Đã xóa người dùng {uid}"}

@app.post("/api/remote-unlock")
def remote_unlock():
    backend.mqtt_client.publish(backend.MQTT_TOPIC_CMD, "WEB_UNLOCK")
    backend.create_history_record("WEB_ADMIN", "WEB_REMOTE_UNLOCK", None)
    return {"status": "success", "message": "Đã gửi lệnh mở cửa"}

@app.post("/api/remote-stop-alarm")
def remote_stop_alarm():
    backend.mqtt_client.publish(backend.MQTT_TOPIC_CMD, "WEB_STOP_ALARM")
    backend.create_history_record("WEB_ADMIN", "WEB_STOPPED_ALARM", None)
    return {"status": "success", "message": "Đã tắt báo động"}

@app.post("/api/generate-otp")
def generate_otp():
    otp = str(random.randint(100000, 999999))
    backend.mqtt_client.publish(backend.MQTT_TOPIC_CMD, f"WEB_SET_OTP: {otp}")
    backend.create_history_record("WEB_ADMIN", "OTP_GENERATED", None)
    
    return {
        "status": "success", 
        "otp": otp, 
        "message": f"Đã cấp mã OTP: {otp} (Có hiệu lực 10 phút)"
    }

@app.post("/api/remote-change-door-pass")
async def change_door_pass(req: DoorPassRequest):
    try:
        backend.mqtt_client.publish("quangthang/smartlock/cmd", f"WEB_CHANGE_PASS: {req.new_password}")
        return {"status": "success", "message": "Đã gửi lệnh đổi mật khẩu cửa"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
@app.post("/api/camera/move")
async def move_camera(data: dict):
    direction = data.get("direction")
    action = data.get("action")
    
    try:
        onvif_res = await run_in_threadpool(_onvif_action, direction, action)
        return {"status": "success", "camera_status": onvif_res}
    except Exception as e:
        return {"status": "error", "message": str(e)}