from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

from sqlalchemy import Column, DateTime
from datetime import datetime, timezone, timedelta

VIETNAM_TZ = timezone(timedelta(hours=7))
def get_vietnam_time():
    return datetime.now(VIETNAM_TZ)

class History(Base):
    __tablename__ = "history"

    HistoryId = Column(Integer, primary_key=True, index=True)
    ImageUrl = Column(String)
    CreatedDate = Column(DateTime(timezone=True), default=get_vietnam_time)
    UID = Column(String)
    Status = Column(String)
