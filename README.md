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

样例来源见 fixtures/DATA-SOURCES.md。代码与自制样例 MIT；没有正式题面、评测答案或内部编排代码。

## 已有能力与样例结果

12格共48m²，终点[2,2]集水36m²，终点[2,4]集水12m²，面积守恒。
`drainage.py`以精确平方坡度选D8流向、按高程累积贡献面积并划分终点归属。
不执行填洼、真实坐标投影或洪水模拟；平地保留为终点，不假设存在未观测的出流。
