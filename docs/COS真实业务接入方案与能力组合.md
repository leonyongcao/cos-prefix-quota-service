# 真实业务里 COS 的接入方式与能力组合

> 本文系统整理腾讯云 COS（对象存储）在真实业务场景下的开发接入方式，覆盖：上传/下载链路、事件触发机制、EventBridge 与 SCF 底层原理、以及与本项目 `cos-prefix-quota-service` 的结合落地。

---

## 0. 全局结论先讲

真实业务里"上传一张图片"**不是简单调一个上传接口就完事**，而是一个组合方案：

> **前端直传 + 服务端签名 + 事件回调 + 数据处理 + 受控下载**

腾讯云 XML API（`https://cloud.tencent.com/document/api/436/7751`）只是**数据面接口（最底层）**，真实工程里更多是组合使用：

| 阶段 | 真正用到的接口 / 能力 | 谁来调 |
|---|---|---|
| 鉴权 | STS `GetFederationToken` 申请临时密钥 | 业务后台 |
| 上传 | `PutObject` / `PostObject` / 分片三件套 | 客户端直传（推荐） |
| 上传完成通知 | **COS 事件通知 → SCF / CKafka / EventBridge** | COS 主动推送 |
| 数据加工 | 数据万象 CI（图片处理、内容审核、转码…） | URL 参数式 / 异步任务 |
| 下载/分发 | 预签名 URL / CDN 加速域名 | 业务后台签发 |

---

## 1. 真实的"上传一张图片"链路

### 1.1 反模式：客户端 → 业务后台 → COS（二段式）

把文件先传到自己服务器再转发到 COS。**问题**：
- 业务服务器变成流量瓶颈
- 要付双倍出网带宽费
- 大文件场景后端要分片合并，复杂度高

### 1.2 推荐模式：前端直传 + STS 临时密钥

```
① 客户端 ──申请临时凭证──▶ 业务后台
② 业务后台 ──调用STS──▶ 腾讯云 STS（GetFederationToken）
③ 业务后台 ──下发临时密钥+随机Key──▶ 客户端
④ 客户端 ──直接 PUT/POST──▶ COS  （走 COS 的 XML API）
⑤ COS ──事件通知──▶ SCF / CKafka / CMQ ──▶ 业务后台
⑥ 业务后台落库 / 触发后续业务（审核、缩略图、入feed流…）
```

**为什么这么设计**（来自官方"使用临时密钥访问 COS"文档）：
- 永久密钥不能下发到 App/Web/小程序，**临时密钥最长 36 小时、可限定 action/resource/prefix**
- 业务服务器只负责签名，不过文件流，省带宽、省 CPU
- 文件 Key 必须由**服务端生成**（带时间随机串），避免恶意覆盖

---

## 2. 上传完之后要不要"回调"？

⚠️ 常见误解：腾讯云 COS 本身**没有像阿里云 OSS 那样的「Callback 头部直接 HTTP 回调到业务URL」机制**。腾讯云用的是**事件通知 + 触发器**模型，更解耦也更可靠。

### 三种回调链路

**方式 A：COS → SCF（云函数）** —— 最主流
- 在桶上配 **COS 触发器**，监听 `cos:ObjectCreated:*` / `cos:ObjectRemoved:*`
- COS 检测到对象创建后，**Push 模型 + 异步调用** SCF
- SCF 函数里拿到 Bucket、Key、Size、ETag，回调你的业务接口或直接写库/写消息队列
- 适合：头像处理、图片入库、自动生成缩略图、写 ES 索引

**方式 B：COS → CKafka / CMQ / TDMQ**
- 桶事件通知投递到消息队列
- 业务服务消费消息，做幂等处理
- 适合：高并发场景、需要削峰、需要多个下游消费同一个事件

**方式 C：客户端"上传完成回调里"通知后端**
- 客户端 SDK `uploadFile` 成功回调后，主动 POST 业务后台
- 简单，但**不可靠**（客户端可能掉线、可能伪造），一般要和 A/B 配合做对账

> 实际工程里 **A + C** 是最常见组合：客户端先告诉后台让 UI 立刻响应，COS 事件再做"权威落库"和兜底。

---

## 3. XML API（436/7751）里要重点接哪些

那份是 COS 的**数据面 XML API 总览**。真实业务高频用到的就这几个：

### 上传相关

| 接口 | 用途 | 典型场景 |
|---|---|---|
| `PutObject` | 简单上传，单次 PUT 整个文件 | < 5GB 小文件、SDK 默认 |
| `PostObject` | 表单上传，浏览器/小程序友好（用 policy 而非 Authorization） | Web/uni-app 直传 |
| `InitiateMultipartUpload` | 开启分片上传 | > 5MB 大文件、需要断点续传 |
| `UploadPart` | 上传单个分片 | 配合上面 |
| `CompleteMultipartUpload` | 合并分片 | 配合上面 |
| `AbortMultipartUpload` | 终止分片任务 | 取消上传 / 清理碎片 |

> SDK（cos-js-sdk-v5、cos-android、cos-ios…）的 `uploadFile` 已经把"小文件走 PUT、大文件走分片"封装好了，不用自己判断。

### 下载/分享相关

- 不直接用 GetObject，多数情况通过 **预签名 URL**：业务后台用 SDK 签一个有时效（如 10 分钟）的 URL 给前端
- 私有桶 + 预签名 是默认安全模式
- 生产环境下载一般还要叠加 **CDN 加速域名**（不能用 COS 签名走 CDN，CDN 有自己的鉴权）

### 桶/管理面（基本是运维一次性配置）

- `PutBucketCORS`：配置跨域（Web 直传必配）
- `PutBucketLifecycle`：生命周期（多少天转低频/归档/删除）
- `PutBucketReplication`：跨地域复制
- `PutBucketNotification`：配置事件通知（也可在控制台/SCF 触发器里配）
- `PutBucketReferer`：防盗链

---

## 4. 配套生态能力

### 4.1 数据万象 CI（和 COS 深度集成）

图片场景几乎一定会用：
- **URL 直接处理**：`?imageMogr2/thumbnail/200x200`、`?imageView2/...`、`?watermark/...`，不用提前生成缩略图
- **盲水印**：版权追溯
- **图片审核（自动审核）**：开启后**新上传的图自动鉴黄/违法/广告**，命中可自动冻结，0 业务代码
- **历史数据审核**：调 API 批量扫库存

### 4.2 安全

- **STS 临时密钥** + **最小权限策略**（限定 `prefix=user/${uin}/`，避免越权）
- **桶策略 / ACL / Referer 防盗链**
- **回源鉴权 / 签名下载**

### 4.3 加速分发

- COS **全球加速域名**（同账号自动接入）
- **CDN/EdgeOne 加速**（公网下载场景）

---

## 5. 完整的"用户上传图片"工程示例

```
[Web/小程序]
    │ ① 选择图片，POST /api/upload-token  (带后缀名/MIME)
    ▼
[业务后台 Node/Go/Java]
    │ ② 校验登录态 + 生成对象 Key:  user/{uid}/{yyyymmdd}/{uuid}.jpg
    │ ③ 调 STS GetFederationToken，限定:
    │     resource = qcs::cos:ap-guangzhou:uid/xxx:bucket/user/{uid}/*
    │     action   = cos:PutObject / cos:PostObject
    │ ④ 返回 {tmpSecretId, tmpSecretKey, sessionToken, key, host, policy}
    ▼
[Web/小程序]
    │ ⑤ 用临时密钥直传 COS（PUT 或 POST）
    │ ⑥ 上传成功，立即 POST /api/notify-upload-done?key=xxx (软通知)
    ▼
[COS]
    │ ⑦ 触发 COS 事件 cos:ObjectCreated:Put → SCF
    ▼
[SCF 云函数]
    │ ⑧ 校验大小/格式 → 调用 CI 鉴黄 → 写 MySQL → 发 Kafka
    │ ⑨ (可选) 调用 CI 生成缩略图 / 加水印
    ▼
[展示侧]
    │ ⑩ 取 cdn域名 + ?imageMogr2/thumbnail/300x 直接展示缩略图
    │     私密图则走 业务后台签预签名 URL 返回
```

---

## 6. COS 事件触发机制（开发视角深度解析）

### 6.1 关键概念纠正

1. **云函数（SCF）不是 source，是 sink（消费者/事件处理者）**。Source 永远是 COS。
2. **CKafka 在 COS 事件链路里也不是 source**，它只是事件投递路径上的"管道（broker）"，自己再被业务消费者订阅消费。

整个机制本质上是一个**发布-订阅模型**：

```
[COS 存储桶]   ──发布事件(Event Source)──▶   [事件目标 Sink]
   ↑                                          ├─ SCF 函数（Push 模型，COS 主动调用）
   桶级配置                                    ├─ CKafka（Push 到 Topic，业务自己 Pull 消费）
   PutBucketNotification                      └─ 事件总线 EventBridge（再路由到下游）
```

所有事件目标都通过**桶上的"通知配置（Notification Configuration）"**绑定，这是 COS 维护的**事件源映射（Event Source Mapping）**。

### 6.2 事件源映射存在哪里

按官方文档"COS 触发器说明"：

> **Push 模型**：COS 会监控指定的 Bucket 动作（事件类型）并调用相关函数，将事件数据推送给 SCF 函数。**在推模型中使用 Bucket 通知来保存 COS 的事件源映射**。

也就是说：
- 触发器的**配置（事件类型 + 前后缀过滤 + 目标函数 ARN）保存在 COS 桶**上
- 不是保存在 SCF 那一侧（SCF 那边只是显示一个"已绑定"的视图）
- 对应的 API 是 **`PutBucketNotification`**（控制台、SCF 控制台、Terraform 都是封装了这个 API）

### 6.3 触发模型：Push（不是 Pull）

```
用户PUT文件 ──▶ COS 写入成功 ──▶ COS 内部事件总线
                                    │
                                    ▼ (异步Push，调用方不感知)
                            匹配 Notification 规则
                                    │
                          ┌─────────┼─────────┐
                          ▼         ▼         ▼
                       SCF调用   CKafka写入   EventBridge
```

关键特性：
- **异步调用**：COS 不等 SCF 返回，结果不会回流给上传方
- **至少一次（at-least-once）**：失败会重试，所以**消费者必须做幂等**
- **同地域强约束**：COS 桶和 SCF 函数必须在同一个 region
- **事件唯一性**：同一个 Bucket + 同一个事件 + 重叠的前后缀 = 只能绑定一个目标

### 6.4 事件类型

| 事件类型 | 含义 |
|---|---|
| `cos:ObjectCreated:*` | 任意创建（PUT/POST/COPY/CompleteMultipart） |
| `cos:ObjectCreated:Put` | 简单上传 |
| `cos:ObjectCreated:Post` | 表单上传（Web 直传） |
| `cos:ObjectCreated:Copy` | 复制对象 |
| `cos:ObjectCreated:CompleteMultipartUpload` | 分片上传完成 |
| `cos:ObjectRemoved:*` | 任意删除 |
| `cos:ObjectRemoved:Delete` | 主动删除 |
| `cos:ObjectRemoved:DeleteMarkerCreated` | 版本控制删除 |
| `cos:ObjectRestore:*` | 归档恢复（Post/Completed） |
| `cos:ReplicationXxx` | 跨区域复制相关 |

实际开发常用就 3 个：`ObjectCreated:*`、`ObjectRemoved:*`、`CompleteMultipartUpload`（大文件场景需要单独监听）。

### 6.5 过滤条件与限制

每条规则可以加：
- **Prefix**：如 `user/avatar/`
- **Suffix**：如 `.jpg`

⚠️ 限制（开发时必踩坑）：
- 同一个事件类型下，**Prefix/Suffix 不能重叠**
- 单 Bucket 最多 10 个触发器规则
- 单 SCF 函数最多绑 10 个 COS 触发器

---

## 7. 路径一：COS → SCF（最主流）

### 7.1 SCF 收到的事件结构

按官方文档，事件以 `Records` 数组形式 push 给函数（注意是数组）：

```json
{
  "Records": [{
    "cos": {
      "cosSchemaVersion": "1.0",
      "cosObject": {
        "url": "http://testpic-1253970026.cos.ap-chengdu.myqcloud.com/testfile",
        "meta": {
          "x-cos-request-id": "NWMxOWY4MGFfMjViMjU4NjRfMTUyMV...",
          "Content-Type": "image/jpeg",
          "x-cos-meta-mykey": "myvalue"
        },
        "key": "/1253970026/testpic/testfile",
        "size": 1029
      },
      "cosBucket": {
        "region": "cd",
        "name": "testpic",
        "appid": "1253970026"
      },
      "cosNotificationId": "unkown"
    },
    "event": {
      "eventName": "cos:ObjectCreated:*",
      "eventVersion": "1.0",
      "eventTime": 1545205770,
      "eventSource": "qcs::cos",
      "requestParameters": {
        "requestSourceIP": "192.168.15.101"
      }
    }
  }]
}
```

⚠️ 容易踩的坑：
1. `key` 字段是 `/{appid}/{bucket}/{真实Key}`，**前面带了 appid 和 bucket 前缀**。要用真实 Key 访问 COS，得 `key.split("/", 3)[-1]`
2. `cosObject.size` 单位是字节
3. `meta` 里只有上传时设置的自定义头（`x-cos-meta-*`）和 `Content-Type`，**没有 ETag**，要 ETag 得自己 HEAD 一下
4. `eventTime` 是 Unix 秒级时间戳，不是毫秒

### 7.2 Python SCF 处理代码示例

```python
# main_handler 是 SCF Python 运行时的固定入口
import json
import logging
from qcloud_cos import CosConfig, CosS3Client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def main_handler(event, context):
    for record in event["Records"]:
        cos_info = record["cos"]
        bucket_name = cos_info["cosBucket"]["name"]            # 不带 -appid
        appid = cos_info["cosBucket"]["appid"]
        region = cos_info["cosBucket"]["region"]               # 简写：cd/gz/sh
        raw_key = cos_info["cosObject"]["key"]                 # /1253970026/testpic/foo.jpg
        # 提取真实对象 Key
        object_key = raw_key.split("/", 3)[-1]                 # foo.jpg
        size = cos_info["cosObject"]["size"]
        event_name = record["event"]["eventName"]

        logger.info(f"[{event_name}] {bucket_name}/{object_key} size={size}")

        # —— 业务处理 ——
        # 1) 幂等检查
        if already_processed(object_key):
            return "skip"

        # 2) 调用 CI 做缩略图（URL 参数式直接拼）
        thumb_url = f"https://{bucket_name}-{appid}.cos.{region}.myqcloud.com/{object_key}?imageMogr2/thumbnail/300x300"

        # 3) 写业务库
        save_to_db(object_key, size, thumb_url)

        # 4) 可选：投递 Kafka 让其他下游消费
        # produce_kafka("file-uploaded-topic", {...})

    return "ok"
```

### 7.3 重试与失败处理

来自官方"错误类型与重试策略"文档：

| 错误类型 | 重试策略 |
|---|---|
| **用户代码错误**（函数 throw 异常或 return error） | 默认重试 **2 次**（可配 0-2），失败送死信队列 |
| **系统错误 / 并发超限 / 扩容超限** | 持续重试，间隔 1 分钟，最长 6 小时（可调） |
| **超过最长保留时间** | 进死信队列，否则被丢弃 |

**死信队列（DLQ）**是 CMQ 队列，配置在函数上。生产强烈建议配置：
- 否则 COS 事件丢了你都不知道
- 用 DLQ 配合监控告警，让运维人为兜底

幂等做法：
- DB 用 `(bucket, key, eventTime)` 当唯一键 upsert
- 或 Redis SETNX `processed:{requestId}` TTL 24h

---

## 8. 路径二：COS → CKafka（高吞吐 / 多消费者）

### 8.1 CKafka 在链路里的角色

**COS 把事件投递到 CKafka 的 Topic**，业务的消费者再以标准 Kafka 协议从 Topic 拉取：

```
COS ──▶ (Push 写消息) ──▶ CKafka Topic ──▶ ConsumerGroup A (业务服务1)
                                       ├─▶ ConsumerGroup B (Flink 实时计算)
                                       └─▶ ConsumerGroup C (数仓入湖)
```

为什么要选这条路（vs 直接 SCF）：

| 维度 | SCF | CKafka |
|---|---|---|
| 多消费者 | 一个事件触发一个函数 | 多 ConsumerGroup 各自独立消费 |
| 削峰 | 函数有并发上限，超了走重试队列 | Kafka 天然削峰，消费者按节奏拉 |
| 顺序 | 不保证（多实例并发） | 同 Partition 内有序 |
| 消费方语言 | 函数支持的运行时 | 任意语言（标准 Kafka 协议） |
| 持久化 | 重试 6h | 按 Topic 保留期，可设几天 |
| 适合场景 | 轻量、单一处理 | 高吞吐、多下游、需顺序 |

### 8.2 接入方式

**方式 A：COS 控制台 → 应用集成 → 数据导出至 CKafka**（半托管）
- 控制台一键配置，背后**自动创建一个 SCF 函数**做 COS→CKafka 桥接
- 优点：开箱即用；缺点：本质还是过 SCF，不是直连
- 适合 COS 访问日志/审计日志导到 Kafka 做分析

**方式 B：EventBridge 事件总线**（推荐，原生）
- COS 作为云服务事件源，自动投递到事件总线
- 在 EventBridge 里建**事件规则**，目标选 CKafka 实例 + Topic
- 事件遵循 **CloudEvents 1.0** 规范

⚠️ 注意 EventBridge 的事件结构和 SCF 直接收到的不一样：EB 用 CloudEvents 包了一层。消费侧解析逻辑不同。

### 8.3 消费者侧示例（Go）

```go
// 用标准 sarama 库消费就行，CKafka 完全兼容 Kafka 协议
config := sarama.NewConfig()
config.Net.SASL.Enable = true
config.Net.SASL.User = "ckafka-instance#username"
config.Net.SASL.Password = "xxx"
config.Consumer.Offsets.Initial = sarama.OffsetOldest

consumer, _ := sarama.NewConsumerGroup([]string{"ckafka-host:port"}, "my-group", config)
consumer.Consume(ctx, []string{"cos-events-topic"}, &handler{})

// handler.ConsumeClaim 里解析 message.Value，处理业务
```

---

## 9. 路径三：COS → EventBridge → 任意目标（最灵活）

EventBridge 是腾讯云的统一事件路由器，COS 事件**默认全部投递**到它的"云服务事件集"。配置**事件规则（Rule）**：

```
事件源: COS
   ↓
事件规则 Rule
  - 过滤: data.cosObject.key 以 "user/" 开头
  - 转换: JSONPath 提取关键字段
   ↓
事件目标 Target（多选）
  - SCF 函数
  - CKafka
  - HTTP/HTTPS Webhook（直接打到自己业务服务的 URL！）
  - 消息推送（短信/邮件/电话/企微）
  - CLS 日志
```

**这才是真正接近"HTTP 回调"的能力**：EventBridge 的 Webhook 目标可以直接 POST 到你业务后台的 URL，不用走 SCF。配合签名校验，完全可以替代"客户端通知后端"那条软通知链路。

---

## 10. 三种路径选择决策树

```
你只是要做一件简单事？比如生成缩略图、写一条 DB 记录
        │
        ├─ 是 ──▶ COS → SCF（最简单，5 分钟跑起来）
        │
        ├─ 否，多个下游/高吞吐/有顺序要求
        │       └─▶ COS → CKafka → 自建消费者（生产级）
        │
        └─ 否，想直接 HTTP 回调到业务后台 / 多目标分发
                └─▶ COS → EventBridge → Webhook/SCF/CKafka/...
```

---

## 11. EventBridge 是什么

### 11.1 一句话定义

> 腾讯云 EventBridge 是一款**安全、稳定、高效的无服务器事件管理平台**，作为流数据和事件的**自动收集、处理、分发管道**。
> —— 官方《事件总线概述》

### 11.2 它是什么"技术"

**EventBridge ≈ 云上托管的 Pub/Sub + 路由引擎 + 协议转换器**。

技术对应物：
- **AWS 上的对应产品**：Amazon EventBridge（同名同概念）
- **业内归类**：EDA（Event-Driven Architecture，事件驱动架构）的核心中间件
- **开源对应物**：Apache RocketMQ EventBridge / Knative Eventing / NATS / Kafka + KSQL
- **协议规范**：完全遵循 **CloudEvents 1.0**（CNCF 的开源事件标准）

它不是新发明，而是把企业里"自己用 Kafka + 各种 Connector + 业务代码胶水"才能做到的事，做成了一个云上**全托管的中心化平台**。

### 11.3 内部架构（三层模型）

```
┌─────────────────────────────────────────────────────────────────┐
│                    EventBridge 事件总线                           │
│                                                                  │
│   [事件源 Source]  ──▶  [事件集 EventBus]  ──▶  [事件目标 Target] │
│                            │                                     │
│                       事件规则(Rule)                              │
│                       - 模式匹配（filter）                         │
│                       - 数据转换（transform）                      │
└─────────────────────────────────────────────────────────────────┘
       ▲                          ▲                       │
       │                          │                       ▼
   COS/CVM/MySQL              路由+清洗               SCF / CKafka /
   自定义应用                                          CLS / Webhook /
   SaaS                                               短信/邮件/...
```

| 概念 | 类比 Kafka | 类比邮件系统 |
|---|---|---|
| **事件源 Source** | Producer | 发件人 |
| **事件集 EventBus** | Topic 集合 | 邮局 |
| **事件规则 Rule** | KSQL/分区路由 | 收件规则（关键词分类） |
| **事件目标 Target** | Consumer | 收件人 |

### 11.4 关键技术能力

**(1) 标准化协议（CloudEvents 1.0）**

```json
{
  "specversion": "1.0",
  "id": "事件唯一ID",
  "type": "cos:created:object",
  "source": "cos.cloud.tencent",
  "subject": "qcs::cos:ap-gz:...",
  "time": "1615430559146",
  "data": { /* 业务负载 */ }
}
```

不管事件来自 COS、CVM 还是自己的应用，下游处理代码**完全统一**。

**(2) 模式匹配（Pattern Matching）**

```json
{
  "source": "cos.cloud.tencent",
  "region": "ap-guangzhou",
  "data": {
    "cosObject": {
      "key": [{"prefix": "user/avatar/"}, {"suffix": ".jpg"}]
    }
  }
}
```

**(3) 数据转换（Transform）**：完整事件透传 / JSONPath 抽取部分字段，可在送给下游前**重新塑形**。

**(4) 一对多扇出（Fan-out）**：一条事件可同时路由到 SCF + CKafka + CLS + Webhook。

**(5) 配套基础设施**：事件查询 / 日志 / 审计 / 全链路追踪 / **事件重放**（关键能力，故障恢复时把历史事件重新跑一遍）。

### 11.5 EventBridge vs CKafka 区别

| 维度 | EventBridge | CKafka |
|---|---|---|
| 定位 | **事件路由器**，强调"分发逻辑" | **消息队列**，强调"持久化通道" |
| 协议 | CloudEvents 1.0 | Kafka Wire Protocol |
| 是否需要消费者代码 | 不需要，配置目标即可 | 需要写 Consumer |
| 顺序保证 | 不保证 | 同 Partition 有序 |
| 适用场景 | 跨系统事件分发、低代码集成 | 高吞吐流处理、数据管道 |

实际工程里两者经常配合：EventBridge 做前端路由 → CKafka 做缓冲 → 下游再消费。

---

## 12. SCF 是什么

### 12.1 一句话定义

> 腾讯云云函数（SCF）为函数即服务（**Function as a Service，FaaS**）产品，提供无服务器（Serverless）和 FaaS 的**计算平台**。
> —— 官方《云函数 - 相关概念》

### 12.2 底层到底有没有服务器

**有服务器，只是被云厂商接管了，对开发者完全透明。**

官方原话："无服务器并不是没有服务器就能够进行计算，而是对于开发者来说，**无需了解底层的服务器情况**，也能使用到相关资源。"

底层栈：

```
┌──────────────────────────────────────────────────┐
│  你的代码（main_handler）                         │  ← 你只关心这一层
├──────────────────────────────────────────────────┤
│  Runtime（Python3.x / Node.js / Go / Java...）    │  ← SCF 提供
├──────────────────────────────────────────────────┤
│  函数运行容器（轻量级容器，类似 Docker）           │  ← SCF 调度
├──────────────────────────────────────────────────┤
│  腾讯云调度系统（决定在哪台机器、何时拉起容器）     │  ← SCF 管理
├──────────────────────────────────────────────────┤
│  CVM 物理资源池（CPU / 内存 / 网络）              │  ← 本质还是云服务器
└──────────────────────────────────────────────────┘
```

底层就是**腾讯云内部维护的一个超大 CVM/容器资源池**，SCF 控制面在收到事件时：
1. 从池子里**调度一个空闲容器**（或冷启动新建一个）
2. 把代码 / 依赖加载进去
3. 把事件作为参数调你的 `handler`
4. 函数返回后容器**保留一段时间**等下次复用，长时间没请求就**回收**

SCF 的日志里有：
```
START RequestId: xxx
Init Report ... Coldstart: 236ms (PullCode: 70ms InitRuntime: 8ms ...)
END RequestId: xxx
```

`Coldstart` 就是**冷启动**：池子里没空闲实例时新拉一个的耗时。这是 FaaS 最经典的概念。

### 12.3 SCF 和传统 CVM 的本质区别

| 维度 | CVM（IaaS） | SCF（FaaS） |
|---|---|---|
| 你管理的 | 操作系统 + 运行环境 + 应用 | **只有代码** |
| 计费颗粒度 | 按秒/小时（包括空闲） | **按调用次数 + 实际运行毫秒数 + 内存** |
| 扩缩容 | 手动 / 配置 AS | **自动**（瞬间从 0 扩到上千实例） |
| 闲时成本 | 不调用也付钱 | **0 调用 = 0 费用** |
| 状态 | 有持久状态 | **无状态**（不能存内存数据） |
| 启动延迟 | 启动后常驻 | 冷启动几十~几百 ms |
| 单次最长执行 | 不限 | 最长 **15 分钟**（事件函数） |
| 适用场景 | 长驻服务、有状态、稳定负载 | 突发流量、事件驱动、定时任务 |

### 12.4 SCF 提供的两种部署形态

**(1) 代码包模式**（最常用）：上传 zip / 在线编辑器写代码，选 Runtime（Python/Node.js/Go/Java/PHP/Custom）。

**(2) 容器镜像模式**：
- **Job 镜像函数**：执行 `CMD`/`EntryPoint`，跑完释放，事件以**环境变量**注入
- **WebServer 镜像函数**：镜像里跑 HTTP Server 监听 **9000 端口**，事件以 **HTTP 请求**形式传入

镜像模式让你能跑任意复杂依赖（带 ffmpeg、带 PyTorch 的镜像）。

### 12.5 SCF 触发模型

SCF **不主动跑**，必须被"触发"：

| 触发器类型 | 谁来触发 | 调用方式 |
|---|---|---|
| **API 网关触发器** | HTTP 请求 | 同步调用（前端等返回） |
| **COS 触发器** | COS 文件事件 | 异步调用 |
| **CKafka 触发器** | Kafka 消息 | SCF Pull 消费 |
| **定时触发器** | Cron 表达式 | 异步调用 |
| **CMQ/TDMQ 触发器** | 消息到达 | 异步调用 |
| **EventBridge** | 任意事件 | 异步调用 |
| **手动调用 API** | InvokeFunction | 同步/异步 |

### 12.6 SCF 关键特性

**冷启动**
- 第一次调用 / 长时间没调用 / 并发突增 → 拉新容器 → 慢
- 一次冷启动一般 100ms ~ 几秒（取决于代码包大小、镜像大小、VPC 是否开启）
- 优化：**预置并发**（保持 N 个实例常驻）、**镜像最小化**、**同地域镜像仓库**

**无状态**
- 容器随时可能被回收
- 状态外置：Redis、DB、COS

**单实例并发**
- 默认一个容器同时只处理 1 个请求
- 可配置"单实例多并发"，省钱

**计费**（按量付费）
```
费用 = 调用次数 × 单价 + 运行时长(GB·s) × 单价
```

---

## 13. 接入清单

**一定要接的：**
1. STS SDK（`tencentcloud-sdk-*` 里的 `sts.GetFederationToken`）
2. COS XML API 中的 `PutObject` 或 `PostObject`（通过官方 SDK 调用即可）
3. 桶上配置 **CORS** + **事件通知**（控制台一次性配好）

**强烈建议接的：**
4. COS 事件触发器（SCF 或 CKafka）—— 取代"客户端通知"做权威落库
5. 数据万象 CI 图片处理（URL 参数式，零代码）
6. 预签名 URL 下载（私有桶场景）

**按需接入的：**
7. CI 内容审核（UGC 业务必备）
8. CDN/EdgeOne 加速（公网分发）
9. 生命周期规则（降本）
10. 跨地域复制（容灾）

---

## 14. 在本项目（cos-prefix-quota-service）的落地方案

本服务按 prefix 做配额管理，**事件触发对它特别有用**：
- 用户上传/删除文件会改变前缀下的用量
- 不能靠用户主动调上报接口（不可信、易丢）
- 用 COS 事件做权威驱动最合适

### 14.1 最小可用方案

```
COS 桶 配置 ObjectCreated/ObjectRemoved 触发器
   │
   ▼
SCF 函数 (Python, 同地域)
   │  解析 event → 提取 prefix → size delta
   ▼
调用 quota_service HTTP API（私网/带签名）
   │
   ▼
quota_service 更新 Redis/DB 中前缀配额
```

如果配额服务在 VPC 内，SCF 要配置 **VPC 网络** 才能访问内网。

### 14.2 高吞吐方案

如果上传 QPS 很高（>1000/s），**强烈建议** SCF 不直接调服务，而是把事件投到 CKafka，quota_service 起一个 consumer 慢慢处理，避免 SCF 高并发把服务打爆：

```
COS ──▶ EventBridge ──▶ CKafka Topic
                              │
                              ▼
                     quota_service Consumer (按 partition 并行)
                              │
                              ▼
                        Redis/DB 累加配额
```

### 14.3 幂等设计

COS 事件**至少一次**送达，必须幂等：
- 用 `(bucket, key, eventName, requestId)` 做去重键
- DB upsert 或 Redis SETNX TTL 24h

---

## 15. 不确定项 / 备注

- 真正的内部实现（SCF 调度算法、容器复用策略、EventBridge 存储引擎）腾讯云**没有公开**。业内一般推测：SCF 早期基于 KVM/Kata Containers，近年用轻量级容器（类似 Firecracker），EventBridge 底层应该是基于 Kafka/Pulsar 改造而来——这部分属于推测，官方文档没写。
- 价格、免费额度可能随时变化，具体看《计费概述》。
- 如果业务在**私有云 / TCS / TStack**，EventBridge 可能没有，只有 SCF + CKafka 两条路可走，建议先确认。
- COS 事件触发**不保证 Exactly-Once**，敏感操作（如配额累加）必须做幂等。

---

## 16. 参考文档

- COS XML API 总览：https://cloud.tencent.com/document/api/436/7751
- 使用临时密钥访问 COS：腾讯云官方文档
- 使用预签名 URL 访问 COS：腾讯云官方文档
- COS 触发器说明（SCF）：腾讯云官方文档
- COS 事件通知：腾讯云官方文档
- 数据万象 CI 产品概述：腾讯云官方文档
- 事件总线 EventBridge 概述：腾讯云官方文档
- 云函数 SCF 相关概念：腾讯云官方文档
- CloudEvents 1.0 规范：https://cloudevents.io/
