from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    """统一使用 UTC 时间，避免多地域部署时时区混乱。"""
    return datetime.now(timezone.utc)


class UploadStatus(StrEnum):
    """上传会话状态。"""

    PENDING = "PENDING"      # 已预占 quota，但客户端尚未确认上传完成
    COMPLETED = "COMPLETED"  # 已通过 HEAD Object 确认真实对象大小
    FAILED = "FAILED"        # 上传失败，预留给业务扩展
    EXPIRED = "EXPIRED"      # 上传会话过期，需释放 reserved_bytes


class ObjectStatus(StrEnum):
    """对象记录状态。"""

    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class PrefixQuota(Base):
    """COS 前缀级配额表。

    COS 不支持目录级硬 quota，因此业务侧用 bucket + prefix 表示一个“逻辑目录”，
    并维护 quota_bytes / used_bytes / reserved_bytes。

    注意：SQLite 只有 INTEGER PRIMARY KEY 才能稳定自增，因此主键使用 Integer，
    其余容量字段使用 BigInteger。
    """

    __tablename__ = "cos_prefix_quota"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False, default="team")
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 配额上限、已确认使用量、已申请但尚未完成上传的预占量，单位均为 bytes。
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("bucket", "prefix", name="uk_bucket_prefix"),
        Index("idx_owner", "owner_type", "owner_id"),
    )


class CosObjectRecord(Base):
    """COS 对象记录表。

    用于处理覆盖上传、删除释放 quota、事件校准等场景。
    """

    __tablename__ = "cos_object_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    prefix: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ObjectStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("bucket", "object_key", name="uk_bucket_object"),
        Index("idx_prefix_status", "bucket", "prefix", "status"),
    )


class UploadSession(Base):
    """上传会话表。

    每次上传前先创建一条会话，并把预计新增容量写入 reserved_bytes。
    上传完成后，再用 COS HEAD Object 查询真实大小并结算到 used_bytes。
    """

    __tablename__ = "cos_upload_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    prefix: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=UploadStatus.PENDING)
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("idx_upload_object", "bucket", "object_key"),
        Index("idx_upload_status_expire", "status", "expire_at"),
    )
