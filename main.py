import os
import json
import secrets
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel

# === 1. 数据库配置 ===
db_url = os.environ.get("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {}
if not db_url:
    db_url = sqlite_url
    connect_args = {"check_same_thread": False}

engine = create_engine(db_url, connect_args=connect_args)

# === 2. 用户权限配置中心 (这里设置账号) ===
# role: "admin" (可编辑), "reader" (仅阅读)
USERS = {
    "admin": {
        "password": "admin_xiatianlia", # 请修改你的管理员密码
        "role": "admin"
    },
    "guest": {
        "password": "guest123", # 请修改你的访客密码
        "role": "reader"
    }
}

# === 数据模型 ===
class Customer(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    customer_service: str = Field(default="", index=True)
    full_data: str
    last_updated: str

class AppSettings(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

class CustomerInput(BaseModel):
    id: str
    personalInfo: Dict[str, Any]
    allergyHistory: List[Any] = []
    medicationHistory: List[Any] = []
    pastMedicalHistory: List[Any] = []
    surgeryHospitalizationHistory: List[Any] = []
    vaccinationHistory: List[Any] = []
    familyHistory: List[Any] = []
    periods: List[Any] = []
    lastUpdated: str

# === 3. 安全认证核心逻辑 ===
security = HTTPBasic()

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    username = credentials.username
    if username not in USERS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    user_info = USERS[username]
    # 验证密码
    is_password_correct = secrets.compare_digest(credentials.password, user_info["password"])
    
    if not is_password_correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    # 返回用户信息，包含角色
    return {"username": username, "role": user_info["role"]}

# === APP 初始化 ===
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# === 接口 ===

# 新增：前端查询当前是谁在登录，有什么权限
@app.get("/api/me")
def read_users_me(current_user: dict = Depends(get_current_user)):
    return current_user

@app.get("/api/load_data")
def load_data(session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    # 任何人（只要登录了）都可以读取数据
    customers = session.exec(select(Customer)).all()
    settings_record = session.get(AppSettings, "appSettings")
    next_id_record = session.get(AppSettings, "nextCustomerId")

    return {
        "customers": [json.loads(c.full_data) for c in customers],
        "settings": json.loads(settings_record.value) if settings_record else {"pageSize": 10},
        "nextCustomerId": int(next_id_record.value) if next_id_record else 1
    }

@app.post("/api/save_data")
def save_all_data(data: Dict[str, Any], session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    # === 核心防线：如果是 reader 角色，直接拒绝保存 ===
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")

    try:
        customer_list = data.get("customers", [])
        session.exec(SQLModel.metadata.tables["customer"].delete())
        
        for c_data in customer_list:
            p_info = c_data.get("personalInfo", {})
            new_customer = Customer(
                id=c_data["id"],
                name=p_info.get("name", "未知"),
                customer_service=p_info.get("customerService", ""),
                full_data=json.dumps(c_data),
                last_updated=c_data.get("lastUpdated", "")
            )
            session.add(new_customer)

        settings = data.get("settings", {})
        next_id = data.get("nextCustomerId", 1)
        
        session.merge(AppSettings(key="appSettings", value=json.dumps(settings)))
        session.merge(AppSettings(key="nextCustomerId", value=str(next_id)))

        session.commit()
        return {"status": "success"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))

app.mount("/", StaticFiles(directory="static", html=True), name="static")