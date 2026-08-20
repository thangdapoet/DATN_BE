import time
from onvif import ONVIFCamera

# Thông tin Camera Account (Tài khoản Local)
IP = "192.168.1.50"
PORT = 2020  # 👈 Tapo sử dụng cổng 2020 cho ONVIF
USER = "thangdapoet"
PASS = "15112004"

print("--- ĐANG THỬ NGHIỆM GIAO THỨC CHUẨN ONVIF ---")
try:
    print(f"Đang kết nối ONVIF tới {IP}:{PORT}...")
    
    # 1. Khởi tạo Camera
    cam = ONVIFCamera(IP, PORT, USER, PASS)
    print("✅ Đăng nhập thành công!")
    
    # 2. Lấy dịch vụ Media và PTZ (Pan/Tilt/Zoom)
    media = cam.create_media_service()
    ptz = cam.create_ptz_service()
    
    # 3. Lấy token của luồng hình ảnh hiện tại
    profiles = media.GetProfiles()
    token = profiles[0].token
    
    # 4. Cấu hình lệnh xoay ngang (Pan)
    request = ptz.create_type('ContinuousMove')
    request.ProfileToken = token
    # x = 1.0 (Xoay phải), x = -1.0 (Xoay trái), y = 1.0 (Lên), y = -1.0 (Xuống)
    request.Velocity = {
        'PanTilt': {'x': 0.5, 'y': 0.0}
    }
    
    print("Đang gửi lệnh xoay ngang sang phải...")
    ptz.ContinuousMove(request)
    
    # Cho camera xoay trong 1 giây
    time.sleep(1) 
    
    print("Đang gửi lệnh dừng...")
    ptz.Stop({'ProfileToken': token})
    
    print("🎉 THÀNH CÔNG RỰC RỠ! Camera đã xoay qua chuẩn ONVIF.")
    
except Exception as e:
    print(f"❌ THẤT BẠI: {e}")