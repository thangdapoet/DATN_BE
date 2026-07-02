import paho.mqtt.client as mqtt
import cv2
import time
import os
import numpy as np
import shutil  
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
from deepface import DeepFace

from app.database import SessionLocal
from app.models import History

# BIẾN TOÀN CỤC
current_frame = None       
send_event_callback = None 

# --- 1. CẤU HÌNH HỆ THỐNG ---
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC_LOG = "quangthang/smartlock/log"
MQTT_TOPIC_CMD = "quangthang/smartlock/cmd" # Topic gửi lệnh cho phần cứng

# TẠO CLIENT MQTT TOÀN CỤC ĐỂ CÁC HÀM AI CÓ THỂ PUBLISH LỆNH
mqtt_client = mqtt.Client()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_FACES_DIR = os.path.join(BASE_DIR, "known_faces")        
ACCEPTED_DIR = os.path.join(BASE_DIR, "accepted_access")      
WARNING_DIR = os.path.join(BASE_DIR, "security_warnings")       
TEMP_DIR = os.path.join(BASE_DIR, "temp_captures")              

for folder in [KNOWN_FACES_DIR, ACCEPTED_DIR, WARNING_DIR, TEMP_DIR]:
    os.makedirs(folder, exist_ok=True)

access_history = {}

def clear_face_cache():
    """Xóa file cache của DeepFace để bắt buộc AI quét lại ảnh mới khi có thay đổi thẻ."""
    cache_file = os.path.join(KNOWN_FACES_DIR, "representations_facenet.pkl")
    if os.path.exists(cache_file):
        os.remove(cache_file)
        print("[AI] Đã xóa cache để cập nhật dữ liệu sinh trắc học mới.")

def check_anomaly(uid):
    current_time = time.time()
    if uid not in access_history:
        access_history[uid] = []
    access_history[uid].append(current_time)
    access_history[uid] = [t for t in access_history[uid] if current_time - t <= 300]
    
    swipe_count = len(access_history[uid])
    if swipe_count > 3:
        print(f"[BÁO ĐỘNG] Thẻ {uid} quẹt liên tục {swipe_count} lần trong 5 phút!")
        return True
    return False

def create_history_record(uid, status, image_url=None):
    db = SessionLocal()
    try:
        new_record = History(UID=uid, Status=status, ImageUrl=image_url)
        db.add(new_record)
        db.commit()
        db.refresh(new_record)
        print(f"[DATABASE] Đã lưu log thành công! HistoryId: {new_record.HistoryId}")
        return new_record.HistoryId
    except Exception as e:
        db.rollback()
        print(f"[DATABASE LỖI] Không thể lưu log: {e}")
        return None
    finally:
        db.close()

def update_history_record(history_id, status, image_url, final_uid=None):
    db = SessionLocal()
    try:
        record = db.query(History).filter(History.HistoryId == history_id).first()
        if record:
            record.Status = status
            record.ImageUrl = image_url
            if final_uid:
                record.UID = final_uid # Đổi tên log từ thẻ ẩn danh sang tên người tìm được
            db.commit()
            print(f"[DATABASE] Đã cập nhật trạng thái AI cho ID {history_id}: {status}")
    except Exception as e:
        db.rollback()
        print(f"[DATABASE LỖI] Không thể cập nhật trạng thái: {e}")
    finally:
        db.close()

# --- 2. HÀM CHỤP ẢNH TỪ CAMERA ---
def capture_snapshot(event_name, uid_info="", target_dir=TEMP_DIR, is_registration=False):
    global current_frame
    if current_frame is None:
        print("[LỖI] Camera chưa sẵn sàng. Đang bỏ qua chụp ảnh.")
        return None

    print(f"[CAMERA] Đang chộp khung hình từ luồng chạy ngầm...")
    frame_to_save = current_frame.copy() 
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    if is_registration and uid_info:
        file_name = f"{uid_info}.jpg"
        full_path = os.path.join(KNOWN_FACES_DIR, file_name)
        cv2.imwrite(full_path, frame_to_save)
        return f"{os.path.basename(KNOWN_FACES_DIR)}/{file_name}"
    else:
        file_name = f"{timestamp}_{event_name}_{uid_info}.jpg".replace(" ", "")
        full_path = os.path.join(target_dir, file_name)
        
    cv2.imwrite(full_path, frame_to_save)
    return f"{os.path.basename(target_dir)}/{file_name}"

# --- 3. CÁC HÀM AI NHẬN DIỆN ---
# --- 3. CÁC HÀM AI NHẬN DIỆN ---
def verify_face_ai(captured_img_path, uid, history_id):
    """Hàm AI cũ: Xác thực 1:1 dựa trên UID thẻ."""
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    known_face_path = os.path.join(KNOWN_FACES_DIR, f"{uid}.jpg")
    file_name = os.path.basename(full_captured_path)
    
    status = "DENIED"
    relative_final_path = captured_img_path
    ws_payload = None  # Thêm biến này để chuẩn bị data gửi qua WS
    
    try:
        if not os.path.exists(known_face_path):
            print(f"[AI] Thẻ {uid} chưa có ảnh gốc.")
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "NO_REGISTRATION_FACE"
            ws_payload = {"status": "bad", "id": uid, "message": "Thẻ hợp lệ nhưng chưa đăng ký khuôn mặt!"}
            return # Khối finally sẽ vẫn được chạy
            
        print(f"[AI] Đang phân tích sinh trắc học (1:1)...")
        result = DeepFace.verify(
            img1_path=full_captured_path, img2_path=known_face_path, 
            model_name="Facenet", detector_backend="mtcnn",
            distance_metric="euclidean_l2", enforce_detection=True
        )
        
        distance = result["distance"]
        if distance <= 0.75:
            print("[KẾT QUẢ] Hợp lệ!")
            final_img_path = os.path.join(ACCEPTED_DIR, file_name)
            relative_final_path = f"accepted_access/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "SUCCESS"
            # ĐÃ THÊM WS PAYLOAD CHO TRƯỜNG HỢP SUCCESS
            ws_payload = {"status": "ok", "id": uid, "message": f"Xác thực khuôn mặt thành công ({uid})"} 

        else:
            print("[KẾT QUẢ] CẢNH BÁO: Kẻ lạ!")
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "FAKE_OR_STRANGER"
            ws_payload = {"status": "bad", "id": "UNKNOWN", "message": f"CẢNH BÁO: Thẻ {uid} mở cửa. Khuôn mặt không khớp"}
            
    except ValueError:
        print("[KẾT QUẢ] TỪ CHỐI: Không tìm thấy mặt!")
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): shutil.move(full_captured_path, final_img_path)
        status = "FACE_NOT_FOUND"
        ws_payload = {"status": "bad", "id": uid, "message": "Không tìm thấy khuôn mặt"}
    except Exception as e:
        status = f"SYSTEM_ERROR"
        ws_payload = {"status": "bad", "id": uid, "message": "Lỗi hệ thống AI"}
    finally:
        # BƯỚC 1: Bắt buộc Update Database trước (Đổi từ PENDING sang SUCCESS/DENIED)
        if history_id: 
            update_history_record(history_id, status, relative_final_path)
        
        # BƯỚC 2: Lúc này mới gọi WebSocket báo cho Frontend fetch data
        if send_event_callback and ws_payload:
            send_event_callback(ws_payload)


def identify_face_ai(captured_img_path, history_id):
    """Hàm AI mới: Quét toàn bộ dữ liệu 1:N khi giữ phím #."""
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    file_name = os.path.basename(full_captured_path)
    
    status = "DENIED"
    relative_final_path = captured_img_path
    uid_found = None
    ws_payload = None # Chuẩn bị payload cho WS
    
    try:
        print("[AI] Đang quét toàn bộ danh sách để tìm khuôn mặt (1:N)...")
        dfs = DeepFace.find(
            img_path=full_captured_path, 
            db_path=KNOWN_FACES_DIR, 
            model_name="Facenet", 
            detector_backend="mtcnn",
            distance_metric="euclidean_l2", 
            enforce_detection=True,
            silent=True
        )
        
        if len(dfs) > 0 and not dfs[0].empty:
            best_match = dfs[0].iloc[0]
            distance = best_match['distance']
            matched_identity = best_match['identity']
            
            print(f"   -> Thống kê AI: Khớp với {os.path.basename(matched_identity)} (Sai lệch = {distance:.4f})")
            
            if distance <= 0.75:
                uid_found = os.path.basename(matched_identity).replace(".jpg", "")
                
                print(f"[KẾT QUẢ] Nhận diện thành công: {uid_found}!")
                final_img_path = os.path.join(ACCEPTED_DIR, file_name)
                relative_final_path = f"accepted_access/{file_name}"
                shutil.move(full_captured_path, final_img_path)
                status = "SUCCESS"
                
                # Bắn lệnh mở cửa phần cứng (Cứ cho phần cứng chạy trước cho mượt)
                mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_SUCCESS")
                
                ws_payload = {"status": "ok", "id": uid_found, "message": f"Mở cửa bằng khuôn mặt ({uid_found})"}
            else:
                raise ValueError("Không đạt ngưỡng an toàn")
        else:
             raise ValueError("Không khớp với bất kỳ ai")
             
    except ValueError as e:
        print(f"[KẾT QUẢ] TỪ CHỐI FaceID: {e}")
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): shutil.move(full_captured_path, final_img_path)
        status = "UNKNOWN_FACE"
        
        mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_DENIED")
        ws_payload = {"status": "bad", "id": "UNKNOWN", "message": "Face ID thất bại: Người lạ!"}
             
    except Exception as e:
        print(f"[AI LỖI]: {e}")
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): shutil.move(full_captured_path, final_img_path)
        status = "SYSTEM_ERROR"
        mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_DENIED")
        ws_payload = {"status": "bad", "id": "UNKNOWN", "message": "Lỗi hệ thống AI"}
    finally:
        # BƯỚC 1: Update Database trước
        if history_id: 
            update_history_record(history_id, status, relative_final_path, final_uid=uid_found)
            
        # BƯỚC 2: Bắn event WS sau khi DB đã chắc chắn update xong
        if send_event_callback and ws_payload:
            send_event_callback(ws_payload)

# --- 4. HÀM XỬ LÝ SỰ KIỆN MQTT ---
def on_connect(client, userdata, flags, rc):
    print("Đã kết nối MQTT Broker! Hệ thống AI đang lắng nghe...")
    client.subscribe(MQTT_TOPIC_LOG)
    print("-" * 50)

def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8")
    print(f"\n[SỰ KIỆN MỚI] {payload}")
    
    parts = payload.split(": ")
    event = parts[0]
    data = parts[1] if len(parts) > 1 else ""

    if event == "GRANTED_ADMIN":
        if send_event_callback: send_event_callback({"status": "ok", "id": data, "message": "Truy cập bằng thẻ Admin"})

    # --- SỰ KIỆN MỚI: NHẤN GIỮ PHÍM # ---
    elif event == "REQUEST_FACE_AUTH" and data == "HOLD":
        print("-> Nhận yêu cầu mở cửa bằng khuôn mặt (Hold #). Chờ AI...")
        # 1. Chụp hình và đưa vào Temp
        img_path = capture_snapshot("FACE_AUTH", "UNKNOWN", target_dir=TEMP_DIR)
        # 2. Ghi database
        history_id = create_history_record(uid="FACE_REC", status="PENDING", image_url=img_path)
        # 3. Phân tích ảnh 1:N
        if img_path and history_id:
            identify_face_ai(img_path, history_id)

    elif event == "ADMIN_ADDED_CARD":
        print(f"-> Đăng ký thẻ {data}...")
        img_path = capture_snapshot("REGISTRATION", data, is_registration=True)
        create_history_record(uid=data, status="ADMIN_REGISTERED", image_url=img_path)
        clear_face_cache() # Xóa cache để cập nhật AI
        if send_event_callback: send_event_callback({"status": "ok", "id": data, "message": f"Đã thêm thẻ {data}"})

    elif event == "GRANTED" and data == "PASSWORD":
        img_path = capture_snapshot("GRANTED_PASS", data, target_dir=ACCEPTED_DIR)
        create_history_record(uid="PASSWORD", status="SUCCESS", image_url=img_path)
        if send_event_callback: send_event_callback({"status": "ok", "id": "Passcode", "message": "Mở cửa bằng Mật khẩu"})

    elif event == "GRANTED" and data not in ["PASSWORD", "FACE_ID_SUCCESS"]:
        print(f"-> Quẹt thẻ {data}. Chờ AI...")
        check_anomaly(data)
        img_path = capture_snapshot("TEMP", data, target_dir=TEMP_DIR)
        history_id = create_history_record(uid=data, status="PENDING", image_url=img_path)
        if img_path and history_id:
            verify_face_ai(img_path, data, history_id)

            
    elif event == "ADMIN_DELETED_CARD":
        target_img_path = os.path.join(KNOWN_FACES_DIR, f"{data}.jpg")
        if os.path.exists(target_img_path):
            os.remove(target_img_path)
            clear_face_cache() # Xóa cache để cập nhật AI
            if send_event_callback: send_event_callback({"status": "ok", "id": data, "message": f"Đã xóa thẻ {data}"})

    elif event in ["CLONED_WARNING", "PASS_LOCKED", "RFID_LOCKED"]:
        img_path = capture_snapshot(event, data, target_dir=WARNING_DIR)
        create_history_record(uid=data, status=event, image_url=img_path)        
        if send_event_callback:
            if event == "PASS_LOCKED":
                send_event_callback({"status": "bad", "id": "UNKNOWN", "message": "Báo động: Sai mật khẩu 5 lần!"})
            elif event == "RFID_LOCKED":
                send_event_callback({"status": "bad", "id": "UNKNON", "message": "Báo động: Quẹt thẻ sai 5 lần"})
            else:
                send_event_callback({"status": "bad", "id": "UNKNOWN", "message": f"Cảnh báo: {event}"})

# --- 5. KHỞI CHẠY ---
def start_mqtt_background():
    global mqtt_client
    print("Đang khởi động Backend MQTT...")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"[MQTT] Lỗi kết nối Broker: {e}")
        return

    print("Đang làm nóng động cơ AI (Warm-up)... Xin chờ vài giây...")
    try:
        dummy_image = np.zeros((224, 224, 3), dtype=np.uint8)
        DeepFace.extract_faces(img_path=dummy_image, detector_backend="mtcnn", enforce_detection=False)
        print("Động cơ AI đã sẵn sàng hoạt động!")
    except Exception:
        pass 

    mqtt_client.loop_forever()