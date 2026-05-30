"""腾讯云 SCF 入口示例：COS 事件触发后调用该 handler。

部署方式：
1. 将本项目依赖打包到云函数。
2. 配置环境变量 DATABASE_URL、COS_SECRET_ID、COS_SECRET_KEY、COS_REGION、COS_APPID。
3. 在 COS Bucket 上配置 ObjectCreated / ObjectRemove 触发器。

设计说明：
- 这里**不再**反向调用 FastAPI 路由 ``cos_event``，因为路由参数依赖 FastAPI 的
  ``Depends(get_db)``，裸调用会拿到 Depends 对象而非 Session。
- 路由层和本 handler 都直接调用 ``QuotaService.handle_cos_event``，保证逻辑单一来源。
"""

from app.database import Base, SessionLocal, engine
from app.quota_service import QuotaService

Base.metadata.create_all(bind=engine)
service = QuotaService()


def main_handler(event, context):
    db = SessionLocal()
    try:
        return service.handle_cos_event(db, event)
    finally:
        db.close()
