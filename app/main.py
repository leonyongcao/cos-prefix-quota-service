from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.quota_service import NotFoundError, QuotaError, QuotaService, quota_to_out
from app.schemas import (
    ApplyUploadRequest,
    ApplyUploadResponse,
    CompleteUploadResponse,
    DeleteObjectRequest,
    ExpireSessionsResponse,
    QuotaCreate,
    QuotaOut,
    ReconcileRequest,
    ReconcileResponse,
    normalize_prefix,
)

# Demo 版本直接启动时自动建表。生产环境建议改成 Alembic 管理表结构。
Base.metadata.create_all(bind=engine)

app = FastAPI(title="COS Prefix Quota Service", version="1.0.0")
service = QuotaService()


def handle_error(exc: Exception):
    """将业务异常映射为 HTTP 状态码。"""
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, QuotaError):
        raise HTTPException(status_code=409, detail=str(exc))
    raise exc


@app.get("/health")
def health():
    """健康检查接口。"""
    return {"status": "ok"}


@app.post("/quotas", response_model=QuotaOut)
def create_quota(req: QuotaCreate, db: Session = Depends(get_db)):
    """创建或更新某个 COS prefix 的 quota。"""
    row = service.create_or_update_quota(
        db,
        bucket=req.bucket,
        prefix=req.prefix,
        owner_id=req.owner_id,
        owner_type=req.owner_type,
        quota_bytes=req.quota_bytes,
    )
    return quota_to_out(row)


@app.get("/quotas", response_model=list[QuotaOut])
def list_quotas(db: Session = Depends(get_db)):
    """查看所有 prefix quota 当前用量。"""
    return [quota_to_out(row) for row in service.list_quotas(db)]


@app.get("/quotas/{bucket}/{prefix:path}", response_model=QuotaOut)
def get_quota(bucket: str, prefix: str, db: Session = Depends(get_db)):
    """按 bucket + prefix 单独查询某个团队/项目的配额用量。

    ``prefix`` 使用 ``:path`` 转换器以兼容形如 ``team-a/project-x/`` 的多层路径。
    """
    try:
        row = service.find_quota(db, bucket, normalize_prefix(prefix))
    except Exception as exc:
        handle_error(exc)
    return quota_to_out(row)


@app.post("/uploads/apply", response_model=ApplyUploadResponse)
def apply_upload(req: ApplyUploadRequest, db: Session = Depends(get_db)):
    """上传前申请授权。

    这是实现“硬限制”的核心入口：先预占 quota，成功后才返回上传凭证。
    """
    try:
        return service.apply_upload(db, req.bucket, req.prefix, req.object_key, req.file_size)
    except Exception as exc:
        handle_error(exc)


@app.post("/uploads/{upload_id}/complete", response_model=CompleteUploadResponse)
def complete_upload(upload_id: str, db: Session = Depends(get_db)):
    """上传完成确认。

    由业务前端上传成功后调用，服务端会 HEAD Object 校验真实大小。
    """
    try:
        return service.complete_upload(db, upload_id)
    except Exception as exc:
        handle_error(exc)


@app.post("/objects/delete")
def delete_object(req: DeleteObjectRequest, db: Session = Depends(get_db)):
    """通过业务后端删除对象，避免用户直接删除导致 used_bytes 不准。"""
    try:
        return service.delete_object(db, req.bucket, req.object_key)
    except Exception as exc:
        handle_error(exc)


@app.post("/reconcile", response_model=ReconcileResponse)
def reconcile(req: ReconcileRequest, db: Session = Depends(get_db)):
    """对账接口。

    小规模直接 ListObjects；大规模建议接入 COS Inventory 后离线聚合。
    """
    try:
        return service.reconcile_prefix(db, req.bucket, req.prefix, req.dry_run)
    except Exception as exc:
        handle_error(exc)


@app.post("/events/cos")
def cos_event(event: dict, db: Session = Depends(get_db)):
    """接收 COS 事件通知或 SCF 转发事件，用于异步校准 used_bytes。

    注意：COS 事件是异步事后通知，不能阻止对象写入。
    因此它只能做校准/治理，不能代替 /uploads/apply 的上传前 quota 判断。
    """
    return service.handle_cos_event(db, event)


@app.post("/uploads/expire", response_model=ExpireSessionsResponse)
def expire_pending_uploads(db: Session = Depends(get_db)):
    """清理已过期但仍 PENDING 的上传会话，归还 reserved_bytes。

    建议由 cron / SCF 定时器调用，避免客户端拿到 presigned 后未上传导致配额虚占。
    """
    return service.expire_pending_sessions(db)
