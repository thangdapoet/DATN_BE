import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

# Load .env file
load_dotenv()

SQLSERVER_USER = os.getenv("SQLSERVER_USER")
SQLSERVER_PASSWORD = os.getenv("SQLSERVER_PASSWORD")
SQLSERVER_SERVER = os.getenv("SQLSERVER_SERVER", "localhost")
SQLSERVER_PORT = os.getenv("SQLSERVER_PORT", "1433")
SQLSERVER_DB = os.getenv("SQLSERVER_DB")

# Example using pyodbc + SQL Server
DATABASE_URL = (
    f"mssql+pyodbc://{SQLSERVER_USER}:{SQLSERVER_PASSWORD}"
    f"@{SQLSERVER_SERVER},{SQLSERVER_PORT}/{SQLSERVER_DB}"
    "?driver=ODBC+Driver+17+for+SQL+Server"
)

engine = create_engine(
    DATABASE_URL,
    echo=True,           # log SQL (optional, useful in dev)
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dependency for FastAPI routes
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
