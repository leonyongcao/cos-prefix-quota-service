from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from app.config import get_settings


def build_cos_resource(bucket: str, region: str, appid: str, key_pattern: str) -> str:
    """构造 COS 授权资源 QCS。

    示例：qcs::cos:ap-guangzhou:uid/1250000000:bucket-1250000000/team-a/*
    """
    return f"qcs::cos:{region}:uid/{appid}:{bucket}/{key_pattern}"


class StsProvider:
    """STS 临时密钥提供器。

    说明：
    1. 生产环境不建议每个文件都实时调用 STS，因为会增加一次远程 API 时延。
    2. 这里做了进程内缓存：同一个 bucket + prefix 在 STS 未过期前复用临时密钥。
    3. 如果要做最严格的单文件硬限制，建议关闭 ISSUE_STS_ON_APPLY，只返回 presigned_put_url。
    """

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
        self._lock = Lock()

    def get_upload_credential(self, bucket: str, prefix: str, object_key: str | None = None) -> dict:
        settings = get_settings()
        cache_key = (bucket, prefix)
        now = datetime.now(timezone.utc)

        # 提前 60 秒刷新，避免客户端拿到即将过期的 token。
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now + timedelta(seconds=60):
                return cached[1]

        try:
            from sts.sts import Sts
        except Exception as exc:
            raise RuntimeError("qcloud-python-sts is not installed or cannot be imported") from exc

        # 默认限制到团队 prefix，便于大文件分块上传和批量上传。
        # 如果要进一步限制到单对象，可改成 key_pattern = object_key。
        key_pattern = f"{prefix}*"
        resource = build_cos_resource(bucket, settings.cos_region, settings.cos_appid, key_pattern)

        # 只授予上传相关权限，不授予删除、读全桶、改 ACL 等高危权限。
        actions = [
            "name/cos:PutObject",
            "name/cos:PostObject",
            "name/cos:InitiateMultipartUpload",
            "name/cos:UploadPart",
            "name/cos:CompleteMultipartUpload",
            "name/cos:AbortMultipartUpload",
            "name/cos:ListMultipartUploads",
            "name/cos:ListParts",
        ]
        policy = {
            "version": "2.0",
            "statement": [
                {
                    "effect": "allow",
                    "action": actions,
                    "resource": [resource],
                }
            ],
        }
        config = {
            "duration_seconds": settings.sts_duration_seconds,
            "secret_id": settings.cos_secret_id,
            "secret_key": settings.cos_secret_key,
            "bucket": bucket,
            "region": settings.cos_region,
            "policy": policy,
        }
        credential = Sts(config).get_credential()

        expire_at = now + timedelta(seconds=settings.sts_duration_seconds)
        with self._lock:
            self._cache[cache_key] = (expire_at, credential)
        return credential
