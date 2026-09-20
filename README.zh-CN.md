# GeoNames 主要城市数据生成器

这是一个可重复构建的地点数据流水线，把固定版本的 GeoNames 数据整理成适合产品下拉选择的三级结构：

```text
国家 → 一级行政区 → 主要城市
```

它会输出压缩 CSV、SQLite 数据库、质量报告，以及一个可以直接双击打开的多语言完整数据浏览页。构建过程只依赖 Python 标准库。

[打开仓库内的轻量展示页](docs/index.html)

## 快速使用

需要 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

geonames-major-cities all \
  --manifest manifests/geonames-2026-09-17.json \
  --data-dir data \
  --output-dir output
```

最大的固定源文件约 195 MiB。下载完成后会缓存；再次执行时会先校验文件大小和 SHA-256。

构建后直接双击 `output/review.html` 即可浏览全部国家、一级行政区和城市，不需要启动本地服务，也不会访问网络。

固定的 2026-09-17 数据已完成全量验证：250 个国家、3,865 个一级行政区、31,805 个城市；包含 526 个语言标签下的 543,811 条本地化名称；中国 293 个地级市全部匹配成功；移除了 71 个同父级重名城市选项；最终没有未解决的质量问题。

## 数据选择规则

一般国家保留以下地点：

- 国家首都 `PPLC`；
- 一级行政区首府 `PPLA`；
- 二级行政区首府 `PPLA2`；
- 人口不少于 50,000 的普通城市 `PPL`。

中国单独使用固定版本的地级市清单：只读取四位行政区划代码，匹配回 GeoNames 地点；区、县、县级市不进入城市下拉框。北京、上海、天津、重庆作为末级一级行政区，不再展开其下辖区县。

这里的“主要城市”是为产品选择器制定的确定性规则，并不声称是全球统一的法律定义。例如 Brändö 虽然人口很少，但它是 `PPLA2` 行政中心，所以会被保留。

默认 `name` 优先使用 GeoNames 明确提供的英文别名；没有英文别名时保留 GeoNames 主名称。因此这是“英文优先”，不是“只允许 ASCII 字符”，带重音符号的正式地名是正常数据。

同时会导出数据源中全部有效语言名称。历史名、俗称和简称不作为展示名。同一语言存在多个名称时，优先选择 GeoNames 标记的推荐名称，否则稳定选择最早记录。客户端按以下顺序回退：

```text
精确地区语言 → 基础语言 → 英文 → GeoNames 主名称
zh-CN        → zh       → en   → name
```

没有翻译就回退，不使用机器翻译补齐。当前快照中，俄语直接覆盖 19,667 个地点、英语 15,796 个、日语 11,887 个、通用中文 9,971 个。

## 质量保证

构建会检查：

- 同一父级下归一化后重名的城市；
- 找不到父节点的孤儿记录；
- 缺少经纬度的城市；
- 中国地级市清单中无法匹配 GeoNames 的记录；
- 固定的必选/禁选回归样例。

任何检查失败，`qualityStatus` 都会变成 `REVIEW_REQUIRED`，命令返回非零状态，不应直接发布数据。

SQLite 中自带便于浏览和校验的视图：

```sql
SELECT * FROM city_flat;
SELECT * FROM duplicate_check; -- 正常应为 0 行
SELECT * FROM orphan_check;    -- 正常应为 0 行
SELECT * FROM localized_city_names WHERE language_code = 'zh';
SELECT * FROM language_coverage LIMIT 20;
```

城市保留 GeoNames 经纬度；国家和一级行政区不擅自推导坐标。

## 更新方式

上游下载地址可能变化，但每个清单都固定了文件大小和 SHA-256，所以历史构建不会被悄悄替换。更新数据时应新增带日期的 manifest，不要覆盖旧清单；然后完整构建、比较数量变化并人工浏览抽查。

## 补充来源覆盖审计

可以在不改动 GeoNames 正式输出的前提下，审计 Wikidata、Who's On First 和 Overture 能补充多少多语言名称。整个过程只使用 GeoNames ID 和 Wikidata QID 精确关联，不使用名称模糊匹配、机器翻译或大模型判断。

```bash
python -m pip install -e '.[audit]'

geonames-major-cities audit-sources \
  --dataset output/locations.sqlite3 \
  --geonames-alternate-names data/alternateNamesV2.zip \
  --cache-dir audit-cache \
  --output-dir audit-output
```

脚本只投影 Parquet 的必要字段，并通过精确 ID 与当前数据关联。Who's On First 保持远程读取；当前 Overture division release 是一个约 550 MiB 的 Parquet 文件，为了稳定读取嵌套多语言字段，脚本会临时下载这个固定版本文件，缓存命中行后立即删除原始文件。最终生成 `source-audit.json` 和中文 `source-audit.md`。使用 `--refresh` 可强制刷新上游快照。报告会列出覆盖率、冲突、层级兼容性、数据溯源和许可证边界，但不会把候选名称写入正式数据。

## 许可证

本仓库代码使用 MIT License。生成数据包含 GeoNames 等上游数据，重新分发时仍需遵守上游许可证和署名要求。详见 [DATA_SOURCES.md](DATA_SOURCES.md)。
