# 碳核算台账标准化（Carbon Ledger Standardizer）

面向企业碳核算数据工程师的网页工具：上传**字段合同 JSON** 与各厂区**能源台账 CSV**，
在浏览器中查看字段映射、歧义与错误行；仅当台账完全合格时，才提供标准列 CSV 下载。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

打开浏览器访问 <http://localhost:8000>（健康检查：`/healthz`）。
停止：`docker compose down`。

容器以非 root 用户运行，根文件系统只读，仅开放 8000 端口。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
python3 -m pytest -q
```

## 字段合同格式

```json
{
  "fields": [
    {"name": "厂区",        "aliases": ["工厂", "site"], "type": "text",    "required": true},
    {"name": "月份",        "aliases": ["统计月份"],      "type": "month",   "required": true},
    {"name": "用电量(kWh)", "aliases": ["电量", "kwh"],   "type": "decimal", "required": true},
    {"name": "抄表次数",    "aliases": ["次数"],          "type": "integer", "required": false}
  ]
}
```

* `type` 仅限 `text`（文本）、`integer`（整数）、`decimal`（有限小数）、`month`（`YYYY-MM`）。
  也接受中文写法「文本 / 整数 / 有限小数 / 月份」。
* `name` 本身即可作为来源表头，别名无需重复标准列名。
* 表头匹配忽略大小写、全/半角宽度差异与多余空白（`Energy (kWh)` ≡ `energy（kwh）`）。
* 示例文件见 `samples/contract.json`、`samples/ledger.csv`。

## 校验与映射规则

| 场景 | 行为 |
| --- | --- |
| 源列命中别名 | 必须**一一对应**到唯一标准列 |
| 必填标准列无来源 | 拒绝，列出缺失列 |
| 存在多种完整映射 | 拒绝，列出全部候选方案（不按列顺序、票数或样例值猜测） |
| 重复表头（含规范化后重复） | 拒绝，整体不处理 |
| 必填单元格为空 | 拒绝，定位到行号/列 |
| 任一数据行列数不符 | 拒绝，定位到行号 |
| 任一数据行类型不符 | 拒绝，定位到行号/列/值 |
| 未命中别名的额外列 | 忽略，不进入结果 |
| 数据行整行留空（`,,`） | 视为数据行，必填列报空；真正的空白行忽略 |

所有校验为**全有或全无**：只要存在上述任一问题，就不生成任何部分结果或下载链接。

类型语法（严格、整串匹配，不做隐式修剪之外的转换）：

* `integer`：`[+-]?[0-9]+`，例如 `0`, `-12`, `+5`
* `decimal`：`[+-]?([0-9]+(\.[0-9]+)?|\.[0-9]+)`，不接受 `NaN`/`inf`/科学计数法/千分位逗号
* `month`：`YYYY-MM`，月份 `01`–`12`
* `text`：任意字符串（必填时空值视为错误）

映射由约束求解枚举得出：每个命中别名的源列必须分配且只分配一个标准列，每个必填标准列
必须被覆盖；若满足条件的完整分配不唯一（或候选过多无法证明唯一），一律拒绝并列出候选方案。


## 上传限制

* 单文件 ≤ 10 MiB；台账须为 UTF-8（可带 BOM）。
* 结果保存在服务器内存中，下载链接 30 分钟有效；重启即失效。
* 输出的标准列 CSV 为 UTF-8 带 BOM（便于 Excel 正确识别中文表头）。

## 项目结构

```
app/
  main.py       FastAPI 路由、上传处理、内存下载存储
  spec.py       字段合同解析与规范化
  engine.py     映射枚举、歧义检测、逐行类型校验、结果生成
  templates/    浏览器页面
  static/       样式
tests/          pytest（引擎规则 + HTTP 端到端）
samples/        可直接上传的示例
```
