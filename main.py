import os import json
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel

# === 1. 数据库模型 (The Silo) ===
# 为了完美兼容你前端的复杂结构，我们不拆分几十张表，
# 而是使用混合策略：关键字段独立，复杂结构存JSON字符串。
class Customer(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str = Field(index=True)  # 方便搜索
    customer_service: str = Field(default="", index=True) # 方便搜索顾问
    full_data: str  # 存储前端完整的JSON对象字符串
    last_updated: str

# === 数据库连接改装开始 ===
# 尝试从环境变量获取云数据库地址
db_url = os.environ.get("DATABASE_URL")

# 兼容性处理
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# 本地备用方案
sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# 决策逻辑
connect_args = {}
if not db_url:
    db_url = sqlite_url
    connect_args = {"check_same_thread": False}

# 启动引擎
engine = create_engine(db_url, connect_args=connect_args)
# === 数据库连接改装结束 ===

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

# === 2. 数据传输对象 (DTO) ===
class CustomerInput(BaseModel):
    id: str
    personalInfo: Dict[str, Any]
    # 允许接收前端传来的任何额外字段，保证"一字不漏"
    allergyHistory: List[Any] = []
    medicationHistory: List[Any] = []
    pastMedicalHistory: List[Any] = []
    surgeryHospitalizationHistory: List[Any] = []
    vaccinationHistory: List[Any] = []
    familyHistory: List[Any] = []
    periods: List[Any] = []
    lastUpdated: str

class AppSettings(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str

# === 3. API 核心逻辑 (The Brain) ===
app = FastAPI()

# 允许跨域，防止前端报错
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# --- 客户管理接口 ---

@app.get("/api/customers")
def read_customers(session: Session = Depends(get_session)):
    customers = session.exec(select(Customer)).all()
    # 将存储的JSON字符串还原为对象返回给前端
    return [json.loads(c.full_data) for c in customers]

@app.post("/api/save_data")
def save_all_data(data: Dict[str, Any], session: Session = Depends(get_session)):
    """
    对应你前端的 CustomerModel.saveData
    这里接收整个数据包，进行批量更新
    """
    try:
        # 1. 处理客户数据
        customer_list = data.get("customers", [])
        
        # 简单粗暴策略：直接覆盖（为了匹配你前端逻辑）
        # 生产环境建议改为增量更新，但这里为了兼容你的 MVP
        session.exec(SQLModel.metadata.tables["customer"].delete())
        
        for c_data in customer_list:
            # 提取关键信息用于索引
            p_info = c_data.get("personalInfo", {})
            new_customer = Customer(
                id=c_data["id"],
                name=p_info.get("name", "未知"),
                customer_service=p_info.get("customerService", ""),
                full_data=json.dumps(c_data), # 完整保存
                last_updated=c_data.get("lastUpdated", "")
            )
            session.add(new_customer)

        # 2. 处理设置信息
        settings = data.get("settings", {})
        next_id = data.get("nextCustomerId", 1)
        
        # 保存设置
        session.merge(AppSettings(key="appSettings", value=json.dumps(settings)))
        session.merge(AppSettings(key="nextCustomerId", value=str(next_id)))

        session.commit()
        return {"status": "success"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/load_data")
def load_data(session: Session = Depends(get_session)):
    """
    对应你前端的 CustomerModel.loadData
    """
    customers = session.exec(select(Customer)).all()
    settings_record = session.get(AppSettings, "appSettings")
    next_id_record = session.get(AppSettings, "nextCustomerId")

    return {
        "customers": [json.loads(c.full_data) for c in customers],
        "settings": json.loads(settings_record.value) if settings_record else {"pageSize": 10},
        "nextCustomerId": int(next_id_record.value) if next_id_record else 1
    }

# 挂载静态文件，让你的HTML能直接被访问
app.mount("/", StaticFiles(directory="static", html=True), name="static")