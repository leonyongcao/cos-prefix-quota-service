"""定时任务入口。

支持两个子命令：
- ``expire``    清理过期 PENDING 上传会话，归还 reserved_bytes。建议每分钟调用。
- ``reconcile`` 对所有 ACTIVE quota 跑一次对账。建议每天调用一次。

本地运行：
    python cron.py expire
    python cron.py reconcile

腾讯云 SCF 部署：
    将 ``expire_handler`` / ``reconcile_handler`` 配置为 SCF 定时器入口即可。
"""

import sys

from app.database import Base, SessionLocal, engine
from app.quota_service import QuotaService

Base.metadata.create_all(bind=engine)
service = QuotaService()


def expire_handler(event=None, context=None) -> dict:
    """SCF 定时入口：清理过期 PENDING 会话。"""
    db = SessionLocal()
    try:
        return service.expire_pending_sessions(db)
    finally:
        db.close()


def reconcile_handler(event=None, context=None) -> dict:
    """SCF 定时入口：对所有 ACTIVE quota 跑一次 dry_run=False 的对账。"""
    db = SessionLocal()
    summary = []
    try:
        for row in service.list_quotas(db):
            if row.status != "ACTIVE":
                continue
            try:
                result = service.reconcile_prefix(db, row.bucket, row.prefix, dry_run=False)
                summary.append(result)
            except Exception as exc:  # 单个 prefix 失败不阻塞其他
                summary.append({
                    "bucket": row.bucket,
                    "prefix": row.prefix,
                    "error": str(exc),
                })
        return {"reconciled": len(summary), "items": summary}
    finally:
        db.close()


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("expire", "reconcile"):
        print("Usage: python cron.py [expire|reconcile]")
        return 2
    if argv[1] == "expire":
        print(expire_handler())
    else:
        print(reconcile_handler())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
