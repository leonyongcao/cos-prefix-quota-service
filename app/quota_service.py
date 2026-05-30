from datetime import timedelta
from urllib.parse import unquote_plus
from uuid import uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.cos_client import CosClient
from app.metainsight_client import MetaInsightClient, MetaInsightUnavailable
from app.models import CosObjectRecord, ObjectStatus, PrefixQuota, UploadSession, UploadStatus, utc_now
from app.schemas import normalize_prefix
from app.sts_provider import StsProvider


class QuotaError(Exception):
    """Quota 相关业务异常，例如超限、重复完成上传等。"""

    pass


class NotFoundError(Exception):
    """资源不存在异常。"""

    pass


def ensure_key_under_prefix(key: str, prefix: str) -> None:
    """确保对象 key 一定位于被授权的 prefix 下，防止越权写入其他团队目录。"""
    if not key.startswith(prefix):
        raise QuotaError(f"object_key must start with prefix: {prefix}")


def quota_to_out(row: PrefixQuota) -> dict:
    """将数据库模型转换为 API 响应结构。"""
    return {
        "bucket": row.bucket,
        "prefix": row.prefix,
        "owner_id": row.owner_id,
        "owner_type": row.owner_type,
        "quota_bytes": row.quota_bytes,
        "used_bytes": row.used_bytes,
        "reserved_bytes": row.reserved_bytes,
        "available_bytes": max(row.quota_bytes - row.used_bytes - row.reserved_bytes, 0),
        "status": row.status,
    }


class QuotaService:
    """COS 前缀级 Quota 核心服务。

    总体思路：
    1. COS 原生不支持 prefix quota，所以在业务数据库里维护 used/reserved。
    2. 上传前先原子预占 reserved_bytes，未超限才给客户端上传凭证。
    3. 上传完成后 HEAD Object 查询真实大小，再把 reserved 结算到 used。
    4. COS 事件和定期 reconcile 作为兜底校准。
    """

    def __init__(self):
        self.cos = CosClient()
        self.sts = StsProvider()
        self.meta = MetaInsightClient()
        self.settings = get_settings()

    def _measure_prefix_size(self, bucket: str, prefix: str) -> tuple[int, int, str]:
        """获取 prefix 当前真实用量。

        优先走 MetaInsight 聚合查询（秒级），失败时降级到 ListObjects 全量遍历，
        返回 ``(actual_size, object_count, source)``，source 取值：metainsight / list_objects。
        """
        if self.meta.enabled:
            try:
                summary = self.meta.sum_prefix(bucket, prefix)
                return summary.actual_size, summary.object_count, "metainsight"
            except MetaInsightUnavailable:
                # MetaInsight 不可用时落回 ListObjects，保证可用性
                pass
        actual_size = 0
        count = 0
        for _, size, _ in self.cos.iter_objects(bucket, prefix):
            actual_size += size
            count += 1
        return actual_size, count, "list_objects"

    def create_or_update_quota(self, db: Session, bucket: str, prefix: str, owner_id: str, owner_type: str, quota_bytes: int) -> PrefixQuota:
        """创建或更新某个 bucket + prefix 的 quota。

        新建 quota 时会主动调用 MetaInsight / ListObjects 查询当前真实用量并写入 used_bytes，
        解决"第一步校验拿不到 COS 当前存量"的问题。已有 quota 仅做配置覆盖，不重置用量。
        """
        prefix = normalize_prefix(prefix)
        row = db.execute(
            select(PrefixQuota).where(PrefixQuota.bucket == bucket, PrefixQuota.prefix == prefix)
        ).scalar_one_or_none()
        if row is None:
            initial_used, _, _ = self._measure_prefix_size(bucket, prefix)
            row = PrefixQuota(
                bucket=bucket,
                prefix=prefix,
                owner_id=owner_id,
                owner_type=owner_type,
                quota_bytes=quota_bytes,
                used_bytes=initial_used,
            )
            db.add(row)
        else:
            row.owner_id = owner_id
            row.owner_type = owner_type
            row.quota_bytes = quota_bytes
            row.updated_at = utc_now()
        db.commit()
        db.refresh(row)
        return row

    def list_quotas(self, db: Session) -> list[PrefixQuota]:
        """列出所有 prefix quota。"""
        return list(db.execute(select(PrefixQuota).order_by(PrefixQuota.bucket, PrefixQuota.prefix)).scalars())

    def find_quota(self, db: Session, bucket: str, prefix: str) -> PrefixQuota:
        """按 bucket + prefix 精确查找 quota。"""
        prefix = normalize_prefix(prefix)
        row = db.execute(
            select(PrefixQuota).where(
                PrefixQuota.bucket == bucket,
                PrefixQuota.prefix == prefix,
                PrefixQuota.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"quota not found for {bucket}/{prefix}")
        return row

    def find_quota_by_key(self, db: Session, bucket: str, key: str) -> PrefixQuota:
        """根据对象 key 找到最匹配的 quota 前缀。

        如果存在 team-a/ 和 team-a/project-x/，优先匹配更长的前缀。
        """
        rows = db.execute(
            select(PrefixQuota).where(PrefixQuota.bucket == bucket, PrefixQuota.status == "ACTIVE")
        ).scalars()
        matched = [row for row in rows if key.startswith(row.prefix)]
        if not matched:
            raise NotFoundError(f"quota prefix not found for object key: {key}")
        return sorted(matched, key=lambda row: len(row.prefix), reverse=True)[0]

    def get_recorded_size(self, db: Session, bucket: str, key: str) -> tuple[int, str | None]:
        """获取对象当前大小。

        优先使用本地对象记录，若没有记录则 HEAD COS，支持覆盖上传时计算增量。
        """
        row = db.execute(
            select(CosObjectRecord).where(
                CosObjectRecord.bucket == bucket,
                CosObjectRecord.object_key == key,
                CosObjectRecord.status == ObjectStatus.ACTIVE,
            )
        ).scalar_one_or_none()
        if row:
            return row.size_bytes, row.etag
        exists, size, etag = self.cos.object_exists_size(bucket, key)
        return (size, etag) if exists else (0, None)

    def reserve_quota(self, db: Session, bucket: str, prefix: str, delta: int) -> None:
        """原子预占 quota。

        这是上传前硬限制的关键：用单条 UPDATE 完成“检查 + 增加 reserved”。
        并发上传时，只有满足条件的请求能更新成功。
        """
        if delta <= 0:
            return
        stmt = (
            update(PrefixQuota)
            .where(
                PrefixQuota.bucket == bucket,
                PrefixQuota.prefix == prefix,
                PrefixQuota.status == "ACTIVE",
                PrefixQuota.used_bytes + PrefixQuota.reserved_bytes + delta <= PrefixQuota.quota_bytes,
            )
            .values(
                reserved_bytes=PrefixQuota.reserved_bytes + delta,
                updated_at=utc_now(),
            )
        )
        result = db.execute(stmt)
        if result.rowcount != 1:
            raise QuotaError("quota exceeded")

    def apply_upload(self, db: Session, bucket: str, prefix: str, key: str, file_size: int) -> dict:
        """申请上传授权。

        业务流程：
        1. 校验 key 必须在 prefix 下。
        2. 查询旧对象大小，支持覆盖上传只计算增量。
        3. 原子预占 reserved_bytes。
        4. 生成上传会话。
        5. 返回 presigned PUT URL；可选返回 prefix 级 STS 临时密钥。

        时延说明：
        - presigned URL 是本地签名，通常很快。
        - STS 是远程接口，已在 StsProvider 做了 prefix 级缓存。
        - 如果追求严格单文件硬限制，可设置 ISSUE_STS_ON_APPLY=false，仅返回单对象 presigned URL。
        """
        prefix = normalize_prefix(prefix)
        ensure_key_under_prefix(key, prefix)
        self.find_quota(db, bucket, prefix)

        old_size, _ = self.get_recorded_size(db, bucket, key)
        reserved_delta = max(file_size - old_size, 0)
        upload_id = uuid4().hex
        expire_at = utc_now() + timedelta(seconds=self.settings.sts_duration_seconds)

        try:
            self.reserve_quota(db, bucket, prefix, reserved_delta)
            session = UploadSession(
                id=upload_id,
                bucket=bucket,
                object_key=key,
                prefix=prefix,
                expected_size=file_size,
                old_size=old_size,
                reserved_delta=reserved_delta,
                expire_at=expire_at,
            )
            db.add(session)
            db.commit()
        except Exception:
            db.rollback()
            raise

        credential = {}
        if self.settings.issue_sts_on_apply:
            credential = self.sts.get_upload_credential(bucket=bucket, prefix=prefix, object_key=key)
        presigned = self.cos.presigned_put_url(bucket, key, self.settings.presigned_url_expires)
        return {
            "upload_id": upload_id,
            "bucket": bucket,
            "object_key": key,
            "prefix": prefix,
            "reserved_delta": reserved_delta,
            "credential": credential,
            "presigned_put_url": presigned,
            "expires_in": self.settings.sts_duration_seconds,
        }

    def complete_upload(self, db: Session, upload_id: str) -> dict:
        """上传完成确认。

        不信任客户端传入的大小，而是通过 COS HEAD Object 查询真实大小。
        若真实增量超过预占量，需要再次尝试补占 quota，失败时可选择删除对象。
        """
        session = db.get(UploadSession, upload_id)
        if session is None:
            raise NotFoundError("upload session not found")
        if session.status != UploadStatus.PENDING:
            raise QuotaError(f"upload session is not pending: {session.status}")

        actual_size, etag = self.cos.head_size(session.bucket, session.object_key)
        actual_delta = actual_size - session.old_size
        extra_delta = max(actual_delta - session.reserved_delta, 0)

        try:
            if extra_delta > 0:
                self.reserve_quota(db, session.bucket, session.prefix, extra_delta)

            db.execute(
                update(PrefixQuota)
                .where(PrefixQuota.bucket == session.bucket, PrefixQuota.prefix == session.prefix)
                .values(
                    used_bytes=PrefixQuota.used_bytes + actual_delta,
                    reserved_bytes=PrefixQuota.reserved_bytes - session.reserved_delta,
                    updated_at=utc_now(),
                )
            )

            row = db.execute(
                select(CosObjectRecord).where(
                    CosObjectRecord.bucket == session.bucket,
                    CosObjectRecord.object_key == session.object_key,
                )
            ).scalar_one_or_none()
            if row is None:
                row = CosObjectRecord(
                    bucket=session.bucket,
                    object_key=session.object_key,
                    prefix=session.prefix,
                    size_bytes=actual_size,
                    etag=etag,
                    status=ObjectStatus.ACTIVE,
                )
                db.add(row)
            else:
                row.prefix = session.prefix
                row.size_bytes = actual_size
                row.etag = etag
                row.status = ObjectStatus.ACTIVE
                row.updated_at = utc_now()

            session.status = UploadStatus.COMPLETED
            session.updated_at = utc_now()
            db.commit()
        except Exception:
            db.rollback()
            # 配额校验/写库失败时，必须显式归还 apply 阶段已经预占的 reserved_bytes，
            # 否则要等 expire_pending_sessions / reconcile 才会释放，期间团队配额会被虚占。
            try:
                db.execute(
                    update(PrefixQuota)
                    .where(
                        PrefixQuota.bucket == session.bucket,
                        PrefixQuota.prefix == session.prefix,
                    )
                    .values(
                        reserved_bytes=PrefixQuota.reserved_bytes - session.reserved_delta,
                        updated_at=utc_now(),
                    )
                )
                session.status = UploadStatus.FAILED
                session.updated_at = utc_now()
                db.commit()
            except Exception:
                db.rollback()
            if self.settings.auto_delete_over_quota_object:
                try:
                    self.cos.delete_object(session.bucket, session.object_key)
                except Exception:
                    pass
            raise

        return {
            "upload_id": upload_id,
            "bucket": session.bucket,
            "object_key": session.object_key,
            "actual_size": actual_size,
            "old_size": session.old_size,
            "delta": actual_delta,
            "status": UploadStatus.COMPLETED,
        }

    def delete_object(self, db: Session, bucket: str, key: str) -> dict:
        """通过业务后端删除对象，并释放对应 quota。"""
        quota = self.find_quota_by_key(db, bucket, key)
        row = db.execute(
            select(CosObjectRecord).where(CosObjectRecord.bucket == bucket, CosObjectRecord.object_key == key)
        ).scalar_one_or_none()
        size = row.size_bytes if row and row.status == ObjectStatus.ACTIVE else self.cos.object_exists_size(bucket, key)[1]

        self.cos.delete_object(bucket, key)
        db.execute(
            update(PrefixQuota)
            .where(PrefixQuota.bucket == bucket, PrefixQuota.prefix == quota.prefix)
            .values(
                used_bytes=PrefixQuota.used_bytes - size,
                updated_at=utc_now(),
            )
        )
        if row:
            row.status = ObjectStatus.DELETED
            row.updated_at = utc_now()
        db.commit()
        return {"bucket": bucket, "object_key": key, "released_bytes": size}

    def apply_created_event(self, db: Session, bucket: str, key: str, size: int, etag: str | None = None) -> None:
        """处理 COS ObjectCreated 事件，用于异步校准 used_bytes。

        该方法是事后治理，不能阻止上传。硬限制仍由 apply_upload 完成。
        """
        quota = self.find_quota_by_key(db, bucket, key)
        row = db.execute(
            select(CosObjectRecord).where(CosObjectRecord.bucket == bucket, CosObjectRecord.object_key == key)
        ).scalar_one_or_none()
        old_size = row.size_bytes if row and row.status == ObjectStatus.ACTIVE else 0
        delta = size - old_size
        db.execute(
            update(PrefixQuota)
            .where(PrefixQuota.bucket == bucket, PrefixQuota.prefix == quota.prefix)
            .values(used_bytes=PrefixQuota.used_bytes + delta, updated_at=utc_now())
        )
        if row is None:
            db.add(CosObjectRecord(bucket=bucket, object_key=key, prefix=quota.prefix, size_bytes=size, etag=etag, status=ObjectStatus.ACTIVE))
        else:
            row.prefix = quota.prefix
            row.size_bytes = size
            row.etag = etag
            row.status = ObjectStatus.ACTIVE
            row.updated_at = utc_now()
        db.commit()

    def apply_removed_event(self, db: Session, bucket: str, key: str) -> None:
        """处理 COS ObjectRemove 事件，用于异步释放 used_bytes。"""
        row = db.execute(
            select(CosObjectRecord).where(CosObjectRecord.bucket == bucket, CosObjectRecord.object_key == key)
        ).scalar_one_or_none()
        if row is None or row.status == ObjectStatus.DELETED:
            return
        db.execute(
            update(PrefixQuota)
            .where(PrefixQuota.bucket == bucket, PrefixQuota.prefix == row.prefix)
            .values(used_bytes=PrefixQuota.used_bytes - row.size_bytes, updated_at=utc_now())
        )
        row.status = ObjectStatus.DELETED
        row.updated_at = utc_now()
        db.commit()

    def reconcile_prefix(self, db: Session, bucket: str, prefix: str, dry_run: bool) -> dict:
        """按 prefix 对账 used_bytes。

        优先走 MetaInsight 聚合（秒级），不可用时降级到 ListObjects 全量遍历。
        大规模生产建议同时配合 COS Inventory 生成清单后离线复核，避免实时遍历压力。
        """
        quota = self.find_quota(db, bucket, prefix)
        actual_size, count, source = self._measure_prefix_size(bucket, prefix)
        recorded = quota.used_bytes
        if not dry_run:
            quota.used_bytes = actual_size
            quota.reserved_bytes = 0
            quota.updated_at = utc_now()
            db.commit()
        return {
            "bucket": bucket,
            "prefix": prefix,
            "object_count": count,
            "actual_used_bytes": actual_size,
            "recorded_used_bytes": recorded,
            "updated": not dry_run,
            "source": source,
        }

    def expire_pending_sessions(self, db: Session) -> dict:
        """清理已过期但仍处于 PENDING 状态的上传会话。

        客户端拿到 presigned URL 后没传完，这些会话的 reserved_bytes 会一直挂着，
        定时调用本方法将其置为 EXPIRED 并把预占容量归还到对应 prefix。
        建议通过 cron / SCF 定时器每分钟触发一次。
        """
        now = utc_now()
        rows = db.execute(
            select(UploadSession).where(
                and_(
                    UploadSession.status == UploadStatus.PENDING,
                    UploadSession.expire_at < now,
                )
            )
        ).scalars().all()

        expired = 0
        released = 0
        for session in rows:
            if session.reserved_delta > 0:
                db.execute(
                    update(PrefixQuota)
                    .where(
                        PrefixQuota.bucket == session.bucket,
                        PrefixQuota.prefix == session.prefix,
                    )
                    .values(
                        reserved_bytes=PrefixQuota.reserved_bytes - session.reserved_delta,
                        updated_at=now,
                    )
                )
                released += session.reserved_delta
            session.status = UploadStatus.EXPIRED
            session.updated_at = now
            expired += 1
        db.commit()
        return {"expired": expired, "released_bytes": released}

    def handle_cos_event(self, db: Session, event: dict) -> dict:
        """统一解析 COS 事件通知 / SCF 转发事件。

        FastAPI 路由 ``/events/cos`` 与腾讯云 SCF ``main_handler`` 都直接调用本方法，
        避免在 SCF 入口里反向调用 FastAPI 路由（会拿到 Depends 对象而非 Session）。
        """
        records = event.get("Records") or []
        handled = 0
        for record in records:
            event_info = record.get("event") or {}
            event_name = event_info.get("eventName") or record.get("eventName") or ""
            cos_info = record.get("cos") or {}
            bucket_info = cos_info.get("cosBucket") or {}
            object_info = cos_info.get("cosObject") or {}

            bucket = (
                bucket_info.get("name")
                or bucket_info.get("bucket")
                or bucket_info.get("bucketName")
            )
            key = object_info.get("key") or object_info.get("url")
            if not bucket or not key:
                continue
            key = unquote_plus(key.lstrip("/"))
            size = int(
                object_info.get("size")
                or object_info.get("meta", {}).get("Content-Length")
                or 0
            )
            etag = object_info.get("eTag") or object_info.get("etag")

            try:
                lower_name = event_name.lower()
                if "ObjectCreated" in event_name or "created" in lower_name:
                    self.apply_created_event(db, bucket, key, size, etag)
                    handled += 1
                elif (
                    "ObjectRemove" in event_name
                    or "remove" in lower_name
                    or "delete" in lower_name
                ):
                    self.apply_removed_event(db, bucket, key)
                    handled += 1
            except NotFoundError:
                # 没有配置 quota 的 prefix 不处理，避免把无关目录误统计进系统。
                continue
        return {"handled": handled}
