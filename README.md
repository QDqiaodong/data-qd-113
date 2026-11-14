# data-qd-113 · 栅格地形汇流分析

Strict-descent D8 drainage accumulation and catchment delineation。数据流程与完整口径见 [docs/contract.md](docs/contract.md)。

## 安装

Python >=3.11，运行和测试仅使用标准库。先确认 `python3 --version`。
每个项目独立安装，不复用其他仓的虚拟环境或全局包：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
```

已具备相同版本的公共 wheel 缓存时，可用 `--no-index --find-links <wheel目录>` 安装构建工具；
否则首次安装可能联网。实际业务运行与测试无需网络、账号、Docker 或外部服务。

## 演示与测试

在仓库根目录执行；输出为可独立读取的 JSON，重跑全量重算、不累加。

```sh
.venv/bin/terrain-flow --source fixtures/terrain.json --output run/report.json
.venv/bin/terrain-flow --source fixtures/terrain.json --output run/report.json --stream-threshold 3
.venv/bin/python -m unittest discover -s tests -v
```

`--stream-threshold N`（正整数，可选）在原报告追加`stream_network`；不带该参数时
输出不变。河网只保留`upstream_cells >= N`的有效格并沿既有D8边连接，节点与极大
有向链的编号只由坐标决定，Strahler级别按河网内支流计算，链长以
`cell_size_m*(正交步数+对角步数*sqrt(2))`精确表达。阈值过大时返回空河网。
结构、复杂度与边界语义见 [docs/contract.md](docs/contract.md)。

输入校验或计算失败退出 2，保留原输出；JSON 通过同目录临时文件完成后替换。
不对正在被其他进程写入的输出运行；不承诺文件系统断电恢复或在线并发发布。
修改源码后需重新安装。`run/`、`.venv/` 与构建产物均不入 Git。

CLI 生产路径不使用全量逐格/河网记录列表或完整报告字符串：D8 图以扁平整数数组
保存，报告逐条记录流式写入文件并在 stdout 重放同一文档；`drain`/`run` 对象 API
保持不变。因此 10 万格、约 30 MB 的 JSON 报告也不会带来等量内存占用（见
[docs/contract.md](docs/contract.md)）。

## 批次入湖

按显式清单把多个独立 terrain JSON 逐源入湖，每源复用上面的单文件 D8 计算，
成功结果写成不可变分区，并生成可校验重建的目录清单：

```sh
.venv/bin/terrain-flow batch --manifest manifest.json --lake run/lake
.venv/bin/terrain-flow verify --lake run/lake
.venv/bin/terrain-flow rebuild-catalog --lake run/lake
```

```json
{"sources": [
  {"id": "alpha", "path": "fixtures/terrain.json"},
  {"id": "beta",  "path": "fixtures/terrain-ridge.json", "stream_threshold": 2}
]}
```

分区为 `sources/<id>/v<N>/{report.json,meta.json}`，`catalog.json` 只列已提交内容。
内容与参数均未变化的源在重跑时跳过；任一变化形成新版本且不覆盖旧分区。
单源失败被隔离并继续批次，`failed>0` 时退出 1；清单或湖完整性错误退出 2 且不写入。
报告逐字节等于对应单文件报告，不拼接栅格。报告在暂存目录里边写边算 SHA-256 与
字节数后一次性重命名提交；湖扫描对产物摘要分块读取，峰值内存与报告大小无关。
完整口径（原子提交边界、冲突与校验规则、内存界与磁盘代价）
见 [docs/contract.md](docs/contract.md)。

## 地形连通层级索引

在既有格网上构建随水位上升的合并森林，并按原顺序回答至多 10 万条
`(row,col,level)` 查询。水位 `h` 时仅高程 `<= h` 的格激活、按 8 邻域连通，`null`
为障碍且不连网格外；同高程格整批处理，新盆建叶、单分量只扩张不生节点、多分量一次
合成多叉节点，避免并列鞍部产生人为二叉链。

```sh
.venv/bin/terrain-flow connectivity \
  --grid fixtures/terrain.json --queries queries.json --output run/connectivity.json
```

```json
{"queries": [
  {"row": 0, "col": 0, "level": 4},
  {"row": 2, "col": 2, "level": -1}
]}
```

输出含按 `(事件高程, 最小坐标)` 编号的 `forest`（每节点给出 `event_level`、
`min_cell` 与 `children`）、森林根 `roots`，以及逐条查询的 `node_id`、面积与精确
蓄水体积 `Σ(h-z)×格面积`；未激活或 `null` 返回 `null`。事件与查询单趟归并、并查集
迭代实现，总时间 O((V+Q) log(V+Q))、空间 O(V+Q)，10 万格深链不递归。语义、输出字段
与复杂度见 [docs/contract.md](docs/contract.md)。

样例来源见 fixtures/DATA-SOURCES.md。代码与自制样例 MIT；没有正式题面、评测答案或内部编排代码。

## 已有能力与样例结果

12格共48m²，终点[2,2]集水36m²，终点[2,4]集水12m²，面积守恒。
`drainage.py`以精确平方坡度选D8流向、按高程累积贡献面积并划分终点归属。
不执行填洼、真实坐标投影或洪水模拟；平地保留为终点，不假设存在未观测的出流。
