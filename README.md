# Incremental Build Planner API

一个基于 Python / FastAPI 的增量构建规划服务。提交节点构成的依赖图（节点名、
内容摘要、直接依赖名）以及上次构建记录的节点指纹，服务会：

1. 按依赖拓扑计算每个节点的 **SHA-256 指纹**（内容摘要 + 按名称排序的直接
   依赖名及其指纹）；
2. 指纹与上次相同的节点标记为 **缓存命中**——命中节点不进入构建批次，但其指纹
   仍然参与所有下游节点的计算；
3. 只为未命中节点输出可**并行执行的构建批次**，依赖于命中节点时不额外等待；
4. 缺失依赖、依赖成环、重名节点等情况返回结构化的 `400` 错误；
5. 结果与请求中节点/依赖的书写顺序无关（按名称确定性排序）。

另外，`POST /impacts` 接收 `before`、`after` 两份节点图（字段与 `/plans`
相同），做**变更影响分析**：列出已删除节点，把新图中的新增节点、内容摘要变化、
直接依赖集合变化标为根因，并沿新图依赖关系给出每个受影响节点到各根因的最短
传导路径。

`POST /cache-retention` 面向**构建前的缓存清理**：在容量限制下，从已有制品中
精确挑出要保留的集合，使目标节点的重建总代价最低（详见下文对应章节）。

`POST /execution-audits` 面向**构建后的执行核查**：沿用 `/plans` 的图与历史
指纹（命中节点视为初始完成），按 `seq` 回放构建执行事件流（start/success/
failure），校验每个事件是否合法，忽略违规事件并给出每个节点的终态（详见
下文对应章节）。

`POST /failure-isolation` 面向**发布前的失败隔离**：在依赖图中精确选出一组
隔离节点（视为从图中移除），阻断所有失败构建节点通向发布目标的路径，并使
隔离总成本最低（详见下文对应章节）。

`POST /release-bundles` 面向**发布制品包选择**：在最多 16 个候选制品中，
按与 `/plans` 相同的规则重算指纹并精确选出目标节点及其全部传递依赖各一个
互相一致的制品，使发布包总大小最小（详见下文对应章节）。

`POST /release-transitions` 面向**发布包制品切换规划**：给出当前包与目标包
各闭包节点选择的制品 id，精确求把当前包逐节点切换为目标包的**最少步骤**，
允许经过渡制品或回退，每一步之后的中间包都必须依赖一致；同长方案按整条
`(节点, 制品 id)` 序列字典序取最小（详见下文对应章节）。

`POST /release-policies` 面向**组织条款发布判定**：每条直接依赖边标记为
`bundled`（随发布打包）或 `build_only`（仅构建期需要），仅沿 `bundled` 边取
目标的传递闭包，检查闭包内节点携带的条款标签是否全部已履行、是否出现互斥
标签对，并给出每个缺失标签与每对冲突从目标到标签来源的最短依赖路径（详见
下文对应章节）。

## 目录结构

```
app/
  main.py       # FastAPI 入口：POST /plans、/impacts、/cache-retention、/execution-audits、/failure-isolation、/release-bundles、/release-transitions、/release-policies、GET /health
  models.py     # 请求/响应 Pydantic 模型
  planner.py    # 图校验、指纹、拓扑排序、缓存判定与批次划分（纯函数核心）
  impacts.py    # 两图对比、根因识别与最短传导路径分析（纯函数核心）
  retention.py  # 容量受限的缓存保留选择（纯函数核心）
  audit.py      # 执行事件流回放与违规核查（纯函数核心）
  isolation.py  # 发布前失败隔离节点的最小成本选择（纯函数核心）
  bundles.py    # 依赖一致的发布制品包精确选择（纯函数核心）
  transitions.py# 发布包之间最少步骤的逐节点制品切换规划（纯函数核心）
  policies.py   # 组织条款标签的发布判定（纯函数核心）
tests/
  test_planner.py
  test_impacts.py
  test_retention.py
  test_audit.py
  test_isolation.py
  test_bundles.py
  test_transitions.py
  test_policies.py
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务启动后监听 <http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

不使用 Docker 时，也可以直接用 uvicorn 启动（需要 Python 3.11+，依赖见
`requirements.txt`）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 开发模式（代码变更自动重载）：
uvicorn app.main:app --reload --port 8000
```

随后即可向 <http://localhost:8000> 发送请求，例如：

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/release-transitions \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"name":"a","content_digest":"a","dependency_names":[]}],"target":"a","artifacts":[{"id":"a1","node":"a","fingerprint":"f1","size":1,"dependencies":{}},{"id":"a2","node":"a","fingerprint":"f2","size":1,"dependencies":{}}],"current_ids":["a1"],"next_ids":["a2"]}'
# {"possible":true,"steps":[{"node":"a","id":"a2","bundle":{"a":"a2"}}]}
```

## 请求示例

### 1. 全量构建（无历史指纹，全部未命中）

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core", "content_digest": "core-src-v1", "dependency_names": []},
      {"name": "util", "content_digest": "util-src-v1", "dependency_names": ["core"]},
      {"name": "api",  "content_digest": "api-src-v1",  "dependency_names": ["util", "core"]},
      {"name": "web",  "content_digest": "web-src-v1",  "dependency_names": ["api"]},
      {"name": "docs", "content_digest": "docs-src-v1", "dependency_names": []}
    ]
  }'
```

响应（`201 Created`）：

```json
{
  "plans": {
    "api":  {"fingerprint": "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848", "cache_hit": false, "batch": 2, "dependencies": ["core", "util"]},
    "core": {"fingerprint": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714", "cache_hit": false, "batch": 0, "dependencies": []},
    "docs": {"fingerprint": "69e6c050b3b05da340d1854a36aa7bdebc8eeae67f0f1846e8015d8f02267cd1", "cache_hit": false, "batch": 0, "dependencies": []},
    "util": {"fingerprint": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8", "cache_hit": false, "batch": 1, "dependencies": ["core"]},
    "web":  {"fingerprint": "bba914464b9203d19c8f99136f433911d07b8a1e59f0e3d34f08797a8062c338", "cache_hit": false, "batch": 3, "dependencies": ["api"]}
  },
  "batches": [
    ["core", "docs"],
    ["util"],
    ["api"],
    ["web"]
  ]
}
```

`batches[0]` 内的节点无任何未命中的上游，可立即并行构建；批次号严格递增，
同批可并行、批次间等待。

### 2. 增量构建（回传上次指纹，仅 `api` 内容变化）

把上一次响应中的指纹原样放入 `previous_fingerprints`，并将 `api` 的内容摘要
从 `api-src-v1` 改为 `api-src-v2`：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core", "content_digest": "core-src-v1", "dependency_names": []},
      {"name": "util", "content_digest": "util-src-v1", "dependency_names": ["core"]},
      {"name": "api",  "content_digest": "api-src-v2",  "dependency_names": ["util", "core"]},
      {"name": "web",  "content_digest": "web-src-v1",  "dependency_names": ["api"]},
      {"name": "docs", "content_digest": "docs-src-v1", "dependency_names": []}
    ],
    "previous_fingerprints": {
      "core": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714",
      "util": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8",
      "api":  "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848",
      "web":  "bba914464b9203d19c8f99136f433911d07b8a1e59f0e3d34f08797a8062c338",
      "docs": "69e6c050b3b05da340d1854a36aa7bdebc8eeae67f0f1846e8015d8f02267cd1"
    }
  }'
```

响应：

```json
{
  "plans": {
    "core": {"fingerprint": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714", "cache_hit": true,  "batch": null, "dependencies": []},
    "util": {"fingerprint": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8", "cache_hit": true,  "batch": null, "dependencies": ["core"]},
    "docs": {"fingerprint": "69e6c050b3b05da340d1854a36aa7bdebc8eeae67f0f1846e8015d8f02267cd1", "cache_hit": true,  "batch": null, "dependencies": []},
    "api":  {"fingerprint": "d34604a6919f36ace1c17050340656adf635f51bd39680ce077dcd95b8ed469a", "cache_hit": false, "batch": 0, "dependencies": ["core", "util"]},
    "web":  {"fingerprint": "288e7266fd5896705462469815b80ca0a182271457ec6ce1abae2f6df5671d28", "cache_hit": false, "batch": 1, "dependencies": ["api"]}
  },
  "batches": [
    ["api"],
    ["web"]
  ]
}
```

注意：

- `core`、`util`、`docs` 命中，`batch` 为 `null`，不进入任何批次；
- `api` 虽然依赖 `core`、`util`（均命中），**直接进入批次 0，不额外等待**；
- `web` 自身内容未变，但因其依赖 `api` 的指纹变了，指纹随之改变并进入批次 1；
- 全部命中时 `batches` 为 `[]`。

### 3. 错误示例

缺失依赖（HTTP `400`）：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"name":"a","content_digest":"x","dependency_names":["ghost"]}]}'
```

```json
{
  "code": "missing_dependency",
  "message": "One or more nodes depend on names that are not in the graph",
  "details": {"missing": {"a": ["ghost"]}}
}
```

依赖成环（HTTP `400`，`cycle` 给出一条首尾相接的环路径）：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[
    {"name":"a","content_digest":"x","dependency_names":["b"]},
    {"name":"b","content_digest":"y","dependency_names":["a"]}
  ]}'
```

```json
{
  "code": "cycle_detected",
  "message": "Dependency graph contains a cycle",
  "details": {"cycle": ["a", "b", "a"]}
}
```

其他错误码：`duplicate_node`（节点重名，HTTP `400`）；请求体字段类型错误、
未知字段或空名称由 Pydantic 拒绝（HTTP `422`）。

## POST /impacts：变更影响分析

`POST /impacts` 接收 `before`、`after` 两份节点图（节点字段与 `/plans`
完全相同），比较两图并沿**新图**的依赖关系传播影响：

- `deleted_nodes`：只存在于 `before` 的节点名（已删除，按名称排序）；
- `root_causes`：新图中的变更根因，每个节点带一到多种变更类型——
  - `added`：新增节点（只存在于 `after`）；
  - `content_changed`：同名节点的 `content_digest` 发生变化；
  - `dependencies_changed`：同名节点的直接依赖**集合**发生变化
    （仅调整书写顺序不算变化）；
- `impacts`：所有受影响节点（含根因自身）。`impacts[节点][根因]` 给出该
  根因的变更类型 `changes` 和从根因到该节点的**最短传导路径** `path`
  （沿“被依赖 → 依赖方”方向）；等长路径按节点名序列的字典序取舍，根因到
  自身的路径只含自身一个节点。

两份图各自独立校验重名、缺失依赖与环；校验失败返回 `400`，错误
`details.graph` 为 `"before"` 或 `"after"` 指明是哪份图。两图中节点与
依赖的书写顺序均不影响结果。

### 调用示例

下面的例子里：`core` 内容摘要变化、`api` 的依赖集合变化（去掉 `core`）、
`metrics` 是新增节点、`legacy` 被删除：

```bash
curl -s -X POST http://localhost:8000/impacts \
  -H 'Content-Type: application/json' \
  -d '{
    "before": [
      {"name": "core",   "content_digest": "core-v1",   "dependency_names": []},
      {"name": "util",   "content_digest": "util-v1",   "dependency_names": ["core"]},
      {"name": "api",    "content_digest": "api-v1",    "dependency_names": ["util", "core"]},
      {"name": "web",    "content_digest": "web-v1",    "dependency_names": ["api"]},
      {"name": "docs",   "content_digest": "docs-v1",   "dependency_names": []},
      {"name": "legacy", "content_digest": "legacy-v1", "dependency_names": []}
    ],
    "after": [
      {"name": "core",    "content_digest": "core-v2",    "dependency_names": []},
      {"name": "util",    "content_digest": "util-v1",    "dependency_names": ["core"]},
      {"name": "api",     "content_digest": "api-v1",     "dependency_names": ["util"]},
      {"name": "web",     "content_digest": "web-v1",     "dependency_names": ["api"]},
      {"name": "docs",    "content_digest": "docs-v1",    "dependency_names": []},
      {"name": "metrics", "content_digest": "metrics-v1", "dependency_names": ["api"]}
    ]
  }'
```

响应（`200 OK`）：

```json
{
  "deleted_nodes": ["legacy"],
  "root_causes": {
    "api":     {"changes": ["dependencies_changed"]},
    "core":    {"changes": ["content_changed"]},
    "metrics": {"changes": ["added"]}
  },
  "impacts": {
    "api": {
      "api":  {"changes": ["dependencies_changed"], "path": ["api"]},
      "core": {"changes": ["content_changed"],      "path": ["core", "util", "api"]}
    },
    "core": {
      "core": {"changes": ["content_changed"],      "path": ["core"]}
    },
    "metrics": {
      "api":     {"changes": ["dependencies_changed"], "path": ["api", "metrics"]},
      "core":    {"changes": ["content_changed"],      "path": ["core", "util", "api", "metrics"]},
      "metrics": {"changes": ["added"],                "path": ["metrics"]}
    },
    "util": {
      "core": {"changes": ["content_changed"],      "path": ["core", "util"]}
    },
    "web": {
      "api":  {"changes": ["dependencies_changed"], "path": ["api", "web"]},
      "core": {"changes": ["content_changed"],      "path": ["core", "util", "api", "web"]}
    }
  }
}
```

注意：

- `docs` 与任何根因都不连通，不出现在 `impacts` 中；
- `web`、`metrics` 自身内容未变，但处在根因下游，因此被列为受影响节点，
  且每个到达它的根因各有一条记录；
- 根因到自身的路径只含自身，如 `["core"]`；
- 两份图完全相同时 `deleted_nodes`、`root_causes`、`impacts` 均为空。

校验失败示例（`after` 图缺失依赖，HTTP `400`）：

```json
{
  "code": "missing_dependency",
  "message": "One or more nodes depend on names that are not in the graph",
  "details": {"missing": {"a": ["ghost"]}, "graph": "after"}
}
```

## POST /cache-retention：容量受限的缓存保留

构建前清理缓存时，`POST /cache-retention` 在容量限制下**精确**挑选要保留的
制品集合，使目标节点的重建总代价最低。请求包含：

- `nodes`：节点图（字段与 `/plans` 完全相同）；
- `target`：本次构建必须可用的目标节点；
- `rebuild_costs`：每个节点的**非负整数**重建代价（必须恰好覆盖图中所有节点）；
- `artifacts`：已有制品列表，每项含 `name`（节点名）、`fingerprint`（构建时
  记录的指纹）、`size`（**正整数**占用空间）；
- `capacity`：**非负整数**总容量。

规则：

- 制品指纹与当前图重算指纹**相同**才是候选；指纹过期的制品直接淘汰；
- 保留的节点直接可用，不再访问其依赖（保留点“截断”依赖遍历）；其余节点从
  目标出发递归重建，被多条路径共享的上游只重建、计费一次；
- 总代价 = 需重建节点的重建代价之和；在所有总大小不超容量的候选子集中取
  最小总代价，等价最优再按**排序后的保留节点名列表字典序**取最小（因此
  名字更小的制品即使不降低代价也可能被保留）；
- 匹配候选超过 **16** 个时返回 `400 too_many_candidates`（精确枚举以
  2¹⁶ 为上界）；制品重名、目标/制品/代价引用了图中不存在的节点、节点缺少
  重建代价等都会返回明确的 `400` 错误；
- 请求中节点、制品、代价的书写顺序不影响结果。

响应字段：`kept`（保留的制品节点名，排序）、`evicted`（被淘汰的制品节点名，
含指纹过期与未选中的候选，排序）、`rebuild`（需重建节点，排序）、
`total_cost`（重建总代价）。

### 调用示例

沿用上面的示例图：`web` 依赖 `api`，`api` 依赖 `util` 与 `core`。`core`、
`util`、`api` 的制品指纹有效，`docs` 的制品指纹已过期；容量为 14：

```bash
curl -s -X POST http://localhost:8000/cache-retention \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core", "content_digest": "core-src-v1", "dependency_names": []},
      {"name": "util", "content_digest": "util-src-v1", "dependency_names": ["core"]},
      {"name": "api",  "content_digest": "api-src-v1",  "dependency_names": ["util", "core"]},
      {"name": "web",  "content_digest": "web-src-v1",  "dependency_names": ["api"]},
      {"name": "docs", "content_digest": "docs-src-v1", "dependency_names": []}
    ],
    "target": "web",
    "rebuild_costs": {"core": 10, "util": 8, "api": 20, "web": 5, "docs": 3},
    "artifacts": [
      {"name": "core", "fingerprint": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714", "size": 4},
      {"name": "util", "fingerprint": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8", "size": 6},
      {"name": "api",  "fingerprint": "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848", "size": 10},
      {"name": "docs", "fingerprint": "aaaa050b3b05da340d1854a36aa7bdebc8eeae67f0f1846e8015d8f02267cd1", "size": 2}
    ],
    "capacity": 14
  }'
```

响应（`200 OK`）：

```json
{
  "kept": ["api"],
  "evicted": ["core", "docs", "util"],
  "rebuild": ["web"],
  "total_cost": 5
}
```

解读：

- 保留 `api`（大小 10 ≤ 14）后只需重建 `web`，代价 5，为最优；
- `{core, api}` 同样代价 5 且刚好占满容量，但排序后的保留列表
  `["api"]` 字典序小于 `["api", "core"]`，故只保留 `api`；
- `docs` 指纹过期，不是候选，直接列入 `evicted`；
- `util`（大小 6）若保留可截断对 `core` 的传递访问，但 `api` 还**直接**
  依赖 `core`，所以保留 `util` 并不能省去 `core` 的重建。

错误示例（匹配候选超过 16 个，HTTP `400`）：

```json
{
  "code": "too_many_candidates",
  "message": "17 artifacts match the current graph; at most 16 are supported",
  "details": {"candidates": 17, "limit": 16}
}
```

其他错误码：`duplicate_artifact`（同一节点多个制品）、`unknown_target`
（目标不在图中）、`unknown_artifact`（制品引用未知节点）、
`unknown_rebuild_cost` / `missing_rebuild_cost`（代价引用未知节点 / 节点
缺少代价），均为 HTTP `400`；图本身的 `duplicate_node`、
`missing_dependency`、`cycle_detected` 校验与 `/plans` 一致；容量为负、
制品大小非正整数、代价为负、未知字段等由 Pydantic 拒绝（HTTP `422`）。

## POST /execution-audits：构建执行核查

`POST /execution-audits` 接收与 `/plans` 相同的 `nodes`、
`previous_fingerprints`，另加一份构建执行事件列表 `events`（每项含唯一
非负整数 `seq`、`node`、事件类型 `type`：`start` / `success` / `failure`），
按 `seq` 升序回放并核查每个事件是否合法：

- 按 `/plans` 的指纹规则重算指纹，**命中节点视为初始完成**，终态为
  `cached`；未命中节点初始为 `pending`；
- `start` 仅当节点处于 `pending` **且所有直接依赖已完成**（`cached` 或
  `succeeded`）时合法，节点转为 `running`；
- `success` / `failure` 仅能结束**已开始且未结束**（即 `running`）的节点，
  分别转为 `succeeded` / `failed`；
- 违规事件被**忽略且不改变任何状态**，按 `seq`、`node` 和原因记入
  `violations`（同时回显事件 `type`），原因取值：
  - `dependencies_incomplete`：`start` 时存在未完成的直接依赖；
  - `not_pending`：对非 `pending` 节点（已缓存/运行中/已结束）执行 `start`；
  - `not_running`：对非 `running` 节点执行 `success` / `failure`；
- 响应 `states` 给出每个节点的终态：`cached`、`succeeded`、`failed`、
  `running`、`pending` 或 `blocked`——**失败节点的未完成后继**（传递闭包）
  均为 `blocked`，因为失败的依赖永远不会完成，后继无法再开始；
- `events` 在请求中的书写顺序不影响结果（`seq` 唯一，回放顺序固定）。

错误：事件 `seq` 重复返回 `400 duplicate_seq`；事件引用图中不存在的节点
返回 `400 unknown_node`；图本身的 `duplicate_node`、`missing_dependency`、
`cycle_detected` 校验与 `/plans` 一致；`seq` 为负、未知事件类型、未知字段
等由 Pydantic 拒绝（HTTP `422`）。

### 调用示例

沿用 `/plans` 示例 2 的图与历史指纹（`core`、`util`、`docs` 命中，`api`
内容变为 `api-src-v2`，`web` 指纹随之下游变化），并新增依赖 `web` 的
`deploy`。注意 `events` 在请求中故意乱序，回放仍按 `seq` 进行：

```bash
curl -s -X POST http://localhost:8000/execution-audits \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core",   "content_digest": "core-src-v1",   "dependency_names": []},
      {"name": "util",   "content_digest": "util-src-v1",   "dependency_names": ["core"]},
      {"name": "api",    "content_digest": "api-src-v2",    "dependency_names": ["util", "core"]},
      {"name": "web",    "content_digest": "web-src-v1",    "dependency_names": ["api"]},
      {"name": "docs",   "content_digest": "docs-src-v1",   "dependency_names": []},
      {"name": "deploy", "content_digest": "deploy-src-v1", "dependency_names": ["web"]}
    ],
    "previous_fingerprints": {
      "core": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714",
      "util": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8",
      "api":  "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848",
      "web":  "bba914464b9203d19c8f99136f433911d07b8a1e59f0e3d34f08797a8062c338",
      "docs": "69e6c050b3b05da340d1854a36aa7bdebc8eeae67f0f1846e8015d8f02267cd1"
    },
    "events": [
      {"seq": 2, "node": "api",  "type": "success"},
      {"seq": 0, "node": "api",  "type": "start"},
      {"seq": 4, "node": "web",  "type": "failure"},
      {"seq": 1, "node": "web",  "type": "start"},
      {"seq": 5, "node": "docs", "type": "start"},
      {"seq": 3, "node": "web",  "type": "start"}
    ]
  }'
```

响应（`200 OK`）：

```json
{
  "states": {
    "api": "succeeded",
    "core": "cached",
    "deploy": "blocked",
    "docs": "cached",
    "util": "cached",
    "web": "failed"
  },
  "violations": [
    {"seq": 1, "node": "web",  "type": "start", "reason": "dependencies_incomplete"},
    {"seq": 5, "node": "docs", "type": "start", "reason": "not_pending"}
  ]
}
```

解读：

- `seq 0–2`：`api` 的依赖 `core`、`util` 均命中（视为已完成），`start` 合法，
  随后 `success` 结束为 `succeeded`；
- `seq 1`：`web` 在 `api` 尚未完成时 `start`，违规被忽略（`api` 此时仍在
  运行），`web` 保持 `pending`；`seq 3` 的再次 `start` 合法；
- `seq 4`：`web` 构建失败，终态 `failed`；其后继 `deploy` 从未开始且再无
  可能开始，终态 `blocked`；
- `seq 5`：`docs` 是缓存命中节点，不接受 `start`，违规被忽略；
- 全部命中且无事件时 `states` 全为 `cached`、`violations` 为 `[]`。

错误示例（事件 `seq` 重复，HTTP `400`）：

```json
{
  "code": "duplicate_seq",
  "message": "Event seq values must be unique",
  "details": {"seq": [3]}
}
```

## POST /failure-isolation：发布前失败隔离

发布前，`POST /failure-isolation` 在依赖图中**精确**选出一组隔离节点，阻断
所有失败构建节点通向发布目标的路径，并使隔离总成本最低。请求包含：

- `nodes`：节点图（字段与 `/plans` 完全相同）；
- `failed_nodes`：**非空**的失败构建节点名列表（不可重复）；
- `targets`：**非空**的发布目标节点名列表（不可重复）；
- `isolation_costs`：可隔离节点名 → **非负整数**隔离成本（失败节点与目标
  不能作为可隔离节点）。

规则：

- 路径沿“依赖节点 → 依赖它的节点”方向（即失败影响沿依赖链向下游传播），
  失败节点 `f` 能到达目标 `t` 当且仅当 `t` 传递依赖于 `f`；
- 隔离节点视为**从图中移除**：任何残余路径都不能经过它；失败节点与目标
  本身不可隔离；
- 在所有能切断**全部**失败源 → 目标路径的候选子集中，取总成本最小者；
  同成本时按**排序后的隔离节点名列表字典序**取最小（逐元素比较，因此
  `["extra", "m"]` 会排在 `["m"]` 之前——与 `/cache-retention` 的平局
  规则一致）；
- 原本就不存在失败源到目标的路径时，返回空集合与成本 `0`；
- 任何候选子集都无法阻断时（例如失败节点是目标的**直接**依赖，路径上没
  有可隔离节点），返回 `400 isolation_impossible`；
- 可隔离候选最多 **16** 个，超出返回 `400 too_many_candidates`（精确枚举
  以 2¹⁶ 为上界）；
- 请求中节点、失败集合、目标集合与成本字典的书写顺序不影响结果。

响应字段：`isolated`（需隔离的节点名，排序；原本无路径时为空表）、
`total_cost`（隔离总成本）。

### 调用示例

下图：`web` 依赖 `api`，`api` 依赖 `util` 与 `core`，`deploy` 依赖 `web`。
`core` 构建失败、`deploy` 是发布目标；`core` 经 `util → api` 与 `api` 直接
依赖两条路径影响 `deploy`：

```bash
curl -s -X POST http://localhost:8000/failure-isolation \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core",   "content_digest": "core-src-v1",   "dependency_names": []},
      {"name": "util",   "content_digest": "util-src-v1",   "dependency_names": ["core"]},
      {"name": "api",    "content_digest": "api-src-v1",    "dependency_names": ["util", "core"]},
      {"name": "web",    "content_digest": "web-src-v1",    "dependency_names": ["api"]},
      {"name": "deploy", "content_digest": "deploy-src-v1", "dependency_names": ["web"]}
    ],
    "failed_nodes": ["core"],
    "targets": ["deploy"],
    "isolation_costs": {"util": 2, "api": 5, "web": 6}
  }'
```

响应（`200 OK`）：

```json
{
  "isolated": ["api"],
  "total_cost": 5
}
```

解读：

- 只隔离 `util`（成本 2）不够——`core → api → web → deploy` 的直接依赖
  路径仍然连通；
- 隔离 `api`（成本 5）同时切断两条路径，为最低成本方案；隔离 `web`
  （成本 6）也可行但更贵；
- 若 `core` 与 `deploy` 本不连通，则返回 `{"isolated": [], "total_cost": 0}`。

无法阻断示例（失败节点是目标的直接依赖，HTTP `400`）：

```bash
curl -s -X POST http://localhost:8000/failure-isolation \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "lib", "content_digest": "lib-v1", "dependency_names": []},
      {"name": "app", "content_digest": "app-v1", "dependency_names": ["lib"]}
    ],
    "failed_nodes": ["lib"],
    "targets": ["app"],
    "isolation_costs": {}
  }'
```

```json
{
  "code": "isolation_impossible",
  "message": "No subset of the isolatable nodes cuts every failed-node to target path; failed nodes and targets themselves cannot be isolated",
  "details": {"failed_nodes": ["lib"], "targets": ["app"]}
}
```

其他错误码（均为 HTTP `400`）：`duplicate_failed_node` / `duplicate_target`
（失败/目标列表内重名）、`unknown_failed_node` / `unknown_target` /
`unknown_candidate`（引用了图中不存在的节点）、`failed_target_overlap`
（失败节点与目标重叠）、`invalid_candidate`（把失败节点或目标列为可隔离
节点）、`too_many_candidates`（候选超过 16 个）；图本身的
`duplicate_node`、`missing_dependency`、`cycle_detected` 校验与 `/plans`
一致；失败/目标列表为空、成本为负、未知字段等由 Pydantic 拒绝
（HTTP `422`）。

## POST /release-bundles：依赖一致的发布制品包

发布前，`POST /release-bundles` 在候选制品中**精确**选出一个自洽的发布包：
目标节点及其**全部传递依赖**每个节点恰好选一个制品（共享依赖只计一次），
且所选制品之间的依赖引用必须互相落在对方声明的可接受制品 id 列表内。请求
包含：

- `nodes`：节点图（字段与 `/plans` 完全相同）；
- `target`：发布目标节点名（必须在图中）；
- `artifacts`：候选制品列表，**最多 16 个**，每项含：
  - `id`：**非空且全局唯一**的制品 id；
  - `node`：制品所属节点名（必须在图中）；
  - `fingerprint`：制品构建时记录的指纹；
  - `size`：**正整数**制品大小；
  - `dependencies`：该节点的**直接依赖节点名 → 可接受制品 id 的非空列表**
    的映射（键必须与节点的直接依赖**完全一致**，不多不少）。

规则：

- 按 `/plans` 的指纹规则从当前图重算指纹；制品 `fingerprint` 与其节点当前
  指纹**不一致即过期**，过期制品不可选；
- 发布包只覆盖目标及其传递依赖闭包中的节点，闭包外节点（如与目标不连通的
  `docs`）即使有匹配制品也不进包；闭包内每个节点**恰好**选择一个制品；
- 对每个入选制品，其每条直接依赖边最终选定的依赖制品 id，必须出现在该制品
  `dependencies[依赖节点名]` 的可接受 id 列表中；
- 在所有可行发布包中取**总大小最小**者；总大小相同时，按**排序后的制品 id
  列表字典序**取最小（逐元素比较，与 `/cache-retention`、`/failure-isolation`
  的平局规则一致）；
- 不存在可行发布包时（闭包缺制品、只有过期制品、可接受 id 无法互相满足等），
  返回 `ready=false`、空 `artifact_ids`、`total_size` 为 0；
- 请求中节点与制品的书写顺序、依赖映射键的书写顺序、可接受 id 列表内的顺序
  均不影响结果。

错误（均为 HTTP `400`）：

- `too_many_artifacts`：候选制品超过 16 个；
- `unknown_target`：目标不在图中；
- `duplicate_artifact_id`：制品 `id` 重复；
- `unknown_artifact_node`：制品 `node` 引用图中不存在的节点；
- `invalid_dependency_map`：制品的 `dependencies` 键集合与该节点的直接依赖
  不符（`details.artifacts` 按 id 排序列出每个问题制品的 `missing` /
  `unexpected` 键）；
- `unknown_artifact_reference`：可接受列表引用了未提供的制品 id；
- `invalid_artifact_reference`：可接受 id 对应的制品属于**别的节点**而非
  映射的依赖名；
- 图本身的 `duplicate_node`、`missing_dependency`、`cycle_detected` 校验与
  `/plans` 一致。

制品大小非正、`id`/`node`/`fingerprint` 为空、可接受列表为空、未知字段等
由 Pydantic 拒绝（HTTP `422`）。

### 调用示例

沿用上面的示例图：`web` 依赖 `api`，`api` 依赖 `util` 与 `core`。`core`、
`util`、`api` 各提供一大一小两个自洽版本，`web` 的制品同时接受两个 `api`
版本：

```bash
curl -s -X POST http://localhost:8000/release-bundles \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core", "content_digest": "core-src-v1", "dependency_names": []},
      {"name": "util", "content_digest": "util-src-v1", "dependency_names": ["core"]},
      {"name": "api",  "content_digest": "api-src-v1",  "dependency_names": ["util", "core"]},
      {"name": "web",  "content_digest": "web-src-v1",  "dependency_names": ["api"]},
      {"name": "docs", "content_digest": "docs-src-v1", "dependency_names": []}
    ],
    "target": "web",
    "artifacts": [
      {"id": "core-big",   "node": "core", "fingerprint": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714", "size": 4, "dependencies": {}},
      {"id": "core-small", "node": "core", "fingerprint": "b53b764e740673cd4449f872a6cdea0998cbd5d92d2d706820ec010a56e0a714", "size": 2, "dependencies": {}},
      {"id": "util-big",   "node": "util", "fingerprint": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8", "size": 6, "dependencies": {"core": ["core-big"]}},
      {"id": "util-small", "node": "util", "fingerprint": "30c69063e340908c43750e92d696cc9410c09814fef4510c05829ceadc7ca3b8", "size": 3, "dependencies": {"core": ["core-small"]}},
      {"id": "api-big",    "node": "api",  "fingerprint": "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848", "size": 10, "dependencies": {"core": ["core-big"], "util": ["util-big"]}},
      {"id": "api-small",  "node": "api",  "fingerprint": "0501f6ff8631fc8fe4f9b797663e2d211d1b14e190d6f172dc46ad6f9f5a8848", "size": 7,  "dependencies": {"core": ["core-small"], "util": ["util-small"]}},
      {"id": "web1",       "node": "web",  "fingerprint": "bba914464b9203d19c8f99136f433911d07b8a1e59f0e3d34f08797a8062c338", "size": 5,  "dependencies": {"api": ["api-big", "api-small"]}}
    ]
  }'
```

响应（`200 OK`）：

```json
{
  "ready": true,
  "artifact_ids": ["api-small", "core-small", "util-small", "web1"],
  "total_size": 17
}
```

解读：

- 大版本链 `core-big + util-big + api-big + web1` 大小 25；小版本链
  `core-small + util-small + api-small + web1` 大小 17，故选择后者；
- `docs` 不在 `web` 的依赖闭包内，即使提供其制品也不会进包，`core` 经
  `util` 与 `api` 的直接依赖被两条路径共享，只计一次；
- 同大小的多个可行包按排序后的 id 列表字典序取舍，例如
  `["a0", "b1"]` 排在 `["a1", "b2"]` 之前。

无解示例（`core` 只有指纹过期的制品，HTTP `200` 但 `ready=false`）：

```json
{
  "ready": false,
  "artifact_ids": [],
  "total_size": 0
}
```

制品引用映射错误示例（可接受 id 指向了别的节点的制品，HTTP `400`）：

```json
{
  "code": "invalid_artifact_reference",
  "message": "An acceptable artifact id belongs to a different node than the dependency it is listed under",
  "details": {
    "references": [
      {"artifact": "util1", "dependency": "core", "artifact_id": "web1"}
    ]
  }
}
```

## POST /release-transitions：发布包制品切换规划

发布过程中需要把一个已经自洽的发布包逐节点切换成另一个自洽的发布包。
`POST /release-transitions` 沿用 `/release-bundles` 的 `nodes`、`target` 与
`artifacts` 字段（制品结构完全相同：`id`、`node`、`fingerprint`、`size`、
`dependencies`），另给两个端点包：

- `current_ids`：**当前包**选用的制品 id 列表；
- `next_ids`：**目标包**选用的制品 id 列表。

与 `/release-bundles` 不同的是：本接口中 `fingerprint` 与 `size` **只作元数据**，
不与当前图重算指纹比较，任何非空指纹都可以参与切换规划。

闭包与制品供给规则：

- 目标节点及其传递依赖闭包**最多 8 个节点**（`closure_too_large`）；
- 闭包内每个节点恰好供给 **1～3 件**制品（`invalid_artifact_count`）；
- 每件制品都**必须属于闭包内节点**，不允许为闭包外节点供给制品
  （`artifact_outside_closure`）；
- 制品 `dependencies` 的键必须与该节点的直接依赖完全一致，可接受 id 必须存在且
  属于所映射的依赖节点（校验规则与 `/release-bundles` 相同）。

端点包规则（`current_ids` / `next_ids`）：

- 两个包都必须为闭包内**每个节点恰选一件**制品：不可缺节点、不可两件制品属于
  同一节点、不可引用未提供的 id、列表内 id 不可重复；
- 对包内每件制品，其每条直接依赖边最终选定的依赖制品 id 必须出现在该制品
  `dependencies[依赖节点名]` 的可接受列表中；
- 任一端点包不满足上述条件即返回 `400 invalid_endpoint_bundle`
  （`details.endpoint` 为 `"current"` 或 `"next"`，先校验 current）。

切换规则：

- **每一步只替换一个闭包节点**当前选用的制品（换成该节点供给的另一件制品）；
- 允许使用**过渡制品**（目标包之外的制品只在中间步骤出现），也允许**回退**
  （同一节点在序列中多次切换）；
- 每个**中间包**仍须依赖一致：某件制品若不接受当前选定的依赖 id，则该切换
  不能发生；
- **精确求最少切换步骤**（对至多 3^8 个包状态做广度优先搜索）；步骤数相同时，
  按整条 `(node, id)` 序列**字典序**取最小（先比节点名，再比制品 id）；
- 两包相同：`possible=true`、`steps` 为空表；
- 不存在始终自洽的切换序列时：`possible=false`、`steps` 为空表；
- 请求中节点、制品以及两个 id 列表的书写顺序均不影响结果。

响应字段：`possible`（是否存在可行切换序列）、`steps`（每步含 `node`——本步
替换制品的闭包节点；`id`——切换后该节点选用的制品 id；`bundle`——**切换之后**
整个闭包的“节点名 → 制品 id”完整映射，每节点恰一件）。

错误（均为 HTTP `400`）：`unknown_target`（目标不在图中）、
`closure_too_large`（闭包超过 8 个节点）、`duplicate_artifact_id`（制品 id
重复）、`unknown_artifact_node`（制品引用图外节点）、
`artifact_outside_closure`（制品属于闭包外节点）、`invalid_artifact_count`
（闭包节点供给制品数不在 1～3 内）、`invalid_dependency_map`（依赖映射键与
节点直接依赖不符）、`unknown_artifact_reference`（可接受列表引用未提供的
id）、`invalid_artifact_reference`（可接受 id 属于别的节点）、
`invalid_endpoint_bundle`（端点包缺节点/多节点/重复 id/引用未知 id/依赖不
一致）；图本身的 `duplicate_node`、`missing_dependency`、`cycle_detected`
校验与 `/plans` 一致。两个 id 列表为空、含空字符串、未知字段等由 Pydantic
拒绝（HTTP `422`）。

### 调用示例

两个节点：`y` 依赖 `x`。当前包为 `{x: x1, y: y1}`（`y1` 只接受 `x1`），目标包
为 `{x: x2, y: y2}`（`y2` 只接受 `x2`）。`x` 不能先换（`y1` 不接受 `x2`），
必须先把 `y` 换到同时接受两种 `x` 的过渡制品 `yt`：

```bash
curl -s -X POST http://localhost:8000/release-transitions \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "x", "content_digest": "x", "dependency_names": []},
      {"name": "y", "content_digest": "y", "dependency_names": ["x"]}
    ],
    "target": "y",
    "artifacts": [
      {"id": "x1", "node": "x", "fingerprint": "f-x1", "size": 1, "dependencies": {}},
      {"id": "x2", "node": "x", "fingerprint": "f-x2", "size": 1, "dependencies": {}},
      {"id": "y1", "node": "y", "fingerprint": "f-y1", "size": 1, "dependencies": {"x": ["x1"]}},
      {"id": "yt", "node": "y", "fingerprint": "f-yt", "size": 1, "dependencies": {"x": ["x1", "x2"]}},
      {"id": "y2", "node": "y", "fingerprint": "f-y2", "size": 1, "dependencies": {"x": ["x2"]}}
    ],
    "current_ids": ["x1", "y1"],
    "next_ids": ["y2", "x2"]
  }'
```

响应（`200 OK`；注意 `next_ids` 的顺序不影响结果）：

```json
{
  "possible": true,
  "steps": [
    {"node": "y", "id": "yt", "bundle": {"x": "x1", "y": "yt"}},
    {"node": "x", "id": "x2", "bundle": {"x": "x2", "y": "yt"}},
    {"node": "y", "id": "y2", "bundle": {"x": "x2", "y": "y2"}}
  ]
}
```

每步的 `bundle` 给出该步替换之后全闭包的节点→制品映射，且每一步都依赖一致：
`(x1,y1) → (x1,yt) → (x2,yt) → (x2,y2)`，共 3 步（最少）。

无切换序列示例（没有过渡制品，`y1` 只接受 `x1`、`y2` 只接受 `x2`，两个自洽包
互不连通，HTTP `200`）：

```json
{
  "possible": false,
  "steps": []
}
```

两包相同时无需任何切换：

```json
{
  "possible": true,
  "steps": []
}
```

端点包非法示例（当前包缺少闭包节点 `x`，HTTP `400`）：

```json
{
  "code": "invalid_endpoint_bundle",
  "message": "The current_ids bundle must choose exactly one artifact for every closure node",
  "details": {"endpoint": "current", "missing_nodes": ["x"]}
}
```

闭包超限示例（闭包 9 个节点，HTTP `400`）：

```json
{
  "code": "closure_too_large",
  "message": "The target closure has 9 nodes; at most 8 are supported",
  "details": {"nodes": 9, "limit": 8}
}
```

## POST /release-policies：组织条款发布判定

发布前，`POST /release-policies` 按组织自定义条款判定目标节点能否发布。请求
包含：

- `nodes`：节点图（字段与 `/plans` 完全相同）；
- `target`：发布目标节点名（必须在图中）；
- `edge_types`：**节点名 → 直接依赖名 → 边类型** 的映射，边类型为
  `bundled`（随发布打包）或 `build_only`（仅构建期需要，不打包）；每个节点
  都必须出现，且其映射键必须与该节点的直接依赖**逐一对应**，不多不少；
- `labels`：节点名 → 该节点携带的组织条款标签列表（可为空表，缺省为空）；
- `fulfilled`：组织已履行的标签列表（缺省为空）；
- `exclusive_pairs`：互斥标签对列表，每对恰好两个不同标签（缺省为空）。

规则：

- 仅沿 `bundled` 边取目标的**传递闭包**（目标自身总在闭包内）；`build_only`
  依赖及其下游只在构建期需要、不随发布打包，其条款标签不参与判定；
- 闭包内节点携带的每个条款标签都必须已履行——**出现但未履行**的标签阻止
  发布，记入 `missing`；
- 互斥标签对的两个标签**同时出现**在闭包内（即使在同一节点上、即使都已
  履行）即构成冲突，阻止发布，记入 `conflicts`；
- `missing` 的每一项与 `conflicts` 每一对的**两端**都标出从目标到标签来源
  节点的**最短依赖路径**（沿 `bundled` 边）；等长路径按节点名序列的字典序
  取最小；标签由目标自身携带时路径只含目标一个节点；
- `allowed` 当且仅当 `missing` 与 `conflicts` 均为空；
- `missing` 按标签名排序，`conflicts` 按标签对排序；请求中节点、边类型、
  标签、已履行列表与互斥对的书写顺序均不影响结果。

错误（均为 HTTP `400`）：

- `unknown_target`：目标不在图中；
- `invalid_edge_types`：边类型映射与直接依赖未逐一对应（`details` 按
  `missing_nodes` / `unexpected_nodes` / `edges` 分别列出缺失节点、未知节点
  与每个节点的缺失/多余边）；
- `unknown_label_node`：`labels` 引用了图中不存在的节点；
- `duplicate_label`：同一节点的标签列表内出现重复标签；
- `duplicate_fulfilled`：已履行列表内出现重复标签；
- `invalid_exclusive_pair`：互斥对的两个标签相同（`self_pairs`）或同一对
  被重复给出（`duplicate_pairs`，与书写顺序无关）；
- 图本身的 `duplicate_node`、`missing_dependency`、`cycle_detected` 校验与
  `/plans` 一致。

边类型取值非 `bundled` / `build_only`、标签为空字符串、互斥对长度不为 2、
未知字段等由 Pydantic 拒绝（HTTP `422`）。

### 调用示例

下图：`web` 依赖 `api`，`api` 依赖 `util`、`core` 与 `testkit`，`util` 依赖
`core`。其中 `api → testkit` 与 `util → core` 为 `build_only` 边，其余为
`bundled` 边；因此 `web` 的打包闭包为 `{web, api, util, core}`，`testkit`
及其 `export-control` 标签不参与判定。`api` 的 `privacy-review` 已履行，
`util` 的 `gpl-3.0` 未履行，且 `gpl-3.0` 与 `core` 携带的 `proprietary`
互斥：

```bash
curl -s -X POST http://localhost:8000/release-policies \
  -H 'Content-Type: application/json' \
  -d '{
    "nodes": [
      {"name": "core",    "content_digest": "core-src-v1",    "dependency_names": []},
      {"name": "util",    "content_digest": "util-src-v1",    "dependency_names": ["core"]},
      {"name": "api",     "content_digest": "api-src-v1",     "dependency_names": ["util", "core", "testkit"]},
      {"name": "web",     "content_digest": "web-src-v1",     "dependency_names": ["api"]},
      {"name": "testkit", "content_digest": "testkit-src-v1", "dependency_names": []}
    ],
    "target": "web",
    "edge_types": {
      "web":     {"api": "bundled"},
      "api":     {"util": "bundled", "core": "bundled", "testkit": "build_only"},
      "util":    {"core": "build_only"},
      "core":    {},
      "testkit": {}
    },
    "labels": {
      "api":     ["privacy-review"],
      "util":    ["gpl-3.0"],
      "core":    ["proprietary"],
      "testkit": ["export-control"]
    },
    "fulfilled": ["privacy-review", "proprietary"],
    "exclusive_pairs": [["gpl-3.0", "proprietary"]]
  }'
```

响应（`200 OK`）：

```json
{
  "allowed": false,
  "missing": [
    {"tag": "gpl-3.0", "path": ["web", "api", "util"]}
  ],
  "conflicts": [
    {
      "pair": ["gpl-3.0", "proprietary"],
      "paths": {
        "gpl-3.0": ["web", "api", "util"],
        "proprietary": ["web", "api", "core"]
      }
    }
  ]
}
```

解读：

- `gpl-3.0` 出现在闭包内（`util` 携带）但未履行，阻止发布；路径
  `["web", "api", "util"]` 是从 `web` 到 `util` 的最短 bundled 路径；
- `gpl-3.0` 与 `proprietary` 同时出现在闭包内，构成冲突；冲突两端各给一条
  最短来源路径（注意 `util → core` 是 `build_only` 边，`proprietary` 的路径
  走 `api → core` 的 `bundled` 边）；
- `proprietary` 虽已履行，冲突判定与是否履行无关，仍然阻止发布；
- `testkit` 仅经 `build_only` 边可达，其 `export-control` 标签不参与判定；
- 若再履行 `gpl-3.0` 并去掉该互斥对，则响应为
  `{"allowed": true, "missing": [], "conflicts": []}`。

边类型未逐一对应示例（`api` 的映射缺少直接依赖 `util`，HTTP `400`）：

```json
{
  "code": "invalid_edge_types",
  "message": "Edge types must list exactly the direct dependencies of every node",
  "details": {"edges": [{"node": "api", "missing": ["util"]}]}
}
```

## 指纹与批次的计算规则

**指纹**（按拓扑序计算）：

```
fingerprint(n) = SHA-256( canonical_json([
    n.content_digest,
    [[dep, fingerprint(dep)] for dep in sorted(n.dependency_names)]
]) )
```

- canonical JSON 即 `json.dumps(..., sort_keys=True, separators=(",",":"))`
  的 UTF-8 字节，结果为 64 位十六进制字符串；
- 直接依赖**按名称排序**，所以依赖列表的书写顺序变化不会改变指纹；
- 依赖指纹一律纳入（命中与否都纳入），因此任何上游变更都会沿依赖链传播；
- `content_digest` 是调用方自管的内容摘要，服务把它当作不透明字符串处理。

**缓存判定**：`fingerprint(n) == previous_fingerprints.get(n)` 即命中；
`previous_fingerprints` 中多余的键（图中不存在的节点名）会被忽略。

**批次级别**（只在未命中节点之间计算，沿拓扑序迭代，无递归深度限制）：

```
level(n) = 0                                          # 所有依赖都命中（或无依赖）
level(n) = 1 + max(level(d))  for d in deps(n) if d 未命中
```

同一 `level` 的节点构成一个并行批次，批次内按名称排序。

## 确定性

拓扑排序（Kahn 算法 + 最小名称堆）、依赖列表、每批节点、响应字典全部按名称
排序；多次以任意顺序提交同一组节点与依赖，响应字节级一致（测试中以 10 组
随机排列校验）。

## API 摘要

| 方法   | 路径                 | 说明                                       |
|--------|----------------------|--------------------------------------------|
| GET    | `/health`            | 健康检查                                   |
| POST   | `/plans`             | 提交图与历史指纹，返回指纹、命中情况与批次 |
| POST   | `/impacts`           | 对比 before/after 两图，返回变更影响分析   |
| POST   | `/cache-retention`   | 容量限制下挑选保留制品，最小化目标重建代价 |
| POST   | `/execution-audits`  | 按 seq 回放执行事件流，核查合法性并给出终态 |
| POST   | `/failure-isolation` | 挑选最小成本隔离节点，阻断失败源到发布目标 |
| POST   | `/release-bundles`    | 选择总大小最小的依赖一致发布制品包         |
| POST   | `/release-transitions`| 规划两包间最少步骤的逐节点制品切换         |
| POST   | `/release-policies`   | 沿 bundled 边闭包判定组织条款能否发布      |

`/plans` 请求体字段：

| 字段                     | 类型                  | 说明                       |
|--------------------------|-----------------------|----------------------------|
| `nodes[].name`           | string（非空，唯一）  | 节点名称                   |
| `nodes[].content_digest` | string（非空）        | 节点自身内容摘要           |
| `nodes[].dependency_names` | string[]            | 直接依赖名（不可重复）     |
| `previous_fingerprints`  | object<string,string> | 上次构建的“节点名 → 指纹”  |

`/impacts` 请求体字段：

| 字段     | 类型     | 说明                                   |
|----------|----------|----------------------------------------|
| `before` | Node[]   | 旧图节点（字段同 `nodes[]`，可为空表） |
| `after`  | Node[]   | 新图节点（字段同 `nodes[]`，可为空表） |

`/cache-retention` 请求体字段：

| 字段                      | 类型                 | 说明                                 |
|---------------------------|----------------------|--------------------------------------|
| `nodes`                   | Node[]               | 节点图（字段同 `/plans`）            |
| `target`                  | string（非空）       | 目标节点，必须在图中                 |
| `rebuild_costs`           | object<string,int≥0> | 每个节点的重建代价（须恰好覆盖全图） |
| `artifacts`               | Artifact[]           | 已有制品，可为空表                   |
| `artifacts[].name`        | string（非空，唯一） | 制品所属节点名，必须在图中           |
| `artifacts[].fingerprint` | string（非空）       | 制品构建时记录的指纹                 |
| `artifacts[].size`        | int（>0）            | 制品占用空间                         |
| `capacity`                | int（≥0）            | 保留制品的总容量上限                 |

`/execution-audits` 请求体字段：

| 字段                     | 类型                  | 说明                                |
|--------------------------|-----------------------|-------------------------------------|
| `nodes`                  | Node[]                | 节点图（字段同 `/plans`）           |
| `previous_fingerprints`  | object<string,string> | 上次构建的“节点名 → 指纹”           |
| `events`                 | Event[]               | 执行事件列表，可为空表              |
| `events[].seq`           | int（≥0，唯一）       | 回放顺序号                          |
| `events[].node`          | string（非空）        | 事件所属节点，必须在图中            |
| `events[].type`          | string                | `start` / `success` / `failure`     |

`/failure-isolation` 请求体字段：

| 字段              | 类型                  | 说明                                     |
|-------------------|-----------------------|------------------------------------------|
| `nodes`           | Node[]                | 节点图（字段同 `/plans`）                |
| `failed_nodes`    | string[]（非空，唯一）| 失败构建节点名，必须在图中               |
| `targets`         | string[]（非空，唯一）| 发布目标节点名，必须在图中且不与失败重叠 |
| `isolation_costs` | object<string,int≥0>  | 可隔离节点名 → 隔离成本（最多 16 个）    |

`/release-bundles` 请求体字段：

| 字段                                  | 类型                         | 说明                                           |
|---------------------------------------|------------------------------|------------------------------------------------|
| `nodes`                               | Node[]                       | 节点图（字段同 `/plans`）                      |
| `target`                              | string（非空）               | 发布目标节点，必须在图中                       |
| `artifacts`                           | BundleArtifact[]（≤16）      | 候选制品列表，指纹匹配当前图者才可选           |
| `artifacts[].id`                      | string（非空，唯一）         | 制品 id                                        |
| `artifacts[].node`                    | string（非空）               | 制品所属节点名，必须在图中                     |
| `artifacts[].fingerprint`             | string（非空）               | 制品构建时记录的指纹                           |
| `artifacts[].size`                    | int（>0）                    | 制品大小                                       |
| `artifacts[].dependencies`            | object<string,string[]>      | 直接依赖节点名 → 可接受制品 id 的非空列表      |

响应字段：`ready`（是否存在可行发布包）、`artifact_ids`（入选制品 id，
排序；无解时为空表）、`total_size`（入选制品大小之和；无解时为 0）。

`/release-transitions` 请求体字段：

| 字段                                  | 类型                         | 说明                                           |
|---------------------------------------|------------------------------|------------------------------------------------|
| `nodes`                               | Node[]                       | 节点图（字段同 `/plans`）                      |
| `target`                              | string（非空）               | 发布目标节点，必须在图中                       |
| `artifacts`                           | BundleArtifact[]             | 候选制品（字段同 `/release-bundles`）；每件须属于闭包节点，闭包节点各 1～3 件；指纹仅作元数据 |
| `current_ids`                         | string[]（非空）             | 当前包选用的制品 id，每闭包节点恰一件          |
| `next_ids`                            | string[]（非空）             | 目标包选用的制品 id，每闭包节点恰一件          |

响应字段：`possible`（是否存在始终自洽的切换序列）、`steps`（最少切换步骤，
同长按 `(node, id)` 序列字典序取最小；每步含 `node`、`id` 与切换后全闭包的
节点→制品 id 映射 `bundle`；两包相同或无解时为空表）。

`/release-policies` 请求体字段：

| 字段                     | 类型                                | 说明                                                |
|--------------------------|-------------------------------------|-----------------------------------------------------|
| `nodes`                  | Node[]                              | 节点图（字段同 `/plans`）                           |
| `target`                 | string（非空）                      | 发布目标节点，必须在图中                            |
| `edge_types`             | object<string,object<string,string>>| 节点名 → 直接依赖名 → `bundled` / `build_only`，须与直接依赖逐一对应 |
| `labels`                 | object<string,string[]>             | 节点名 → 条款标签（节点须在图中，标签不可重复），可缺省 |
| `fulfilled`              | string[]                            | 已履行的标签（不可重复），可缺省                    |
| `exclusive_pairs`        | string[2][]                         | 互斥标签对（每对两个不同标签，不可重复），可缺省    |

响应字段：`allowed`（无缺失标签且无冲突时为 `true`）、`missing`（未履行
标签列表，按标签排序，每项含 `tag` 与从目标到标签来源的最短 bundled 路径
`path`）、`conflicts`（冲突列表，按标签对排序，每项含排序后的 `pair` 与
两端各自的最短来源路径 `paths`）。

## 本地开发与测试（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000
pytest -q
```

测试共 287 项，覆盖指纹公式、缓存传播、批次划分、并行/不等待命中依赖、
缺失依赖、环（含自环）、重名、顺序无关性、两图对比的根因识别、最短传导
路径与字典序取舍、双图分别校验、缓存保留的最优选择与字典序平局取舍、
候选上限与各类引用错误、执行事件回放的合法性与违规忽略、失败节点的
blocked 传递、重复 seq 与未知节点错误、失败隔离的最小成本选择、字典序
平局取舍、空路径空集合、不可阻断与各类校验错误、发布制品包的最小总大小
选择与 id 列表字典序平局、共享依赖只计一次、闭包外制品排除、过期指纹与
依赖不一致导致的无解、发布包切换的最少步骤与 `(node, id)` 序列字典序
平局取舍、过渡制品与节点多次切换（回退）、中间包依赖一致、两包相同与
无解、闭包节点数与每节点制品数上限、闭包外制品与非法端点包等各类校验
错误、发布条款的 bundled 闭包判定、build_only 边截断、缺失标签与互斥
冲突的最短来源路径及字典序取舍、边类型逐一对应校验、重复标签与无效
互斥对错误，以及全部 HTTP 状态码。
