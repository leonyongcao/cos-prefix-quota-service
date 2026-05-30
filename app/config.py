from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置。

    所有配置都通过环境变量或 .env 注入，避免把密钥写死在代码里。
    """

    cos_secret_id: str
    cos_secret_key: str
    cos_region: str = "ap-guangzhou"
    cos_appid: str
    cos_default_bucket: str | None = None

    # STS 临时密钥有效期。生产环境可以设置为 30 - 120 分钟，减少频繁申请 STS 的时延。
    sts_duration_seconds: int = 1800

    # 是否在 /uploads/apply 中返回 STS。严格单文件硬限制场景可关闭，只返回 presigned_put_url。
    issue_sts_on_apply: bool = True

    database_url: str = "sqlite:///./quota.db"
    auto_delete_over_quota_object: bool = False
    presigned_url_expires: int = 1800

    # MetaInsight 智能检索：用于秒级聚合 prefix 下真实用量，
    # 替代 ListObjects 全量遍历。未开通时自动降级到 ListObjects。
    use_metainsight: bool = False
    metainsight_dataset_name: str | None = None
    metainsight_region: str | None = None  # 不填则复用 cos_region

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
