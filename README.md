# 仪器校准漂移补偿 API

基于 **FastAPI + SQLite** 的仪器校准服务：客户端在各时点提交参考值与同点多次读数，服务标记异常读数、计算校准偏差，并在相邻时点间线性插值偏差，对待校正读数施加漂移补偿。

## 快速开始（单条命令）

```bash
docker compose up --build
```

- **API 地址：http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `calibration-data`（容器内 `/data/calibration.db`），容器重启数据不丢。

## 校准规则

1. **观测登记**：每次提交参考值 `reference`、容差 `tolerance`、同一时点 `timestamp` 的至少 3 次读数。
2. **异常判定**：读数偏离该组**中位数**超过 `tolerance` 即标记为异常（原始读数仍完整保留）。
3. **校准偏差**：`bias = reference − 有效读数（剔除异常后）的中位数`。
4. **漂移补偿**：在相邻观测时点间对 `bias` 做**线性插值**，`校正读数 = 原始读数 + 插值偏差`。
5. **区间重算**：新增/修订观测只重算受影响的相邻区间（原位置缺口重新接通、新位置区间被时点拆分），其余历史区间与原始数据不变。
6. **覆盖范围**：查询时点超出 `[最早观测, 最晚观测]` 时拒绝（HTTP 400）；恰落在观测点上时直接使用该点偏差。
7. **修订与版本**：观测带单调递增的 `revision`（新建为 1）。`PUT` 修订须携带当前 `revision`，版本过期或目标时点已被占用均返回 409；成功后观测 ID 不变、版本 +1，并保存一份修订前快照（旧输入与旧偏差），可通过修订历史接口按版本倒序查询。
8. **累计漂移量**：`GET /drift-integral?from=&to=` 在覆盖范围内对当前分段线性 bias 做**精确解析积分**（梯形公式；段内穿越 0 时按解析交点拆分绝对值面积，不做固定步长采样），返回有号面积 `signed_bias_seconds`、绝对面积 `absolute_bias_seconds` 与逐段 `contributions`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations` | 登记校准观测，返回偏差与异常明细（201） |
| GET | `/observations` | 列出全部观测（含原始读数与异常标记） |
| PUT | `/observations/{id}` | 修订观测（乐观锁版本校验，409 冲突；ID 不变，版本 +1） |
| GET | `/observations/{id}/revisions` | 按版本倒序列出修订前快照（旧输入与旧偏差） |
| GET | `/intervals` | 列出当前插值区间 |
| POST | `/corrections` | 对指定时点的读数做漂移补偿 |
| GET | `/drift-integral?from=&to=` | 对分段线性 bias 在观测覆盖范围内精确积分 |
| GET | `/health` | 健康检查 |

时间戳支持 Unix 秒（数字）或 ISO 8601 字符串（如 `2026-09-18T08:00:00Z`），缺省为服务器当前时间。

### 登记观测

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8, 150.0]}'
```

响应（`150.0` 偏离组中位数 `99.1` 超差，判为异常；`bias = 100.0 − 99.0 = 1.0`）：

```json
{
  "id": 1, "revision": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 1.0,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
  ],
  "outliers": [150.0]
}
```

### 修订观测

修订请求体与登记一致（`reference`、`tolerance`、`readings`、`timestamp`），额外携带客户端持有的当前 `revision`。服务在单个事务内重新判定异常、重算偏差，并重建**原位置与新位置**受影响的相邻插值区间；观测 ID 保持不变。版本不匹配（已被他人修订）或目标时间戳已被其它观测占用时返回 409，数据不变。

```bash
curl -X PUT http://localhost:8000/observations/1 \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.1, 99.0, 99.2], "revision": 1}'
```

响应与登记观测相同，`revision` 推进为 `2`：

```json
{
  "id": 1, "revision": 2, "timestamp": 1789718400.0,
  "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 0.9,
  "readings": [
    {"value": 99.1, "is_outlier": false}, {"value": 99.0, "is_outlier": false},
    {"value": 99.2, "is_outlier": false}
  ],
  "outliers": []
}
```

版本冲突（或时间戳冲突）响应：

```json
{"detail": "observation 1 is at revision 2, not 1; fetch the latest observation and retry"}
```

### 查询修订历史

每次成功修订都会先保存一份修订前快照。按版本倒序返回变更时间、旧输入（旧时点、旧参考值、旧容差、旧读数）与旧偏差：

```bash
curl http://localhost:8000/observations/1/revisions
```

```json
[
  {
    "revision": 1,
    "changed_at": 1789720200.0, "changed_at_iso": "2026-09-18T08:30:00+00:00",
    "old_timestamp": 1789718400.0, "old_timestamp_iso": "2026-09-18T08:00:00+00:00",
    "old_reference": 100.0, "old_tolerance": 0.5, "old_bias": 1.0,
    "old_readings": [99.0, 99.2, 98.8, 150.0]
  }
]
```

从未修订过的观测返回空数组 `[]`；观测 ID 不存在时两个接口均返回 404。

### 漂移补偿

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

响应包含校正值、所用区间及两端点观测的异常明细：

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
     "bias": 1.0, "outliers": [150.0]},
    {"observation_id": 2, "timestamp": 1789725600.0, "timestamp_iso": "2026-09-18T10:00:00+00:00",
     "bias": 3.0, "outliers": []}
  ]
}
```

### 累计漂移量积分

在观测覆盖范围内对当前分段线性 bias 做精确积分。查询边界可落在插值区间**内部**（按现有区间裁剪）；`from` 必须严格小于 `to`，任一边界超出 `[最早观测, 最晚观测]` 返回 400。有号面积用梯形公式；bias 在段内穿越 0 时按**解析交点**拆成两个三角形求绝对面积，不使用固定步长采样。

假设已登记三个观测：`08:00` bias **+2**、`09:00` bias **−2**、`10:00` bias **+4**（两段均过零）：

```bash
curl 'http://localhost:8000/drift-integral?from=2026-09-18T08:00:00Z&to=2026-09-18T10:00:00Z'
```

```json
{
  "from": 1789718400.0, "to": 1789725600.0,
  "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
  "signed_bias_seconds": 1000.0,
  "absolute_bias_seconds": 2666.6666666666665,
  "contributions": [
    {
      "t_start": 1789718400.0, "t_end": 1789722000.0,
      "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T09:00:00+00:00",
      "bias_start": 2.0, "bias_end": -2.0,
      "signed_bias_seconds": 0.0,
      "absolute_bias_seconds": 1000.0,
      "zero_crossing": 1789720200.0,
      "zero_crossing_iso": "2026-09-18T08:30:00+00:00"
    },
    {
      "t_start": 1789722000.0, "t_end": 1789725600.0,
      "start_iso": "2026-09-18T09:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
      "bias_start": -2.0, "bias_end": 4.0,
      "signed_bias_seconds": 1000.0,
      "absolute_bias_seconds": 1666.6666666666665,
      "zero_crossing": 1789723200.0,
      "zero_crossing_iso": "2026-09-18T09:20:00+00:00"
    }
  ]
}
```

- `signed_bias_seconds = 0 + 1000 = 1000`，`absolute_bias_seconds = 1000 + 1666.67 ≈ 2666.67`：各贡献按时间排序，其有号/绝对面积之和**分别等于**汇总值。
- `zero_crossing` 为该段内部 bias 穿越 0 的解析交点（端点恰为 0 不算段内过零，字段为 `null`）。
- 查询窗口落在区间内部时，贡献段自动裁剪，例如 `from=08:30&to=09:15` 只返回两段被裁剪后的贡献，`bias_start`/`bias_end` 为裁剪边界处的插值偏差。
- 面积单位为 **bias·秒**（偏差单位 × 时间秒），可正可负；绝对面积恒非负。

### 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 查询时点/积分窗口超出校准覆盖范围，或 `from` 不小于 `to`（响应含可用区间） |
| 404 | 修订/查询修订历史时观测 ID 不存在 |
| 409 | 同一时点重复登记观测；修订时版本过期或目标时点已被占用 |
| 422 | 读数不足 3 次、全部读数被判异常、参数非法 |

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 运行测试
uvicorn app.main:app --reload       # 本地启动（默认 ./calibration.db）
```

环境变量 `CALIB_DB_PATH` 可覆盖 SQLite 文件路径（容器内默认 `/data/calibration.db`）。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  service.py      # 观测入库、相邻区间重算、补偿查询
  calibration.py  # 纯计算：异常判定、偏差、线性插值
  models.py       # Pydantic 请求/响应模型
  db.py           # SQLite 连接与建表
tests/test_api.py # 端到端 API 测试
Dockerfile / docker-compose.yml
```
