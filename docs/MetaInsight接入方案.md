# MetaInsight 接入方案

> 用于解决本项目"第一步初始化时无法快速拿到 prefix 当前真实存量"的问题。

---

## 1. 背景：当前痛点

当前 `cos-prefix-quota-service` 在初始化或首次接入一个已有 prefix 时，必须现场调 `ListObjects` 全量遍历整个 prefix 才能算出 `used_bytes`：

```python
# app/quota_service.py reconcile_prefix()
for _, size, _ in self.cos.iter_objects(bucket, prefix):
    actual_size += size
    count += 1
```

问题：

- 单 prefix 千万级对象时，`ListObjects` 需要分页几万次，**耗时分钟级到小时级**
- 每次 1000 个 key 的 `ListObjects` 请求都计费
- 第一步校验（`POST /quotas` 创建配额时）就卡住，体验差
- 想做"按 prefix 即时统计 / 多维度统计"基本不可能

---

## 2. MetaInsight 是什么

### 2.1 定位

> MetaInsight（智能检索）是腾讯云 **数据万象 CI** 提供的 **COS 元数据索引与检索服务**。
>
> 把 COS 桶内对象的元数据（key、size、storage_class、content-type、last_modified、自定义 tag、图像/文档智能标签等）实时同步到一套**元数据索引引擎**（业内可理解为 ES + 向量库的封装），对外提供**简单查询 + 聚合查询 + 多模态检索** 能力。

### 2.2 核心能力（与 quota 服务相关的部分）

| 能力 | 用途 |
|---|---|
| **绑定存储桶 → 自动建索引** | 关联即用，存量 + 增量都能索引 |
| **简单查询（SimpleQuery）** | 按 prefix / size / 时间范围筛选对象 |
| **聚合查询（Aggregations）** | **`SUM(size) GROUP BY prefix`** ← 我们最需要的能力 |
| **基础元信息算子（COS 基础元数据）** | 不依赖 AI，仅同步 size/key 等基础信息 |
| **OpenAPI** | REST API + 各语言 SDK |

### 2.3 计费（仅看本项目用到的部分）

| 计费项 | 价格 |
|---|---|
| COS 读请求（建索引时回源 COS） | ¥0.01 / 万次 |
| 元数据管理（标准） | ¥0.06 / 千个 / 月 |
| 标准检索 | ¥2 / 千次 |

按 1000 万对象规模估算：
- 元数据管理：1000 万 × 0.06 / 1000 = **600 元/月**
- 检索次数（每天 1 次对账）：30 × 0.002 = **0.06 元/月**

成本相对 ListObjects 频繁全量遍历**显著降低**（List 自身免费，但调用次数费用、计算时间、数据库被打慢的成本会明显更高）。

---

## 3. 引入 MetaInsight 后的架构变化

### 3.1 改造前

```
[创建 Quota / 初始化]
        │
        ▼
[ListObjects 全量遍历]  ──分页 N 次──▶ [按 prefix 求和]
        │ 慢，分钟到小时级
        ▼
[写入 used_bytes]
```

### 3.2 改造后

```
COS 桶 ─同步─▶ MetaInsight 元数据索引（持续运行）
                         │
[创建 Quota / 初始化]      │
        │                 │
        ▼                 │
[MetaInsight 聚合查询]  ◀─┘
   SUM(size) WHERE prefix='team-a/'
        │ 秒级
        ▼
[写入 used_bytes]
```

---

## 4. 具体接入方式

### 4.1 前置开通（一次性运维操作）

1. 在数据万象控制台开通 **MetaInsight 智能检索**
2. **创建数据集**：选择"基础元信息"算子模板（不需要图片/文档智能特征，省钱）
3. **绑定存储桶**：勾选你的 quota 服务管理的 bucket
4. **开启存量数据索引**：让 MetaInsight 把存量对象也建索引
5. 等存量索引完成（控制台可看进度），后续增量自动同步

### 4.2 服务侧改造点

#### A. 新增 `app/metainsight_client.py`

- 封装"按 prefix 查 SUM(size) + COUNT"
- 失败/未开通时**降级到 ListObjects**

#### B. `quota_service.reconcile_prefix()` 优先走 MetaInsight

```python
def reconcile_prefix(...):
    if settings.use_metainsight:
        try:
            actual_size, count = self.meta.sum_prefix(bucket, prefix)
        except Exception:
            actual_size, count = self._list_object_sum(bucket, prefix)
    else:
        actual_size, count = self._list_object_sum(bucket, prefix)
```

#### C. `create_or_update_quota()` 新增"自动初始化用量"

- 创建 quota 时，**自动调一次 MetaInsight** 拿到当前真实存量
- 写入 `used_bytes`
- 这样"第一步校验"就能拿到正确数据

#### D. 配置开关

```bash
# .env 新增
USE_METAINSIGHT=true
METAINSIGHT_DATASET_NAME=cos-quota-dataset
METAINSIGHT_REGION=ap-guangzhou
```

---

## 5. 优劣势对比

| 维度 | ListObjects 方案 | MetaInsight 方案 |
|---|---|---|
| 初始化耗时 | 分钟～小时 | **秒级** |
| 对 COS 影响 | 高频 List 请求 | 一次性建索引，后续轻量 |
| 多维统计 | ❌ 不支持 | ✅ 按 prefix/类型/时间聚合 |
| 实时性 | 实时（但慢） | 准实时（同步有少许延迟） |
| 成本 | List 调用累计 | 元数据管理月费 |
| 复杂度 | 简单 | 需要开通 + 数据集管理 |
| 兜底 | / | List 仍保留作为容灾 |

---

## 6. 注意事项

1. **MetaInsight 索引同步有延迟**（通常秒级～分钟级），所以仍需保留 reserved_bytes 预占机制做"上传前硬限制"
2. **MetaInsight 的"按前缀求和"** 在不同地域 SDK 写法可能略有差异，需以最新 OpenAPI 为准
3. 如果对账时**索引缺失/未同步**，必须 fallback 到 ListObjects 全量扫描
4. MetaInsight 有数据集级配额（对象数上限、QPS 限制），大客户需提工单申请

---

## 7. 改造后的对外能力

| 接口 | 改造前 | 改造后 |
|---|---|---|
| `POST /quotas` | 只创建空配额 | **创建时自动初始化 used_bytes** |
| `POST /quotas/reconcile` | ListObjects 慢遍历 | **MetaInsight 秒级聚合** |
| 新增 `GET /quotas/{prefix}/snapshot` | / | 实时返回 prefix 真实用量（可选） |
