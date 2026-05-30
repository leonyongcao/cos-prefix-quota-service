from pydantic import BaseModel, Field, field_validator


def normalize_prefix(prefix: str) -> str:
    value = prefix.strip().lstrip("/")
    if value and not value.endswith("/"):
        value += "/"
    return value


def normalize_key(key: str) -> str:
    return key.strip().lstrip("/")


class QuotaCreate(BaseModel):
    bucket: str
    prefix: str
    owner_id: str
    owner_type: str = "team"
    quota_bytes: int = Field(gt=0)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        return normalize_prefix(value)


class QuotaUpdate(BaseModel):
    quota_bytes: int = Field(gt=0)


class QuotaOut(BaseModel):
    bucket: str
    prefix: str
    owner_id: str
    owner_type: str
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int
    available_bytes: int
    status: str


class ApplyUploadRequest(BaseModel):
    bucket: str
    prefix: str
    object_key: str
    file_size: int = Field(ge=0)
    content_type: str | None = None

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        return normalize_prefix(value)

    @field_validator("object_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return normalize_key(value)


class ApplyUploadResponse(BaseModel):
    upload_id: str
    bucket: str
    object_key: str
    prefix: str
    reserved_delta: int
    credential: dict
    presigned_put_url: str | None = None
    expires_in: int


class CompleteUploadResponse(BaseModel):
    upload_id: str
    bucket: str
    object_key: str
    actual_size: int
    old_size: int
    delta: int
    status: str


class DeleteObjectRequest(BaseModel):
    bucket: str
    object_key: str

    @field_validator("object_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return normalize_key(value)


class ReconcileRequest(BaseModel):
    bucket: str
    prefix: str
    dry_run: bool = True

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        return normalize_prefix(value)


class ReconcileResponse(BaseModel):
    bucket: str
    prefix: str
    object_count: int
    actual_used_bytes: int
    recorded_used_bytes: int
    updated: bool
    source: str | None = None


class ExpireSessionsResponse(BaseModel):
    expired: int
    released_bytes: int
