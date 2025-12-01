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
else:
    # PostgreSQL 连接池配置
    connect_args = {
        "connect_timeout": 10,
        "options": "-c statement_timeout=30000"
    }

# 关键修复：添加连接池配置
engine = create_engine(
    db_url, 
    connect_args=connect_args,
    pool_pre_ping=True,  # 使用前检查连接是否有效
    pool_recycle=3600,   # 每小时回收连接
    pool_size=10,        # 连接池大小
    max_overflow=20,     # 最大溢出连接数
    echo=False           # 生产环境关闭SQL日志
)

# === 2. 用户权限配置中心 (这里设置账号) ===
# role: "admin" (可编辑), "reader" (仅阅读)
USERS = {
    "admin": {
        "password": "admin_xiatianlia",
        "role": "admin"
    },
    "guest": {
        "password": "guest123",
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
    is_password_correct = secrets.compare_digest(credentials.password, user_info["password"])
    
    if not is_password_correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    
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

@app.on_event("shutdown")
def on_shutdown():
    engine.dispose()  # 优雅关闭连接池

# === 接口 ===

@app.get("/api/me")
def read_users_me(current_user: dict = Depends(get_current_user)):
    return current_user

@app.get("/api/load_data")
def load_data(session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    try:
        customers = session.exec(select(Customer)).all()
        settings_record = session.get(AppSettings, "appSettings")
        next_id_record = session.get(AppSettings, "nextCustomerId")

        return {
            "customers": [json.loads(c.full_data) for c in customers],
            "settings": json.loads(settings_record.value) if settings_record else {"pageSize": 10},
            "nextCustomerId": int(next_id_record.value) if next_id_record else 1
        }
    except Exception as e:
        print(f"加载数据时出错: {str(e)}")
        raise HTTPException(status_code=500, detail=f"加载数据失败: {str(e)}")

# 保留原有的全量保存接口（向后兼容）
@app.post("/api/save_data")
def save_all_data(data: Dict[str, Any], session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")

    try:
        customer_list = data.get("customers", [])
        
        # 删除现有数据
        existing_customers = session.exec(select(Customer)).all()
        for customer in existing_customers:
            session.delete(customer)
        
        session.flush()
        
        # 添加新的客户数据
        for c_data in customer_list:
            p_info = c_data.get("personalInfo", {})
            new_customer = Customer(
                id=c_data["id"],
                name=p_info.get("name", "未知"),
                customer_service=p_info.get("customerService", ""),
                full_data=json.dumps(c_data, ensure_ascii=False),
                last_updated=c_data.get("lastUpdated", "")
            )
            session.add(new_customer)
        
        session.flush()

        # 保存设置
        settings = data.get("settings", {})
        next_id = data.get("nextCustomerId", 1)
        
        session.merge(AppSettings(key="appSettings", value=json.dumps(settings, ensure_ascii=False)))
        session.merge(AppSettings(key="nextCustomerId", value=str(next_id)))

        session.commit()
        
        return {"status": "success", "message": f"成功保存 {len(customer_list)} 条数据"}
        
    except Exception as e:
        session.rollback()
        print(f"保存数据时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

# === 新增：增量保存接口（性能优化） ===

@app.patch("/api/customer/{customer_id}")
def update_customer(
    customer_id: str, 
    customer_data: CustomerInput,
    session: Session = Depends(get_session), 
    current_user: dict = Depends(get_current_user)
):
    """单个客户的增量更新 - 性能优化接口"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")
    
    try:
        # 查找现有客户
        existing = session.get(Customer, customer_id)
        
        p_info = customer_data.personalInfo
        
        if existing:
            # 更新现有客户
            existing.name = p_info.get("name", "未知")
            existing.customer_service = p_info.get("customerService", "")
            existing.full_data = json.dumps(customer_data.dict(), ensure_ascii=False)
            existing.last_updated = customer_data.lastUpdated
        else:
            # 新建客户
            new_customer = Customer(
                id=customer_id,
                name=p_info.get("name", "未知"),
                customer_service=p_info.get("customerService", ""),
                full_data=json.dumps(customer_data.dict(), ensure_ascii=False),
                last_updated=customer_data.lastUpdated
            )
            session.add(new_customer)
        
        session.commit()
        return {"status": "success", "message": f"客户 {customer_id} 更新成功"}
        
    except Exception as e:
        session.rollback()
        print(f"更新客户时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")

@app.delete("/api/customer/{customer_id}")
def delete_customer(
    customer_id: str,
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """删除单个客户"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")
    
    try:
        customer = session.get(Customer, customer_id)
        if customer:
            session.delete(customer)
            session.commit()
            return {"status": "success", "message": f"客户 {customer_id} 删除成功"}
        else:
            raise HTTPException(status_code=404, detail="客户不存在")
            
    except Exception as e:
        session.rollback()
        print(f"删除客户时出错: {str(e)}")
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")

@app.patch("/api/settings")
def update_settings(
    settings_data: Dict[str, Any],
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """更新应用设置"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")
    
    try:
        settings = settings_data.get("settings", {})
        next_id = settings_data.get("nextCustomerId")
        
        if settings:
            session.merge(AppSettings(key="appSettings", value=json.dumps(settings, ensure_ascii=False)))
        
        if next_id is not None:
            session.merge(AppSettings(key="nextCustomerId", value=str(next_id)))
        
        session.commit()
        return {"status": "success", "message": "设置更新成功"}
        
    except Exception as e:
        session.rollback()
        print(f"更新设置时出错: {str(e)}")
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")

app.mount("/", StaticFiles(directory="static", html=True), name="static")
