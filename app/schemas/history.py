from pydantic import BaseModel
from datetime import datetime

class HistoryCreate(BaseModel):
    image_url: str
    created_date: datetime
    status: str
    uid: str

class HistoryResponse(BaseModel):
    HistoryId: int
    ImageUrl: str | None
    CreatedDate: datetime
    UID: str
    Status: str

    class Config:
        orm_mode = True