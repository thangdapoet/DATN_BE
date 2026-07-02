from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import History
from datetime import datetime
from sqlalchemy import cast, Date
import os

router = APIRouter(
    prefix="/history",
    tags=["History"],
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))     # /app/routers
ROOT_DIR = os.path.dirname(BASE_DIR)                      # /app
UPLOAD_DIR = os.path.join(ROOT_DIR, "uploads")            # /app/uploads

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

PUBLIC_HOST = "http://127.0.0.1:8000"   # change later if needed



@router.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    # generate unique file name
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    ext = file.filename.split(".")[-1]
    filename = f"history_{timestamp}.{ext}"

    abs_path = os.path.join(UPLOAD_DIR, filename)

    # save file to uploads folder
    with open(abs_path, "wb") as f:
        f.write(await file.read())

    # full URL for frontend
    public_url = f"{PUBLIC_HOST}/uploads/{filename}"

    return {
        "success": True,
        "image_url": public_url,     
        "absolute_path": abs_path    
    }



@router.post("/")
def create_history(data: dict, db: Session = Depends(get_db)):
    history = History(
        ImageUrl=data["image_url"],
        CreatedDate=datetime.utcnow(),
        UID=data["uid"],
        Status=data["status"]
    )

    db.add(history)
    db.commit()
    db.refresh(history)

    return {
        "message": "History created successfully",
        "data": {
            "id": history.HistoryId,
            "image_url": history.ImageUrl,
            "created_date": history.CreatedDate,
            "status": history.Status,
            "uid": history.UID
        }
    }



@router.get("/grouped-by-date")
def get_history_grouped_by_date(db: Session = Depends(get_db)):

    rows = (
        db.query(
            cast(History.CreatedDate, Date).label("date"),
            History
        )
        .order_by(History.CreatedDate.desc())
        .all()
    )

    grouped = {}

    for date_value, record in rows:
        date_str = date_value.isoformat()

        if date_str not in grouped:
            grouped[date_str] = []

        grouped[date_str].append({
            "HistoryId": record.HistoryId,
            "ImageUrl": record.ImageUrl,
            "CreatedDate": record.CreatedDate,
            "Status": record.Status,
            "UID": record.UID
        })

    return grouped
