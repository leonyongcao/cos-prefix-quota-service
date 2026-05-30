"""MetaInsight 智能检索客户端封装。

用于在创建 quota / 对账时秒级拿到 prefix 下真实用量。
不可用时由调用方自行 fallback 到 ListObjects 全量遍历。

注意：
1. MetaInsight 隶属于数据万象 CI，使用前需在控制台开通智能检索、创建数据集并绑定 bucket。
2. 真实业务里聚合 SUM(size) GROUP BY prefix 是 MetaInsight 最契合的能力。
3. 这里使用通用 HTTP 调用，避免强绑定某一版 cos-python-sdk 中 CI 子包，
   保留按官方 OpenAPI 调整的灵活性。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests
from qcloud_cos.cos_auth import CosS3Auth

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class PrefixSummary:
    """MetaInsight 按 prefix 聚合后的结果。"""

    actual_size: int
    object_count: int


class MetaInsightUnavailable(RuntimeError):
    """MetaInsight 不可用 / 未开通 / 接口超时等场景。

    上层应捕获该异常并 fallback 到 ListObjects。
    """


class MetaInsightClient:
    """MetaInsight 标量检索客户端。

    使用 SimpleQuery 的 Aggregations 能力计算 ``SUM(size)`` 与 ``COUNT(*)``，
    避免 ListObjects 全量分页遍历。
    """

    def __init__(self):
        self.settings = get_settings()
        self.dataset_name = self.settings.metainsight_dataset_name
        self.region = self.settings.metainsight_region or self.settings.cos_region
        # CI 域名：<APPID>.ci.<region>.myqcloud.com
        self.endpoint = f"https://{self.settings.cos_appid}.ci.{self.region}.myqcloud.com"
        self._auth = CosS3Auth(
            SecretId=self.settings.cos_secret_id,
            SecretKey=self.settings.cos_secret_key,
        )

    @property
    def enabled(self) -> bool:
        """配置开关 + 数据集名是否齐备。"""
        return bool(self.settings.use_metainsight and self.dataset_name)

    def sum_prefix(self, bucket: str, prefix: str, timeout: float = 10.0) -> PrefixSummary:
        """按 prefix 聚合查询 SUM(size) + COUNT(*)。

        参数：
            bucket: COS Bucket 名（含 -appid 后缀）。
            prefix: 已归一化的对象前缀，如 ``team-a/``。
            timeout: 单次请求超时（秒）。

        返回：
            PrefixSummary(actual_size=int, object_count=int)

        异常：
            MetaInsightUnavailable：未开通 / 数据集不存在 / 网络或鉴权失败 / 解析失败。
        """
        if not self.enabled:
            raise MetaInsightUnavailable("metainsight disabled by config")

        url = f"{self.endpoint}/dataset/simplequery"
        body = {
            "DatasetName": self.dataset_name,
            "Filter": {
                "Field": "Key",
                "Operation": "PREFIX",
                "Value": prefix,
            },
            "Aggregations": [
                {"Field": "Size", "Operation": "SUM"},
                {"Field": "Key", "Operation": "COUNT"},
            ],
            "MaxResults": 1,
        }
        # bucket 通过自定义头传入，便于服务端按桶维度做隔离与配额。
        headers = {
            "Content-Type": "application/json",
            "x-cos-bucket": bucket,
        }
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), auth=self._auth, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("MetaInsight request failed: %s", exc)
            raise MetaInsightUnavailable(f"network error: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("MetaInsight returned %s: %s", resp.status_code, resp.text[:512])
            raise MetaInsightUnavailable(f"http {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise MetaInsightUnavailable(f"invalid json: {exc}") from exc

        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict) -> PrefixSummary:
        """解析 MetaInsight Aggregations 响应。

        不同版本的接口字段命名可能略有差异，这里做一次容错。
        """
        aggs = payload.get("Aggregations") or payload.get("aggregations") or []
        actual_size = 0
        object_count = 0
        for item in aggs:
            field = (item.get("Field") or item.get("field") or "").lower()
            op = (item.get("Operation") or item.get("operation") or "").lower()
            value = item.get("Value") if "Value" in item else item.get("value")
            try:
                num = int(value) if value is not None else 0
            except (TypeError, ValueError):
                num = 0
            if field == "size" and op == "sum":
                actual_size = num
            elif field == "key" and op == "count":
                object_count = num
        return PrefixSummary(actual_size=actual_size, object_count=object_count)
