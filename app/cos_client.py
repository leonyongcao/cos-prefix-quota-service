from urllib.parse import unquote_plus

from qcloud_cos import CosConfig, CosS3Client

from app.config import get_settings


class CosClient:
    """COS SDK 轻量封装。

    这里集中封装本服务需要用到的 COS XML API 能力：
    - HEAD Object：确认对象真实大小。
    - DELETE Object：删除对象并释放 quota。
    - Presigned URL：生成单对象上传 URL。
    - List Objects：按 prefix 做周期性对账。
    """

    def __init__(self):
        settings = get_settings()
        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
            Scheme="https",
        )
        self.client = CosS3Client(config)

    def head_size(self, bucket: str, key: str) -> tuple[int, str | None]:
        """调用 HEAD Object 获取对象真实大小和 ETag。

        上传完成确认时不能信任客户端传入的 file_size，必须以 COS 返回为准。
        """
        response = self.client.head_object(Bucket=bucket, Key=key)
        size = int(response.get("Content-Length") or response.get("content-length") or 0)
        etag = response.get("ETag") or response.get("etag")
        if isinstance(etag, str):
            etag = etag.strip('"')
        return size, etag

    def object_exists_size(self, bucket: str, key: str) -> tuple[bool, int, str | None]:
        """查询对象是否存在；不存在或无权限时返回 False。"""
        try:
            size, etag = self.head_size(bucket, key)
            return True, size, etag
        except Exception:
            return False, 0, None

    def delete_object(self, bucket: str, key: str) -> None:
        """删除 COS 对象。生产中建议只允许通过业务后端删除，避免 used_bytes 不一致。"""
        self.client.delete_object(Bucket=bucket, Key=key)

    def presigned_put_url(self, bucket: str, key: str, expires: int) -> str:
        """生成单对象 PUT 预签名 URL。

        这是最接近“单文件硬限制”的上传方式：
        - 后端先预占 quota。
        - 只给这个 object_key 的上传 URL。
        - 客户端无法用该 URL 写 prefix 下的其他对象。
        """
        return self.client.get_presigned_url(
            Method="PUT",
            Bucket=bucket,
            Key=key,
            Expired=expires,
        )

    def iter_objects(self, bucket: str, prefix: str):
        """按 prefix 遍历 COS 对象，用于小规模对账。

        大规模生产场景建议使用 COS Inventory 清单，而不是频繁 ListObjects。

        实现说明：
        - 这里**不传 EncodingType**，因此返回的 ``Key`` 已是原始字符串，
          ``NextMarker`` 也保持原样，可直接作为下一次请求的 ``Marker``。
        - 对历史包含 ``+`` / ``%`` / 控制字符等特殊符号的 key，
          ``unquote_plus`` 仅用作防御性兜底；如果未来切换到 ``EncodingType=url``，
          需要同步对 ``marker`` 调用 ``unquote`` 还原，否则翻页会错位。
        """
        marker = ""
        while True:
            response = self.client.list_objects(
                Bucket=bucket,
                Prefix=prefix,
                Marker=marker,
                MaxKeys=1000,
            )
            contents = response.get("Contents") or []
            if isinstance(contents, dict):
                contents = [contents]
            last_raw_key = ""
            for item in contents:
                raw_key = item.get("Key", "")
                last_raw_key = raw_key
                key = unquote_plus(raw_key)
                size = int(item.get("Size", 0))
                etag = item.get("ETag")
                if isinstance(etag, str):
                    etag = etag.strip('"')
                yield key, size, etag
            if response.get("IsTruncated") in ("true", True):
                # 注意：使用 raw_key（未解码）而不是 unquote_plus 之后的 key 作为 marker，
                # 否则在未来切换 EncodingType 时翻页会错位。
                marker = response.get("NextMarker") or last_raw_key
                if not marker:
                    break
            else:
                break
