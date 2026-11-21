# 复杂表格识别与内容理解：在隔离沙箱中解题的表格问答智能体

**团队成员**：郑皓名（23340240063）、黄泓力（24340240003）　**平台用户名**：Nick_lessonUSE

**代码**：https://github.com/eric15342335/AIFep-FDU.github.io （`uv run agent.py run` 一条命令复现）

## 1 方法

[Baseline](https://github.com/eric15342335/AIFep-FDU.github.io/blob/main/baseline.py) 将整页图像和题目一并发给 [qwen3.8-flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) 直接作答；若格式不对或为空，则追加一次修复请求，部分题型还会改换输出格式重试，平台得分为 71.6。

我们使用同一个模型，复用 baseline 的页面渲染、题型提示词和答案格式，并提供 PDF 文本层让模型分步解题：先看整页再放大所需区域，有文本层时直接检索原文，计算交给 Python 完成，最后提交答案并当场检查格式。

每道题都在独立的 [Docker](https://www.docker.com/) 容器（沙箱）中运行一个 [pi](https://github.com/earendil-works/pi) 智能体。沙箱无法访问外网，内部网络中只有模型网关 [LiteLLM](https://github.com/BerriAI/litellm) 一个服务；页面图像、文本层和题目以只读方式传入，答案和完整对话写入可写的输出目录。智能体有五个工具：

- `zoom`：按 0 到 1000 的相对坐标裁剪页面局部，放大后作为图像返回，以便看清小字和密集表格；
- `answer`：用 baseline 的检查函数 [`normalize_response`](https://github.com/eric15342335/AIFep-FDU.github.io/blob/main/tableqa/schema.py) 校验答案，不合格返回原因，合格则保存并结束会话；
- `bash`、`read`、`write`：用于检索文本层和运行 Python 计算。

PDF 文本层是随页面一起保存的文字和坐标，比直接从图像辨认更准确。89 份文档中有 21 份 PDF 带有文本层，但其中 3 份字体编码错误，文字全是乱码，模型读到后会反复尝试，白白消耗 token。为此，程序会逐页检查字母、数字和汉字占非空白字符的比例，低于一半就跳过该页文本层，让模型改看图像。

题目中出现的文件名一律替换为"该文档"，以免模型从文件名猜答案。

<figure class="wide"><img src="../diagrams/zh-architecture.png" alt="系统结构"><figcaption>图 1　系统结构。黄底大框：课程 baseline 代码；黄色虚线边框的步骤：调用这些代码；其余为新增部分。</figcaption></figure>

## 2 创新点

<figure class="side"><img src="../diagrams/zh-question-states.png" alt="单道题的状态机"><figcaption>图 2　单道题的状态机。绿色：会话内的步骤；黄色：baseline 答案检查；虚线：回到下一回合。</figcaption></figure>

1. **文字与图像对齐，由模型决定看哪里。** 文本层中每个词的位置都换算到 `zoom` 所用的 0 到 1000 坐标，因此模型可以先在文本里搜到目标数字或表头，再直接放大该位置核对图像。这一设计结合了课程建议的两条路线（"定位表格、裁剪并提高分辨率"和"文字识别结果加多模态模型"），并由模型按题决定步骤：简单题查看整页即可提交，复杂题则反复检索、放大并用 Python 核算。
2. **格式检查成为模型可调用的工具。** Baseline 在答案返回后才检查格式，我们则将同一个检查函数做成 `answer` 工具：模型看到拒绝原因后，可以继续放大、检索或计算，再提交改正后的答案；容器运行结束后，主机还会用该函数复核一次。
3. **出错时保留已做的工作（图 2）。** 单题求解时，答案被拒后可修改；会话首次结束仍无合格答案时，会在同一会话内提醒一次；用满 25 轮后追加一轮只能提交答案的回合，超时 30 分钟则强制停止。整批运行时，无答案的题会自动重跑一遍，连续 8 题失败则不再开启新题；运行中断后重新执行同一命令，已保存的合格答案会直接复用，不会重复计费。
4. **工程保障。** API Key 仅保存在网关容器中，沙箱无法获取；网关将模型调用上报到 [Phoenix](https://github.com/Arize-ai/phoenix)（用时和 token 数，消息内容可选），每道题也会另存完整对话。此外，为让百炼的[隐式上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)生效，对话只在末尾追加、不改写前文：我们关闭了上下文压缩，并开启 [`preserve_thinking`](https://help.aliyun.com/zh/model-studio/deep-thinking) 保留之前的推理。

## 3 实验设置

模型采用百炼北京地域的 qwen3.8-flash，开启[思考模式](https://help.aliyun.com/zh/model-studio/deep-thinking)，将推理强度设为最高档 xhigh，并开启 preserve_thinking 和[高分辨率图像输入](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)（96 页图像中有 37 页超过默认的 2.6 MP 上限，不开启则会被缩小）。采样参数沿用百炼图像输入思考模式的[默认值](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)：temperature 0.6、top_p 0.95、top_k 20；baseline 则使用 temperature 0 并关闭思考。

运行时，每题最多包含 25 轮常规回合加 1 轮只能提交答案的回合，限时 30 分钟，16 道题并行执行。模型推理全部在百炼完成，沙箱容器、模型网关和 Phoenix 运行在我们自己的电脑上，负责裁剪图像、运行 Python、转发请求和记录日志。

## 4 结果

| 方案 | 模型来源 | 平台得分 |
|---|---|---|
| Baseline（整页直接作答） | 百炼 qwen3.8-flash | 71.6 |
| **本方案（正式提交）** | **百炼 qwen3.8-flash** | **78.6（+7.0）** |

正式运行中，908 道题全部获得通过格式校验的非空答案，且未触发重跑。平均每题进行 3.5 轮（中位数为 2 轮），调用 zoom 2.0 次、bash 0.95 次；16 道题并行运行，共用时约 28 分钟。

整次运行共输入 2305 万 token（其中 80.6% 命中缓存），输出 118 万 token（含思考过程）。按百炼北京地域价格估算，费用为 8.61 元，约合每题 0.95 分钱。
