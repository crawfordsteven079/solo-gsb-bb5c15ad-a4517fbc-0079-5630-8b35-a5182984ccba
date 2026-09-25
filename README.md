# 临时展位三相配电排布 API

基于 **Python + FastAPI + SQLite** 的临时展位三相配电排布服务。

提交最多 **12 台设备**（唯一编号、正整数功率、非空可接入相位集合）以及 **A/B/C 各相容量**，
服务在「所有设备均接入、每台设备整体接入单一允许相位、各相不超容量」的约束下求解分配方案：

1. **首要目标**：使三相负载最大值与最小值之差最小；
2. **次要目标**：差值相同时，取按设备编号排序后的相位序列字典序最小的方案；
3. 不存在可行分配时，返回明确的 `feasible: false` 结果与说明信息。

每次计算的**输入、分配结果与各相负载**均持久化到 SQLite，可随时查询历史记录。
对于原容量下无可行分配的记录，还可通过扩容接口获取「各相最小扩容量 + 对应分配」
建议（保持设备功率与允许相位不变，且不修改原记录）。

此外，服务提供**稳健分相预演**接口：每台设备功率为正整数区间
`[min_power, max_power]`，区间内可独立变化。接口求解「每台设备接入一个允许相位、
且**任意功率组合下各相均不超容量**」的分配，并在其中最小化任意功率组合可能产生的
三相最大负载与最小负载之差。预演结果**不写入历史计算记录**。

对于一条**已有可行分配**的计算记录，还可调用**逐台启动顺序预演**接口：请求逐台
给出每台设备的**启动瞬时功率**（正整数且不低于额定功率），服务沿用记录中的固定
相位与各相容量，在「设备一次启动一台、每个启动瞬间与每个稳定状态各相均不超容量」
的约束下，找出使全过程三相负载「最大值 − 最小值」的**峰值最小**的启动顺序；预演
同样**不修改历史记录**。

## 快速开始（一键启动）

前置条件：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

依赖安装与数据库初始化（自动建表）均在容器启动时完成，无需任何额外步骤。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根端口 |
| http://localhost:8000/docs | Swagger 交互式 API 文档 |
| http://localhost:8000/health | 健康检查 |

停止服务：`docker compose down`（加 `-v` 可同时清除数据卷）。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/app.db`；本地运行 `data/app.db` | SQLite 数据库文件路径，通过环境变量覆盖 |
| 端口映射 | `8000:8000` | 在 `docker-compose.yml` 的 `ports` 中调整宿主机端口 |

数据库文件保存在 Compose 命名卷 `app-data` 中，容器重建后历史计算记录不丢失。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/calculations` | 创建一次排布计算，返回最优分配、各相负载与差值 |
| `GET` | `/api/v1/calculations` | 查询历史计算记录摘要（支持 `limit` / `offset` 分页） |
| `GET` | `/api/v1/calculations/{id}` | 查询单条记录详情（含原始输入设备与分配结果） |
| `POST` | `/api/v1/calculations/{id}/capacity-upgrade` | 为「无可行分配」的记录给出最小扩容建议（原记录不变） |
| `POST` | `/api/v1/calculations/{id}/startup-plan` | 为「有可行分配」的记录预演逐台启动顺序（原记录不变） |
| `POST` | `/api/v1/robust-rehearsals` | 稳健分相预演：设备功率为区间，求任意功率组合下均不超容量的最优分配（不持久化） |
| `GET` | `/health` | 健康检查 |

### 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
      {"id": "D1", "power": 10, "allowed_phases": ["A", "B", "C"]},
      {"id": "D2", "power": 20, "allowed_phases": ["A", "B", "C"]},
      {"id": "D3", "power": 30, "allowed_phases": ["B", "C"]}
    ]
  }'
```

### 响应示例（有可行方案）

```json
{
  "id": 1,
  "created_at": "2026-09-24T08:00:00.000000+00:00",
  "capacities": {"A": 100, "B": 100, "C": 100},
  "feasible": true,
  "imbalance": 20,
  "loads": {"A": 10, "B": 20, "C": 30},
  "message": "计算成功：已找到三相不平衡度最小的分配方案",
  "assignments": [
    {"device_id": "D1", "phase": "A"},
    {"device_id": "D2", "phase": "B"},
    {"device_id": "D3", "phase": "C"}
  ]
}
```

### 响应示例（无可行方案）

```json
{
  "id": 2,
  "created_at": "2026-09-24T08:01:00.000000+00:00",
  "capacities": {"A": 5, "B": 0, "C": 0},
  "feasible": false,
  "imbalance": null,
  "loads": null,
  "message": "无可行分配：不存在所有设备均接入且各相不超容量的方案",
  "assignments": []
}
```

## 容量扩容建议

对于返回 `feasible: false` 的计算记录，可调用扩容接口。服务**保持设备功率与允许
相位不变**，计算 A/B/C 各相需要增加的**非负整数容量**以及扩容后的设备分配，
使所有设备都能接入。优化目标依次为：

1. **三相扩容总量最小**（`total_upgrade`）；
2. 总量相同时，**单相最大扩容量最小**（`max_upgrade`）；
3. 仍相同时，取按设备编号排序的相位序列**字典序最小**的方案。

接口只返回建议，**原计算记录不做任何修改**。记录不存在返回 `404`；
原记录已有可行分配（无需扩容）返回 `409`。

### 请求示例

```bash
# 先创建一条无可行分配的记录
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 5, "B": 0, "C": 0},
    "devices": [
      {"id": "BIG", "power": 50, "allowed_phases": ["A", "B", "C"]},
      {"id": "FIX", "power": 7, "allowed_phases": ["A"]}
    ]
  }'

# 再对该记录请求扩容建议（假设记录 id 为 2）
curl -X POST http://localhost:8000/api/v1/calculations/2/capacity-upgrade
```

### 响应示例

```json
{
  "id": 2,
  "created_at": "2026-09-24T08:01:00.000000+00:00",
  "original_capacities": {"A": 5, "B": 0, "C": 0},
  "capacity_upgrades": {"A": 2, "B": 50, "C": 0},
  "suggested_capacities": {"A": 7, "B": 50, "C": 0},
  "total_upgrade": 52,
  "max_upgrade": 50,
  "assignments": [
    {"device_id": "BIG", "phase": "B"},
    {"device_id": "FIX", "phase": "A"}
  ],
  "loads": {"A": 7, "B": 50, "C": 0},
  "message": "扩容建议：已在最小化三相扩容总量、再最小化单相最大扩容量的目标下，找到使所有设备均接入的容量与分配方案"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `original_capacities` | 原各相容量 |
| `capacity_upgrades` | 各相需要增加的非负整数容量 |
| `suggested_capacities` | 建议容量 = 原容量 + 扩容量 |
| `total_upgrade` / `max_upgrade` | 三相扩容总量 / 单相最大扩容量 |
| `assignments` | 建议容量下的设备相位分配（按设备编号排序） |
| `loads` | 该分配下各相的实际负载（均不超过建议容量） |

### 输入校验规则

- 设备数量 **1 ~ 12** 台，设备编号**唯一**且非空；
- 功率为**正整数**；
- `allowed_phases` 为**非空**数组，元素取自 `A` / `B` / `C`（自动去重）；
- 各相容量为**非负整数**。

不满足规则时返回 `422` 及具体错误信息。

## 稳健分相预演

`POST /api/v1/robust-rehearsals` 用于设备功率尚未确定、只能给出波动区间时的
事前推演。每台设备的功率为正整数区间 `[min_power, max_power]`（**下界不大于
上界**），各设备功率在区间内**独立变化**。接口寻找每台设备接入一个允许相位、
且**任意功率组合下各相均不超容量**的分配（稳健可行），并在稳健可行方案中：

1. **最小化**任意功率组合可能产生的「三相最大负载与最小负载之差」
   （`worst_imbalance`）；
2. 同值时取按设备编号排序的相位序列**字典序最小**的方案。

接口**只做预演，不写入历史计算记录**；无稳健可行方案时返回
`feasible: false` 及说明信息。

### 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/robust-rehearsals \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
      {"id": "D1", "min_power": 5,  "max_power": 10, "allowed_phases": ["A", "B", "C"]},
      {"id": "D2", "min_power": 15, "max_power": 20, "allowed_phases": ["A", "B", "C"]},
      {"id": "D3", "min_power": 25, "max_power": 30, "allowed_phases": ["B", "C"]}
    ]
  }'
```

### 响应示例（稳健可行）

```json
{
  "capacities": {"A": 100, "B": 100, "C": 100},
  "feasible": true,
  "worst_imbalance": 25,
  "load_lower_bounds": {"A": 5, "B": 15, "C": 25},
  "load_upper_bounds": {"A": 10, "B": 20, "C": 30},
  "assignments": [
    {"device_id": "D1", "phase": "A"},
    {"device_id": "D2", "phase": "B"},
    {"device_id": "D3", "phase": "C"}
  ],
  "message": "预演成功：已找到任意功率组合下各相均不超容量、且最坏负载差最小的稳健分配方案"
}
```

### 响应示例（无稳健可行方案）

```json
{
  "capacities": {"A": 40, "B": 40, "C": 40},
  "feasible": false,
  "worst_imbalance": null,
  "load_lower_bounds": null,
  "load_upper_bounds": null,
  "assignments": [],
  "message": "无稳健可行分配：不存在任意功率组合下各相均不超容量的接入方案"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `feasible` | 是否存在稳健可行分配 |
| `worst_imbalance` | 任意功率组合下「三相最大负载 - 最小负载」的最大值 |
| `load_lower_bounds` | 各相负载下界（该相设备均取功率下界之和） |
| `load_upper_bounds` | 各相负载上界（该相设备均取功率上界之和，均不超过容量） |
| `assignments` | 稳健分配方案（按设备编号排序）；无解时为空数组 |

### 输入校验规则

- 设备数量 **1 ~ 12** 台，设备编号**唯一**且非空；
- `min_power` / `max_power` 均为**正整数**，且 `min_power` **不大于** `max_power`；
- `allowed_phases` 为**非空**数组，元素取自 `A` / `B` / `C`（自动去重）；
- 各相容量为**非负整数**。

不满足规则时返回 `422` 及具体错误信息。

## 逐台启动顺序预演

`POST /api/v1/calculations/{id}/startup-plan` 针对一条**已有可行分配**的计算记录，
预演设备**一次一台**依次上电的最佳顺序。服务**沿用记录中每台设备的固定相位与
A/B/C 各相容量**，请求体逐台给出每台设备的**启动瞬时功率**：

- 启动瞬时功率为**正整数**，且**不低于该设备在记录中的额定功率**；
- 某台设备启动**瞬间**，其所在相负载 = 此前该相稳定负载 + 启动瞬时功率；
- 启动**完成后**该设备按**额定功率**持续运行，该相稳定负载 = 此前稳定负载 + 额定功率；
- 每个启动瞬间与每个稳定状态，A/B/C 各相负载都**不得超过该相容量**。

优化目标：

1. **最小化全过程峰值**（`peak`）：所有启动瞬间与稳定状态中
   「三相最大负载 − 三相最小负载」的最大值；
2. 峰值相同时，取**设备编号序列字典序最小**的启动顺序；
3. 不存在任何安全启动顺序时，返回 `feasible: false`（HTTP 状态码仍为 200）。

接口**只做预演，不修改原计算记录**，也不新增历史记录；重复调用结果确定。
记录不存在返回 `404`；原记录为无可行分配返回 `409`；请求设备编号与记录不完全
一致（有缺少或多余、或编号重复）或启动瞬时功率低于额定功率返回 `422`。

### 请求示例

```bash
# 先创建一条有可行分配的记录（假设记录 id 为 1）
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
      {"id": "D1", "power": 2, "allowed_phases": ["A", "B", "C"]},
      {"id": "D2", "power": 1, "allowed_phases": ["A", "B", "C"]}
    ]
  }'

# 逐台给出启动瞬时功率（设备编号与记录完全一致，数组顺序任意）
curl -X POST http://localhost:8000/api/v1/calculations/1/startup-plan \
  -H "Content-Type: application/json" \
  -d '{
    "devices": [
      {"id": "D1", "startup_power": 3},
      {"id": "D2", "startup_power": 4}
    ]
  }'
```

### 响应示例（存在安全启动顺序）

```json
{
  "id": 1,
  "created_at": "2026-09-25T08:00:00.000000+00:00",
  "capacities": {"A": 100, "B": 100, "C": 100},
  "feasible": true,
  "order": ["D1", "D2"],
  "peak": 4,
  "steps": [
    {
      "step": 1,
      "device_id": "D1",
      "startup_loads": {"A": 3, "B": 0, "C": 0},
      "stable_loads": {"A": 2, "B": 0, "C": 0}
    },
    {
      "step": 2,
      "device_id": "D2",
      "startup_loads": {"A": 2, "B": 4, "C": 0},
      "stable_loads": {"A": 2, "B": 1, "C": 0}
    }
  ],
  "message": "启动预演成功：已找到全过程各启动瞬间与稳定状态三相负载差峰值最小、且峰值相同时设备编号序列字典序最小的安全启动顺序"
}
```

### 响应示例（无安全启动顺序）

两台设备额定功率分别为 6、7（合计 13，恰占满 A 相容量），但启动瞬时功率分别为
8、9，A 相容量仅 13：任何一台先启动，另一台的启动瞬间负载都为 15 而超载。

```bash
curl -X POST http://localhost:8000/api/v1/calculations/2/startup-plan \
  -H "Content-Type: application/json" \
  -d '{
    "devices": [
      {"id": "X", "startup_power": 8},
      {"id": "Y", "startup_power": 9}
    ]
  }'
```

```json
{
  "id": 2,
  "created_at": "2026-09-25T08:01:00.000000+00:00",
  "capacities": {"A": 13, "B": 0, "C": 0},
  "feasible": false,
  "order": null,
  "steps": null,
  "peak": null,
  "message": "无安全启动顺序：不存在设备逐台启动且每个启动瞬间与稳定状态各相均不超容量的顺序"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `order` | 最优启动顺序（设备编号序列）；无解时为 `null` |
| `steps[].startup_loads` | 该步设备启动**瞬间**的各相负载（该设备按启动瞬时功率计） |
| `steps[].stable_loads` | 该步启动**完成后**的各相稳定负载（该设备按额定功率计） |
| `peak` | 全过程所有启动瞬间与稳定状态中「三相最大负载 − 最小负载」的峰值 |
| `capacities` | 沿用自原记录的各相容量 |

### 请求校验规则

- `devices` 中的设备编号必须与原记录**完全一致**：不重、不漏、不多（顺序任意）；
- 每台设备的 `startup_power` 为**正整数**，且**不低于**记录中该设备的额定功率；
- 不满足规则时返回 `422`；记录不存在 `404`；原记录不可行 `409`。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 服务地址 http://127.0.0.1:8000
```

运行测试：

```bash
pytest                               # 全部测试（求解器 + API）
python3 -m unittest tests.test_solver  # 仅求解器测试（无第三方依赖）
```

## 项目结构

```
├── app/
│   ├── main.py       # FastAPI 应用与路由
│   ├── schemas.py    # 请求 / 响应数据模型与校验
│   ├── solver.py     # 三相排布求解器（DFS + 下界剪枝，纯标准库）
│   └── db.py         # SQLite 持久化（启动时自动建表）
├── tests/            # 求解器单元测试（含随机对拍）与 API 集成测试
├── requirements.txt  # 运行依赖
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 求解算法说明

按设备编号排序后做深度优先搜索，相位按 A→B→C 顺序展开，因此完整方案按
「设备编号排序后的相位序列」字典序依次产生；仅在负载差值**严格更小**时更新最优解，
同差值时自然保留字典序最小方案。配合容量约束剪枝与负载差值下界剪枝，
12 台设备的最坏情形亦可在秒级内完成求解。

**扩容建议**（`capacity-upgrade`）同样按设备编号排序、相位按 A→B→C 展开做 DFS，
分两遍搜索：第一遍求三相扩容总量的最小值，第二遍在总量固定最小的方案中以
`(单相最大扩容量, 相位序列)` 为键选出最优（即先最小化单相最大扩容，再取字典序
最小）。搜索中利用「只允许单一相位的设备造成的被迫超载 + 自由设备功率扣除各相
空闲容量」构造扩容总量下界进行剪枝，12 台设备的最坏情形亦可在数秒内完成。

**稳健分相预演**（`robust-rehearsals`）利用如下归约：对固定分配，各相负载可
独立取遍「功率下界之和 ~ 功率上界之和」内的所有整数，因此任意功率组合下各相
均不超容量 ⟺ 各相负载**上界**不超容量；而最坏负载差
`max(max_p L_p - min_p L_p) = max_{p≠q}(上界_p - 下界_q)`（注意并非「上界最大值
- 下界最小值」：同一相不可能同时取到上界与下界，故只取相异相位对）。据此在与
主求解器相同的 DFS 框架（按设备编号排序、相位按 A→B→C 展开、仅严格更优才更新、
容量约束与下界剪枝）中直接求解，12 台设备的最坏情形亦可在数秒内完成。

**逐台启动顺序预演**（`startup-plan`）用子集动态规划精确求解。关键观察：
「已启动设备集合」唯一确定当前各相稳定负载（已启动设备的额定功率之和），与启动
的先后顺序无关，故以已启动集合的位掩码为状态（至多 2^12 = 4096 个）。设
`f(mask)` 为从该稳定状态出发启动其余设备的全过程还将达到的最小峰值，则对每台
可安全启动的设备 j（启动瞬间不超容量）：

```
f(mask) = min_j max(g_j(mask), f(mask ∪ {j}))
```

其中 `g_j(mask)` 为 j 的启动瞬间与启动后稳定状态「最大相负载 − 最小相负载」的
较大者；全集状态 `f = 0`，按掩码递减一次填表即可得到最优峰值 `f(∅)`，无解时
`f(∅)` 为空。最优峰值确定后，再按设备编号升序沿「恰能达到该峰值」的转移重构，
得到同峰值下字典序最小的完整启动顺序，并回放生成逐步的启动瞬间 / 稳定负载快照。
12 台设备的全部情形均可在毫秒级完成。
