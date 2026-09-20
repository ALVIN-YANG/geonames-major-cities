# 补充地名来源覆盖审计

生成时间：`2026-09-20T08:43:32+00:00`

本报告由确定性脚本生成。只使用精确的 GeoNames ID 或 Wikidata QID 关联；不使用模糊名称匹配、大模型判断或机器翻译。

## 当前基准

- 地点总数：35,920
- 已有简体中文覆盖：10,150 (28.3%)
- GeoNames 中可用的 Wikidata 精确链接：12,182，对应 12,158 个唯一 QID；其中 24 个 QID 关联多个当前地点，已单独列入 JSON 审查

## 审计结果

| 来源 | 精确匹配 | 通过来源校验 | 新名称对 | 可补中文地点 | 结论 |
|---|---:|---:|---:|---:|---|
| Wikidata | 12,182 | 12,182 | 393,771 | 4,242 | 可作为核心补充候选，但必须先审查名称冲突 |
| Who's On First | 33,121 | 32,161 | 275,126 | 11,287 | 仅用于审计；字段级许可证与来源未解决前不得合并 |
| Overture | 9,763 | 9,403 | 87,058 | 958 | 仅作为独立 ODbL 输出候选，不得混入 MIT 核心数据 |

各来源的增量不能直接相加，因为它们会覆盖同一批地点。冲突只是审计结果，本次没有写入正式数据。

## 中文覆盖推算

| 方案 | 新增中文地点 | 推算总覆盖 | 覆盖率 |
|---|---:|---:|---:|
| Wikidata | 4,242 | 14,392 | 40.1% |
| Who's On First | 11,287 | 21,437 | 59.7% |
| Overture | 958 | 11,108 | 30.9% |
| 三方唯一并集（仅技术上限） | 12,233 | 22,383 | 62.3% |

三方唯一并集只表示技术覆盖上限，不表示可以直接合并；仍必须分别满足许可证和字段溯源要求。

## 授权与后续使用边界

### Wikidata

- 许可：CC0-1.0
- 校验：GeoNames explicit wkdt link; Wikidata P31 and country hierarchy were not fetched in this name-coverage audit
- 溯源：缓存为每个实体保留 QID、修订号和修改时间
- 建议：可作为核心补充候选，但必须先审查名称冲突

### Who's On First

- 许可：mixed; repository and record sources must be evaluated
- 校验：GeoNames ID、地点类型和国家代码必须同时兼容
- 溯源：保留 WOF ID 和几何来源，但反规范化名称列没有字段级来源信息
- 建议：仅用于审计；字段级许可证与来源未解决前不得合并

### Overture

- 许可：ODbL-1.0 for the divisions theme; row sources may add CC0 fields
- 校验：Wikidata QID、地点类型和国家代码必须同时兼容
- 溯源：忽略目录中的匹配缓存保留 Overture sources 结构
- 建议：仅作为独立 ODbL 输出候选，不得混入 MIT 核心数据

## 可复现性

忽略目录 `audit-cache/` 保存源数据匹配缓存与 Wikidata 实体修订号；`--refresh` 可强制重新抓取。JSON 报告包含各来源快照信息和机器可读指标。
