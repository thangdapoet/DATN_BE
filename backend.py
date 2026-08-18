import paho.mqtt.client as mqtt
import cv2
import time
import os
import numpy as np
import shutil  
import logging
import glob

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
    cache_file = os.path.join(KNOWN_FACES_DIR, "representations_arcface.pkl")
    if os.path.exists(cache_file):
        os.remove(cache_file)

def check_anomaly(uid):
    current_time = time.time()
    access_history.setdefault(uid, []).append(current_time)
    access_history[uid] = [t for t in access_history[uid] if current_time - t <= 200]
    
    return len(access_history[uid]) > 5

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

def verify_face_ai(captured_img_path, uid):
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    known_face_path = os.path.join(KNOWN_FACES_DIR, f"{uid}.jpg")
    file_name = os.path.basename(full_captured_path)
    
    try:
        if not os.path.exists(known_face_path):
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            
            create_history_record(uid, "NO_REGISTRATION_FACE", relative_final_path)
            if send_event_callback:
                send_event_callback({"status": "bad", "id": uid, "message": "Thẻ hợp lệ nhưng chưa đăng ký khuôn mặt"})
            return
            
        result = DeepFace.verify(
            img1_path=full_captured_path, img2_path=known_face_path, 
            model_name="ArcFace", detector_backend="mtcnn",
            distance_metric="cosine", enforce_detection=True, anti_spoofing=True 
        )
        
        is_real = result.get("is_real", True)
        
        if is_real and result.get("distance", 1.0) <= 0.5:
            if os.path.exists(full_captured_path):
                os.remove(full_captured_path)
            if send_event_callback:
                send_event_callback({"status": "ok", "id": uid, "message": f"Xác thực khuôn mặt trùng khớp ({uid})"})
        else:
            final_img_path = os.path.join(WARNING_DIR, file_name)
            relative_final_path = f"security_warnings/{file_name}"
            shutil.move(full_captured_path, final_img_path)
            
            msg = f"Cảnh báo: Khuôn mặt không khớp ({uid})" if is_real else f"Phát hiện hình ảnh giả mạo! ({uid})"
            create_history_record(uid, "FAKE_OR_STRANGER", relative_final_path)
            if send_event_callback:
                send_event_callback({"status": "bad", "id": uid , "message": msg})
                
    except ValueError:
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): 
            shutil.move(full_captured_path, final_img_path)
            
        create_history_record(uid, "FACE_NOT_FOUND", relative_final_path)
        if send_event_callback:
            send_event_callback({"status": "bad", "id": uid, "message": "Không tìm thấy khuôn mặt"})
    except Exception:
        if send_event_callback:
            send_event_callback({"status": "bad", "id": uid, "message": "Lỗi hệ thống AI"})
        

def identify_face_ai(captured_img_path):
    full_captured_path = os.path.join(BASE_DIR, captured_img_path)
    file_name = os.path.basename(full_captured_path)
    
    try:
        dfs = DeepFace.find(
            img_path=full_captured_path, db_path=KNOWN_FACES_DIR, 
            model_name="ArcFace", detector_backend="mtcnn",
            distance_metric="cosine", enforce_detection=True,
            anti_spoofing=True, silent=True
        )
        
        if len(dfs) > 0 and not dfs[0].empty:
            best_match = dfs[0].iloc[0]
            if best_match['distance'] <= 0.6:
                uid_found = os.path.basename(best_match['identity']).replace(".jpg", "").split('_')[0]

                if best_match['distance'] < 0.40:
                    existing_imgs = glob.glob(os.path.join(KNOWN_FACES_DIR, f"{uid_found}_*.jpg"))
                    existing_imgs.sort(key=os.path.getmtime) 
                    while len(existing_imgs) >= 5:
                        os.remove(existing_imgs.pop(0))
                    new_timestamp = int(time.time())
                    new_face_path = os.path.join(KNOWN_FACES_DIR, f"{uid_found}_{new_timestamp}.jpg")
                    shutil.copy(full_captured_path, new_face_path)
                    clear_face_cache()
 
                if os.path.exists(full_captured_path):
                    os.remove(full_captured_path)
                    
                mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_SUCCESS")
                if send_event_callback:
                    send_event_callback({"status": "ok", "id": uid_found, "message": f"Mở cửa bằng khuôn mặt ({uid_found})"})
            else:
                raise ValueError("Distance above threshold")
        else:
             raise ValueError("No match found")
             
    except Exception:
        final_img_path = os.path.join(WARNING_DIR, file_name)
        relative_final_path = f"security_warnings/{file_name}"
        if os.path.exists(full_captured_path): 
            shutil.move(full_captured_path, final_img_path)
            
        create_history_record("UNKNOWN", "UNKNOWN_FACE", relative_final_path)
        mqtt_client.publish(MQTT_TOPIC_CMD, "FACE_DENIED")
        
        if send_event_callback:
            send_event_callback({"status": "bad", "id": "UNKNOWN", "message": "Mở cửa bằng khuôn mặt thất bại"})

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
        if img_path:
            identify_face_ai(img_path)

    elif event == "ADMIN_ADDED_CARD":
        img_path = capture_snapshot("REGISTRATION", data, is_registration=True)
        if not img_path:
            return
            
        full_path = os.path.join(BASE_DIR, img_path)
        
        try:
            faces = DeepFace.extract_faces(
                img_path=full_path, 
                detector_backend="mtcnn", 
                enforce_detection=True, 
                anti_spoofing=True     
            )
            
            is_real = any(face.get("is_real", True) for face in faces)
            
            if not is_real:
                raise ValueError("Spoofing detected")
                
            create_history_record(uid=data, status="ADMIN_REGISTERED", image_url=img_path)
            clear_face_cache() 
            if send_event_callback: 
                send_event_callback({"status": "ok", "id": data, "message": f"Đã đăng ký thẻ  ({data})"})

        except ValueError as e:
            err_msg = "Phát hiện ảnh giả mạo" if "Spoofing" in str(e) else "Không có khuôn mặt"
            
            file_name = os.path.basename(full_path)
            warning_path = os.path.join(WARNING_DIR, f"FAIL_REG_{file_name}")
            if os.path.exists(full_path):
                shutil.move(full_path, warning_path)
            
            rel_warning_path = f"security_warnings/FAIL_REG_{file_name}"
            create_history_record(uid=data, status="REGISTRATION_FAILED", image_url=rel_warning_path)
            
            mqtt_client.publish(MQTT_TOPIC_CMD, f"WEB_DELETE_CARD: {data}")
            
            if send_event_callback:
                send_event_callback({
                    "status": "bad", 
                    "id": data, 
                    "message": f"Đăng ký thất bại: {err_msg}. Đã hủy thẻ!"
                })
        except Exception as e:
            if os.path.exists(full_path):
                os.remove(full_path)
            mqtt_client.publish(MQTT_TOPIC_CMD, f"WEB_DELETE_CARD: {data}")
            if send_event_callback:
                send_event_callback({"status": "bad", "id": data, "message": "Lỗi AI. Đã hủy thẻ!"})

    elif event == "GRANTED" and data == "PASSWORD":
        if send_event_callback: 
            send_event_callback({"status": "ok", "id": "Passcode", "message": "Mở cửa bằng mật khẩu"})

    elif event == "GRANTED" and data not in ["PASSWORD", "FACE_ID_SUCCESS"]:
        is_spam = check_anomaly(data)
        
        if is_spam:
            img_path = capture_snapshot("SPAM_WARNING", data, target_dir=WARNING_DIR)
            create_history_record(uid=data, status="SPAM_WARNING", image_url=img_path)
            
            if send_event_callback:
                send_event_callback({
                    "status": "bad", 
                    "id": data, 
                    "message": f"SPAM: Thẻ ({data}) quẹt liên tục bất thường!"
                })
        else:
            img_path = capture_snapshot("TEMP", data, target_dir=TEMP_DIR)
            if img_path:
                verify_face_ai(img_path, data)
            
    elif event == "ADMIN_DELETED_CARD":
        target_img_path = os.path.join(KNOWN_FACES_DIR, f"{data}.jpg")
        if os.path.exists(target_img_path):
            os.remove(target_img_path)
            
        dynamic_imgs = glob.glob(os.path.join(KNOWN_FACES_DIR, f"{data}_*.jpg"))
        for p in dynamic_imgs:
            try: os.remove(p)
            except: pass
            
        clear_face_cache()
        if send_event_callback: 
            send_event_callback({"status": "ok", "id": data, "message": f"Đã xóa thẻ {data}"})

    elif event in ["CLONED_WARNING", "PASS_LOCKED", "RFID_LOCKED", "FACE_LOCKED"]:
        img_path = capture_snapshot(event, data, target_dir=WARNING_DIR)
        create_history_record(uid=data, status=event, image_url=img_path)        
        
        if send_event_callback:
            messages = {
                "PASS_LOCKED": "Báo động: Sai mật khẩu 5 lần",
                "RFID_LOCKED": "Báo động: Quẹt thẻ sai 5 lần",
                "FACE_LOCKED": "Báo động: Quét mặt sai 5 lần",
                "CLONED_WARNING": f"Phát hiện thẻ giả mạo!"
            }
            send_event_callback({
                "status": "bad", 
                "id": data,
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