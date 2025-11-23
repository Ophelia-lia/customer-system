import os
import json
import secrets
import time
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
# 引入 OAuth2 相关库，替换原来的 HTTPBasic
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
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

# === 2. 用户权限配置中心 ===
# role: "admin" (可编辑), "reader" (仅阅读)
# 生产环境建议将密码放入环境变量
USERS = {
    "admin": {
        "password": "lia", 
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

# === 3. 安全认证核心逻辑 (已重构) ===
# 指定 token 获取地址，Swagger UI 会用到
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    验证 Token 并返回当前用户。
    注意：这是 MVP 版本的简易验证，Token 格式为 "bearer-token-{username}-{timestamp}"
    生产环境请务必替换为 JWT (JSON Web Tokens)。
    """
    user_found = None
    
    # 简单的 Token 解析逻辑：检查 Token 中是否包含用户名
    for username, data in USERS.items():
        if username in token:
            user_found = {"username": username, "role": data["role"]}
            break
            
    if not user_found:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_found

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

# [新增] 登录接口：处理前端 login.html 发来的表单数据
@app.post("/api/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = USERS.get(form_data.username)
    if not user:
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    
    # 验证密码 (生产环境应使用哈希比对)
    if not secrets.compare_digest(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    
    # 生成一个伪 Token (包含用户名和时间戳)
    # 前端拿到这个 Token 后，后续请求会在 Header带上：Authorization: Bearer <token>
    access_token = f"bearer-token-{form_data.username}-{int(time.time())}"
    
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "username": form_data.username,
        "role": user["role"]
    }

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
    # === 1. 权限检查 ===
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足：访客账号无法修改数据")

    try:
        # === 2. 获取前端传来的数据 ===
        customer_list = data.get("customers", [])
        settings = data.get("settings", {})
        next_id = data.get("nextCustomerId", 1)

        # === 3. 核心修复：智能同步逻辑 (Smart Sync) ===
        
        # A. 提取前端发来的所有客户 ID
        incoming_ids = {c_data["id"] for c_data in customer_list}

        # B. 找出数据库里现有的所有 ID
        # (这里只查 ID 列，速度极快)
        db_customers = session.exec(select(Customer)).all()
        existing_ids = {c.id for c in db_customers}

        # C. 计算需要删除的 ID (数据库里有，但前端没传过来的，说明用户在前端删了)
        ids_to_delete = existing_ids - incoming_ids
        
        # D. 执行删除 (精准切除，不再误伤)
        for customer in db_customers:
            if customer.id in ids_to_delete:
                session.delete(customer)

        # E. 执行 更新 或 新增 (Upsert)
        for c_data in customer_list:
            p_info = c_data.get("personalInfo", {})
            
            # 构造对象
            customer_obj = Customer(
                id=c_data["id"],
                name=p_info.get("name", "未知"),
                customer_service=p_info.get("customerService", ""),
                full_data=json.dumps(c_data), # 依然存全量 JSON
                last_updated=c_data.get("lastUpdated", "")
            )
            
            # merge 是神器：
            # 如果 ID 存在 -> 更新它
            # 如果 ID 不存在 -> 插入它
            session.merge(customer_obj)

        # === 4. 保存设置 ===
        session.merge(AppSettings(key="appSettings", value=json.dumps(settings)))
        session.merge(AppSettings(key="nextCustomerId", value=str(next_id)))

        # === 5. 提交事务 ===
        session.commit()
        return {"status": "success"}

    except Exception as e:
        session.rollback() # 出错回滚，保证数据安全
        print(f"Error saving data: {e}") # 打印错误日志方便调试
        raise HTTPException(status_code=500, detail=str(e))
# === 新增：单兵作战接口 ===

# 1. 保存单个客户 (新增 或 更新)
@app.post("/api/customer/save")
def save_single_customer(customer_data: Dict[str, Any], session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    # 权限检查
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足")

    try:
        customer_id = customer_data.get("id")
        p_info = customer_data.get("personalInfo", {})
        
        # 构造数据库对象
        customer_obj = Customer(
            id=customer_id,
            name=p_info.get("name", "未知"),
            customer_service=p_info.get("customerService", ""),
            full_data=json.dumps(customer_data), # 将单个客户的字典转为 JSON 字符串存入
            last_updated=customer_data.get("lastUpdated", "")
        )
        
        # 智能同步：有则改之，无则加之
        session.merge(customer_obj)
        session.commit()
        
        return {"status": "success", "id": customer_id}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# 2. 删除单个客户
@app.post("/api/customer/delete/{customer_id}")
def delete_single_customer(customer_id: str, session: Session = Depends(get_session), current_user: dict = Depends(get_current_user)):
    # 权限检查
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="权限不足")

    try:
        # 查找并删除
        customer = session.get(Customer, customer_id)
        if customer:
            session.delete(customer)
            session.commit()
            return {"status": "success", "deleted_id": customer_id}
        else:
            return {"status": "ignored", "detail": "Customer not found"}
            
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
# 挂载静态文件 (必须放在最后，否则会拦截 API 请求)
app.mount("/", StaticFiles(directory="static", html=True), name="static")