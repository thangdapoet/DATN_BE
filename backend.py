import paho.mqtt.client as mqtt
import cv2
import time
import os
import numpy as np
import shutil  
import logging

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
from deepface import DeepFace

from app.database import SessionLocal
from app.models import History

current_frame = None       
send_event_callback = None 

MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC_LOG = "quangthang/smartlock/log"
MQTT_TOPIC_CMD = "quangthang/smartlock/cmd"

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
    cache_file = os.path.join(KNOWN_FACES_DIR, "representations_facenet.pkl")
    if os.path.exists(cache_file):
        os.remove(cache_file)

def check_anomaly(uid):
    current_time = time.time()
    access_history.setdefault(uid, []).append(current_time)
    access_history[uid] = [t for t in access_history[uid] if current_time - t <= 300]
    
    return len(access_history[uid]) > 3

def create_history_record(uid, status, image_url=None):
    db = SessionLocal()
    try:
        new_record = History(UID=uid, Status=status, ImageUrl=image_url)
        db.add(new_record)
        db.commit()
        db.refresh(new_record)
        return new_record.HistoryId
    except Exception:
        db.rollback()
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
                record.UID = final_uid
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

def capture_snapshot(event_name, uid_info="", target_dir=TEMP_DIR, is_registration=False):
    if current_frame is None:
        return None

    frame_to_save = current_frame.copy() 
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    if is_registration and uid_info:
        file_name = f"{uid_info}.jpg"
        full_path = os.path.join(KNOWN_FACES_DIR, file_name)
        cv2.imwrite(full_path, frame_to_save)
        return f"{os.path.basename(KNOWN_FACES_DIR)}/{file_name}"

    file_name = f"{timestamp}_{event_name}_{uid_info}.jpg".replace(" ", "")
    full_path = os.path.join(target_dir, file_name)
    cv2.imwrite(full_path, frame_to_save)
    
    return f"{os.path.basename(target_dir)}/{file_name}"

def verify_face_ai(captured_img_path, uid, history_id):
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    known_face_path = os.path.join(KNOWN_FACES_DIR, f"{uid}.jpg")
    file_name = os.path.basename(full_captured_path)
    
    status = "DENIED"
    relative_final_path = captured_img_path
    ws_payload = None
    
    try:
        if not os.path.exists(known_face_path):
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "NO_REGISTRATION_FACE"
            ws_payload = {"status": "bad", "id": uid, "message": "Thẻ hợp lệ nhưng chưa đăng ký khuôn mặt"}
            return
            
        result = DeepFace.verify(
            img1_path=full_captured_path, img2_path=known_face_path, 
            model_name="Facenet", detector_backend="mtcnn",
            distance_metric="euclidean_l2", enforce_detection=True
        )
        
        if result.get("distance", 1.0) <= 0.75:
            final_img_path = os.path.join(ACCEPTED_DIR, file_name)
            relative_final_path = f"accepted_access/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "SUCCESS"
            ws_payload = {"status": "ok", "id": uid, "message": f"Xác thực khuôn mặt thành công ({uid})"} 
        else:
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            status = "FAKE_OR_STRANGER"
            ws_payload = {"status": "bad", "id": "UNKNOWN", "message": f"Cảnh báo: Khuôn mặt không khớp ({uid})"}
            
    except ValueError:
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): 
            shutil.move(full_captured_path, final_img_path)
        status = "FACE_NOT_FOUND"
        ws_payload = {"status": "bad", "id": uid, "message": "Không tìm thấy khuôn mặt"}
    except Exception:
        status = "SYSTEM_ERROR"
        ws_payload = {"status": "bad", "id": uid, "message": "Lỗi hệ thống AI"}
    finally:
        if history_id: 
            update_history_record(history_id, status, relative_final_path)
        if send_event_callback and ws_payload:
            send_event_callback(ws_payload)

def identify_face_ai(captured_img_path, history_id):
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    file_name = os.path.basename(full_captured_path)
    
    status = "DENIED"
    relative_final_path = captured_img_path
    uid_found = None
    ws_payload = None
    
    try:
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
            if best_match['distance'] <= 0.75:
                uid_found = os.path.basename(best_match['identity']).replace(".jpg", "")
                final_img_path = os.path.join(ACCEPTED_DIR, file_name)
                relative_final_path = f"accepted_access/{file_name}"
                shutil.move(full_captured_path, final_img_path)
                status = "SUCCESS"
                
                mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_SUCCESS")
                ws_payload = {"status": "ok", "id": uid_found, "message": f"Mở cửa bằng khuôn mặt ({uid_found})"}
            else:
                raise ValueError("Distance above threshold")
        else:
             raise ValueError("No match found")
             
    except Exception:
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): 
            shutil.move(full_captured_path, final_img_path)
        status = "UNKNOWN_FACE"
        
        mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_DENIED")
        ws_payload = {"status": "bad", "id": "UNKNOWN", "message": "Face ID thất bại"}
    finally:
        if history_id: 
            update_history_record(history_id, status, relative_final_path, final_uid=uid_found)
        if send_event_callback and ws_payload:
            send_event_callback(ws_payload)

def on_connect(client, userdata, flags, rc):
    client.subscribe(MQTT_TOPIC_LOG)

def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8")
    parts = payload.split(": ")
    event = parts[0]
    data = parts[1] if len(parts) > 1 else ""

    if event == "GRANTED_ADMIN":
        if send_event_callback: 
            send_event_callback({"status": "ok", "id": data, "message": "Truy cập bằng thẻ Admin"})

    elif event == "REQUEST_FACE_AUTH" and data == "HOLD":
        img_path = capture_snapshot("FACE_AUTH", "UNKNOWN", target_dir=TEMP_DIR)
        history_id = create_history_record(uid="FACE_REC", status="PENDING", image_url=img_path)
        if img_path and history_id:
            identify_face_ai(img_path, history_id)

    elif event == "ADMIN_ADDED_CARD":
        img_path = capture_snapshot("REGISTRATION", data, is_registration=True)
        create_history_record(uid=data, status="ADMIN_REGISTERED", image_url=img_path)
        clear_face_cache() 
        if send_event_callback: 
            send_event_callback({"status": "ok", "id": data, "message": f"Đã thêm thẻ {data}"})

    elif event == "GRANTED" and data == "PASSWORD":
        img_path = capture_snapshot("GRANTED_PASS", data, target_dir=ACCEPTED_DIR)
        create_history_record(uid="PASSWORD", status="SUCCESS", image_url=img_path)
        if send_event_callback: 
            send_event_callback({"status": "ok", "id": "Passcode", "message": "Mở cửa bằng Mật khẩu"})

    elif event == "GRANTED" and data not in ["PASSWORD", "FACE_ID_SUCCESS"]:
        check_anomaly(data)
        img_path = capture_snapshot("TEMP", data, target_dir=TEMP_DIR)
        history_id = create_history_record(uid=data, status="PENDING", image_url=img_path)
        if img_path and history_id:
            verify_face_ai(img_path, data, history_id)
            
    elif event == "ADMIN_DELETED_CARD":
        target_img_path = os.path.join(KNOWN_FACES_DIR, f"{data}.jpg")
        if os.path.exists(target_img_path):
            os.remove(target_img_path)
            clear_face_cache()
            if send_event_callback: 
                send_event_callback({"status": "ok", "id": data, "message": f"Đã xóa thẻ {data}"})

    elif event in ["CLONED_WARNING", "PASS_LOCKED", "RFID_LOCKED"]:
        img_path = capture_snapshot(event, data, target_dir=WARNING_DIR)
        create_history_record(uid=data, status=event, image_url=img_path)        
        
        if send_event_callback:
            messages = {
                "PASS_LOCKED": "Báo động: Sai mật khẩu 5 lần",
                "RFID_LOCKED": "Báo động: Quẹt thẻ sai 5 lần"
            }
            send_event_callback({
                "status": "bad", 
                "id": "UNKNOWN", 
                "message": messages.get(event, f"Cảnh báo: {event}")
            })

def start_mqtt_background():
    global mqtt_client
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        logging.error(f"MQTT Connection Error: {e}")
        return

    try:
        DeepFace.extract_faces(
            img_path=np.zeros((224, 224, 3), dtype=np.uint8), 
            detector_backend="mtcnn", 
            enforce_detection=False
        )
    except Exception:
        pass 

    mqtt_client.loop_forever()