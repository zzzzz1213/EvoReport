# EvoReport

EvoReport 是一个本地实习日报/周报助手。它从你明确添加的 Git 工作区中只读采集当日提交与变更摘要，结合手动备注生成报告。你确认的终稿会形成个人数据集，用于评测候选写作策略。

## 快速开始

项目需要 Python 3.11+ 和 Git。

```powershell
cd E:\University\大四\实习\evo\EvoReport
python -m pip install -r requirements.txt
python -m evoreport
```

打开 `http://127.0.0.1:8090/`。服务默认只监听本机。

Windows 也可以直接运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-evoreport.ps1
```

## 每日使用

1. 在“项目管理”添加本地项目的绝对路径：Git 项目自动采集提交；普通项目仅使用手动记录。
2. 在“生成日报”选择日期和项目，补充完成事项、问题与明日计划。
3. 修改自动生成的初稿，点击“确认为终稿”。
4. 周五在“报告历史”选择当周周一，生成周报。
5. 累积足够终稿后，在“写作进化”评测候选策略。

生成后的日报草稿会在编辑时自动暂存到本地数据库。页面刷新后打开“生成日报”会恢复最近编辑的未确认草稿；也可以在“报告历史”选中草稿，点击“继续编辑草稿”。只有点击“确认为终稿”后，内容才会进入个人评测数据集；若自动保存失败，页面会明确提示，请先重试保存再离开。

确认终稿时会重新审查你实际提交的正文。检测到未脱敏敏感信息时必须先修改；若发现来源事实或必需章节缺失，页面会给出脱敏提示，由你检查后决定是否继续。系统会保留生成时初稿，用于记录草稿到终稿的结构化修改反馈；自动暂存不会覆盖这份基线。反馈不重复保存正文，Holdout 反馈不会参与策略学习。

### 更快的每日流程

1. 白天在首页“随手记一条”留下简短工作记录，可选关联项目。
2. 晚上点击“一键汇总今日工作”。
3. 系统会自动合并全部 Git 项目的当日活动和快速记录，然后打开可编辑草稿。
4. 你只需补充问题/明日计划，修改后确认终稿。

### 自然语言快速整理

在“生成日报”粘贴一段自然语言工作描述，系统会在本地按“今日完成、遇到的问题、明日计划”预填表单。带这些明确栏目标签的句子会优先归入对应栏目，即使“遇到问题”中含有“排查”“调整”等动作词；没有标签的句子仍按关键词整理。该操作不会自动保存报告；你检查并生成初稿后，才会进入正常的确认流程。

### 导入旧日报

“报告历史”可导入以前的真实日报。选择对应日期并粘贴原文后，EvoReport 会原样保留正文，标记为“历史导入”，并作为个人写作样本建立评测记录。

为体验页面可导入内置的模拟日报。模拟日报会标记为“模拟样本”，只用于浏览和测试流程，绝不进入个人评测数据集或影响写作策略。

## 自进化与数据集

每份已确认日报保存“事实来源、初稿、终稿”。样本稳定分入 Validation 或 Holdout。候选策略会评估：

- Git/手动事实覆盖率；
- 日报章节完整性；
- 敏感信息安全；
- 与用户终稿的相似度。

默认至少需要 3 份 Validation 和 1 份 Holdout。只有 Validation 不退化、Holdout 安全与综合分不退化的候选才能自动激活。未通过的候选只留档，不能手动绕过门禁。

### 个人写作画像与可解释评测

系统仅从分入 Validation 的已确认真实日报中提取列表偏好、单条长度、开发统计偏好、章节覆盖率和常用项目术语；模拟样本与 Holdout 样本不会参与候选策略生成。满 4 篇 Validation 日报后，画像会影响候选策略。评测页面会展示候选的具体未激活原因，以及 Validation/Holdout 的综合分变化。

## 多 Agent 执行记录

日报生成以本地、确定性方式编排 Planner、Fact、Writer、Critic、Gate 五个角色。Critic 只读取事实来源和候选草稿，独立检查事实覆盖、章节和敏感信息；未通过时 Writer 根据反馈最多返工两轮。Gate 汇总审查结果，待检查草稿会在页面标记。策略评测沿用 Fact/Writer/Critic/Gate 四角色。每次运行记录角色状态、轮次、摘要、耗时与指标；Trace 不重复保存完整日报正文。

### 可选豆包润色

配置火山方舟的 `ARK_API_KEY` 后，Writer Agent 会将**已脱敏的本地初稿**发送给指定模型润色；Critic 与 Gate 仍会在本地检查事实覆盖、章节和敏感信息。未配置 Key 或调用失败时自动回退到本地 Writer。请仅在允许将日报摘要发送至火山方舟时启用。

## 导出与备份

“报告历史”支持导出选中的单篇报告或全部已确认的真实报告为 UTF-8 Markdown 文件，也可创建 SQLite 数据库备份。文件只会写入 EvoReport 项目目录下的 `exports/` 和 `backups/`，模拟样本不会进入已确认报告归档。

选中一篇日报或周报后，也可导出可编辑的 Word `.docx` 文件。Word 导出保留日期、标题、段落、加粗与列表结构，文件保存在 `exports/`。

恢复时选择 EvoReport 的 `.db` 文件，页面先检查完整性并预览项目、报告和记录数量。确认后，系统先把当前数据库保存到 `backups/`，再恢复选中的备份。恢复会以备份中的数据替换当前数据；先检查预览再确认。上传文件最大 64 MB，等待确认的文件暂存于 `backups/pending/`。

## 安全边界

- Git 采集只执行 `rev-parse`、`log`、`branch` 和 `status`。
- 普通项目只验证目录存在，不执行 Git 命令，也不添加虚构的开发统计。
- 不修改被管理项目，不执行项目脚本。
- 默认不发起网络请求；仅在显式配置 `ARK_API_KEY` 时，Writer Agent 才会向火山方舟发送已脱敏初稿。
- 生成报告会对常见 Token、API Key、密码和 Secret 模式进行脱敏。
- 本地数据默认保存在 `evoreport.db`，该文件已被 Git 忽略。

## 测试

```powershell
python -m unittest discover -s tests -v
```

浏览器端到端测试会在临时目录启动独立服务，使用临时 SQLite 数据库与导出目录，不触碰日常数据。需要本机安装 Microsoft Edge 和可选的 Playwright Python 包：

```powershell
python -m pip install playwright
python -m unittest tests.test_browser_e2e -v
```

该测试在无界面浏览器中覆盖空输入提示、生成失败后的按钮恢复、项目添加、日报生成与确认、历史查看及 Word 下载；未安装浏览器依赖时会跳过，不影响其他单元测试。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVOREPORT_HOST` | `127.0.0.1` | 监听地址 |
| `EVOREPORT_PORT` | `8090` | 服务端口 |
| `EVOREPORT_DATABASE_PATH` | 项目根目录 `evoreport.db` | SQLite 路径 |
| `EVOREPORT_ARTIFACT_PATH` | 项目根目录 | 导出（`exports/`）与备份（`backups/`）文件的根目录；未设置时使用项目根目录 |
| `ARK_API_KEY` | 空 | 火山方舟 API Key；仅配置在本机环境变量，绝不写入代码或数据库 |
| `EVOREPORT_ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟 OpenAI 兼容 API 地址 |
| `EVOREPORT_ARK_MODEL` | `glm-5-3-flash-260828` | Writer Agent 使用的文本模型或推理接入点 ID |

### 隔离的测试环境

如需在不触碰正式数据与项目目录的前提下体验完整流程，可运行 `scripts\start-evoreport-test.ps1`。
它会在系统临时目录创建独立的数据库与 artifact 根目录（每次运行生成唯一目录），并使用 `8091` 端口启动服务，
避免读写项目根目录下的 `evoreport.db`、`exports/` 和 `backups/`。停止后删除临时根目录即可清理。

脚本在启动前会保存 `EVOREPORT_DATABASE_PATH`、`EVOREPORT_ARTIFACT_PATH`、`EVOREPORT_PORT`、`EVOREPORT_HOST`、
`ARK_API_KEY` 的调用方原值，结束时恢复；测试进程强制监听 `127.0.0.1`，并清除 `ARK_API_KEY` 以保证使用本地 Writer，
不继承外部模型配置、不发起外部调用。
