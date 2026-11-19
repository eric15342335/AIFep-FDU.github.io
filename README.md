# Frontier Exploration and Practice of Artificial Intelligence (2026 Autumn)

## Project1: 复杂表格识别与内容理解

使用阿里云百炼 `qwen3.8-flash` 完成复杂表格结构恢复、内容提取和内容推理。

本仓库有两套方案：

- **Baseline**：每道题把整页图像发给模型直接作答，格式不对时追加修复请求，平台得分 71.6。用法见下文。
- **Agent 方案**：每道题在独立的 Docker 容器中运行一个 AI agent。agent 可以放大页面局部、读取 PDF 文本层、用 Python 计算，最后提交经过格式校验的答案。正式提交使用百炼 qwen3.8-flash，平台得分 78.6，比 baseline 高 7.0 分；908 道题全部作答，用时约 28 分钟，估算费用 8.61 元。用法、设计和结果见 [docs/agent-harness.md](docs/agent-harness.md)。

Agent 方案只需三步（需要 Docker 和 uv）：

```bash
cp .env.example .env          # 填入百炼 API Key 和 Base URL
uv run agent.py smoke         # 检查接口，花费不到 0.1 元
uv run agent.py run           # 回答全部 908 题，结果在 runs/agent/submission.xlsx
```

## 文件结构

```text
.
├── baseline.py                    # Baseline 主程序，固定使用 qwen3.8-flash
├── agent.py                       # Agent 方案入口：smoke（检查接口）和 run（答题）
├── agent/                         # Agent 沙箱镜像、pi 配置、LiteLLM 网关和 Phoenix
├── docs/                          # Agent 方案的设计、用法和结果
├── tableqa/                       # 预处理、提示词、API 请求、校验与恢复
├── run.sh                         # 运行入口
├── requirements.txt
├── .env.example
├── configs/downloaded_data_fixes.json
├── data/
│   ├── tests.xlsx                 # 908 道题
│   └── files/                     # 89 份实际引用文档，共 97 页
└── submission.xlsx                # 已生成的 Qwen3.8-Flash 全量预测结果
```

## 运行

使用 Python 3.11 或更高版本，在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入自己的百炼 API Key 和 compatible-mode/v1 地址
source .env

# 可先运行 3 题检查配置
bash run.sh --limit 3 --workers 1 --max-requests 10

# 全量运行，自动复用已完成的缓存
bash run.sh --workers 2 --max-requests 2000
```

每次运行的结果和缓存位于 `runs/qwen3_8_flash/`。全量完成且所有题目均生成非空答案后，程序会同步更新根目录的 `submission.xlsx`。少量试跑保留根目录已有的全量结果。

检查数据或提交文件：

```bash
python baseline.py inspect
python baseline.py validate
```

题目读取会自动应用 `configs/downloaded_data_fixes.json` 中的 3 项文件名映射和 1 条题型修正。程序支持 JSON 修复、数值提取的纯文本恢复、结构占用网格恢复和断点续跑，计算题用 `Decimal` 重算模型给出的表达式。

## submission.xlsx

根目录文件是此前 Qwen3.8-Flash 已生成的 908 题预测，包含 `id`、`answer` 两列，908 行答案均非空，并通过 ID 和格式校验。题目没有提供标准答案，该文件是模型预测结果。

赛题来源：[知乎知学堂大模型挑战赛：大模型复杂表格识别与内容理解](https://tianchi.aliyun.com/competition/entrance/532510/information)。
