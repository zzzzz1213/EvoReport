# 数据地图

## 2026-10-19 · 调整项目完成范围

本次没有读写数据库或修改评测样本。清单沿用既有 7 份 Validation、5 份 Holdout 和 26 项有效人工审查标注的记录，明确真实负例仍缺失，长期数据目标不作为完成门槛。

## 2026-10-19 · 日报队列状态

队列仅保存在服务实例内存中，状态接口返回运行数、等待数与上限，不包含日报内容；没有新增数据库字段。等待任务不会预先创建 Agent 运行记录或领取恢复运行，恢复领取后等待时间计入运行预算。

## 2026-10-19 · 角色进程生命周期加固

角色响应校验协议版本和实际进程编号；进程退出或响应异常时清理进程及管道。没有新增数据库字段或修改评测数据。

## 2026-10-19 · 真实数据清单更新

当前 `evaluation_cases` 汇总为 7 份 Validation、5 份 Holdout；系统生成来源分别为 5 份和 4 份。`review_claim_labels` 中有 5 份报告、26 项有效标签，覆盖 1 个项目；人工标签均为有来源。内置 Critic 产生 2 项复合事项误报和 24 项正确放行，无真实无来源样本，召回率为空。尚未建立审查冻结基准或候选规则。

## 2026-10-19 · 当日后续工作补充草稿

`reports.id=07839e64-3956-4707-9f6d-9b9d82c7e230` 保存 2026-10-19 后续工作的系统草稿，来源快照包含 7 条 `manual` 事实。关联 `daily_report` 运行 `b05eb3dd-ef1e-421d-bbfd-3f7d61dac7d3` 已通过；报告保持 `draft`、`final_text` 为空，尚无 `evaluation_cases` 或人工审查标签。同一日期的多份报告不会增加日期多样性。

## 2026-10-19 · 旧库人工标注保存修复

数据库初始化现在分别检查 `review_claim_labels` 与 `review_missed_claims` 的 `claims_sha256` 列，缺失时添加 `TEXT NOT NULL DEFAULT ''`。空默认值只用于兼容迁移，无法通过当前事项切分指纹校验，因此旧标签保持失效状态直至用户重新保存。正式库已完成迁移，未自动写入任何人工标签。

## 2026-10-19 · 五份真实日报草稿补充

新增五条 `origin=generated`、`status=draft` 的日报记录：2026-10-10 `342ab865-f90f-466a-add6-5efc00010577`、10-13 `768292ed-ed97-4231-898d-cd1309a628cf`、10-14 `440c34e4-6a31-48b1-b2e6-2f37127497a6`、10-16 `370ac7d8-6e56-4e75-8208-6cd742325ea7`、10-17 `fb66620a-1c98-4224-bf1d-e52f82af9da5`。来源快照共包含 29 条 `manual` 事实，正文完整保留全部事实；四个 `daily_report` 运行通过，10-17 运行因复合表述的片段级无来源提示标记为 `needs_review`。五份报告的 `final_text` 均为空，未创建 `evaluation_cases` 或人工审查标签。

## 2026-10-19 · 首份本轮真实日报草稿

`reports.id=066fcaf8-9b46-4eea-98d4-7148ff18aaf9` 保存 2026-10-19 的系统生成日报草稿，来源快照包含 6 条 `manual` 事实及对应行号。报告保持 `draft`，`final_text` 为空；关联 `agent_runs.id=549a2af9-fddc-404e-b2c3-e18b8ac36462` 已通过。草稿人工补回生成时在分号后遗漏的“内置 Critic v1 保持不变”，来源快照与 `initial_draft` 保持原始生成记录。当前没有关联 `evaluation_cases` 或人工审查标签。

## 2026-10-19 · Critic 外部受控样本修复

结构化语义权重大于零时，逐项审查会将同一来源事实的片段作为匹配变体，但仍返回原事实索引和证据编号；未新增表或写入外部受控样本，内置 v1 数据不变。

## 2026-10-19 · 外部参考第二轮受控对照

9 组外部受控样本仅在内存运行内置 Critic v1，记录片段数、无来源提示和匹配分；不写数据库、不进入个人评测分组或数据目标。

## 2026-10-19 · 外部参考标注复核

外部参考三项当前判断为待核实，原始有依据判断保留；未写数据库，不计入误报或漏报。


## 2026-10-19 · 外部参考首轮对照

外部参考模拟只读内置 Critic v1 并在内存执行，不写数据库；原始人工判断与系统提示均在独立结果文件，分歧未解决。


## 2026-10-19 · 第一轮外部参考

外部参考保留原链接、时间范围及人工构造变体；尚无人工标签、系统对照结果或数据库导入，周报逐项无来源审查当前不支持。


## 日报候选整理（2026-10-19）

候选文件只引用既有报告的手工来源快照和终稿中的待核信息；不写入数据库、不分配评测分组，也不生成审查标签。


`evaluation_benchmarks.scenarios_json` 在新基准建立时冻结各样本的来源、项目数、篇幅、修改和评测资格标签，以及有效 Validation 人工审查场景；审查 Holdout 的人工标签不参与。场景快照纳入整体指纹，公开基准对象只暴露可评测数量，不返回 Holdout 标签。缺字段的旧基准不回填，保留原指纹和原选样顺序。`writer_prompt_evaluations.evidence_json` 保存本次分组数量和 Validation 场景覆盖摘要，恢复按原快照核验。

默认 SQLite 数据库为项目根目录下的 `evoreport.db`。路径可通过 `EVOREPORT_DATABASE_PATH` 修改。

`GET /api/reports` 的 `project_id` 参数匹配 `reports.source_json.projects[].id`，不依赖当前项目目录是否有 Git 提交；`start` 和 `end` 按报告日期筛选并包含边界。旧报告没有项目快照时只出现在未按项目筛选的列表。事实及 `fact_sources` 按原顺序对应；手工记录保留行号但不推断项目归属，Git 事实保留生成时的项目名称与提交哈希。归档查询不改写原报告或评测分组。

| 表 | 主要数据 | 说明 |
| --- | --- | --- |
| `projects` | 项目名称、本地绝对路径、`project_type` | 登记用户明确添加的 Git 或普通项目 |
| `reports` | 类型、日期、`origin`、所选项目及事实的逐条来源快照、生成时初稿 `initial_draft`、自动暂存草稿、终稿、策略版本 | 项目快照支持按项目检索任务归档；初稿快照用于计算真实修改反馈，`origin` 区分 `generated`、`historical` 与 `synthetic` |
| `work_notes` | 日期、关联项目、快速记录内容 | 白天随手记录，一键汇总时写入报告事实快照 |
| `evaluation_cases` | 报告来源、Validation/Holdout、期望终稿 | 用户确认终稿时自动写入；仅 Validation 参与候选推导和写作画像 |
| `review_claim_labels` | 报告 ID、事项序号、人工来源标签、来源／终稿／切分 SHA-256 指纹、时间 | 仅已确认的 Validation 系统日报可整篇标注；指纹不符时原标注不计入审查基准，旧库自动建表 |
| `review_missed_claims` | 报告 ID、补标序号、终稿行号与零基列位置、来源／终稿／切分 SHA-256 指纹、时间 | 保存 Critic 未识别的无来源片段位置，不复制片段正文；与整篇标注原子替换，旧库自动建表 |
| `critic_rule_versions` | 不可覆盖的版本、声明式 `skill_json`、提案证据 `proposal_json`、算法版本、SHA-256 指纹、来源与时间 | 旧阈值版为 schema v0；新 Skill 为 schema v1，可手动创建或从 Validation 自动提出；同一算法下不重复创建相同阈值 |
| `critic_rule_evaluations` | 候选版本、标注集指纹、基线和候选计数、逐项分类变化、结论与时间 | 同一候选和标注集只保存一次；仅存报告 ID、事项序号和行号，不复制正文；标注集变化后旧结果标为失效 |
| `critic_benchmarks` | 审查基准版本、冻结 Validation 指纹、建立时间、Holdout 一次性领取时间及最终汇总 | 仅保存指纹和汇总，不重复保存正文；Validation 变更或标注不足会阻止最终门禁 |
| `critic_benchmark_members` | 基准版本、报告 ID、Validation／Holdout／排除分组 | 冻结前的有效标注为 Validation，其余已有标注排除，之后新标注归 Holdout；旧基准已用报告不重复进入新 Validation |
| `critic_rule_switches` | 切换 ID、前版和当前版、首次启用／回滚来源、时间 | 仅通过最新审查 Holdout 门禁的候选可首次启用；内置版及曾启用版本可手动切回，历史切换不覆盖 |
| `writing_feedback` | 报告 ID、数据分组、结构化修改信号、创建时间 | 仅系统生成并确认的日报写入；不重复保存正文，默认只读取 Validation 反馈 |

| `memory_entries` | 范围、项目 ID、固定字段、脱敏值、可空到期日 `expires_on`、来源、被纠正记录、状态 | 保存手动偏好、项目背景和评测激活记录；项目备注到期后停止供新报告检索，纠正与清理保留审计链 |
| `memory_suggestion_decisions` | 候选指纹、项目 ID、采纳或忽略状态、记忆 ID、来源报告位置、时间 | 保存用户对归档候选的决定，采纳的项目备注以候选指纹为来源 ID；旧库自动建表 |
| `personal_memory_suggestion_decisions` | 候选指纹、采纳或忽略状态、记忆 ID、终稿修改证据、时间 | 保存个人写作偏好候选的人工决定；不保存初稿或终稿正文，旧库自动建表 |
| `strategies` | 策略版本、格式配置、评测指标、状态 | 只有通过门禁的版本可激活或回滚 |
| `strategy_activations` | 切换前后版本、来源、时间 | 原子记录评测激活、手动启用或自动回滚；最近一次记录确定上线监测窗口 |
| `strategy_monitor_checks` | 激活事件、最新样本 ID、决策、原因、两组样本编号和指标 | 保存上线监测判断；同一激活事件和最新样本只记录一次，回滚与判断写入同一事务 |
| `writer_prompt_versions` | 版本、风格补充、完整指令、SHA-256 指纹、来源和状态 | 内置 v1 与手动创建的不可覆盖版本；同一指令指纹不能重复创建 |
| `writer_prompt_switches` | 切换前后版本、来源、时间 | 原子记录评测启用、手动切换与自动回滚 |
| `writer_prompt_monitor_checks` | 指令切换、末尾样本、决定、原因、证据摘要 | 保存双窗口监测和自动回滚依据，不复制日报正文 |
| `writer_prompt_evaluations` | 候选与当前版本、基准版本、状态、阶段、逐次评分摘要、已尝试模型调用数、累计耗时、恢复次数和原因 | 同一候选与基准只创建一次；恢复原评测并在完成时核对指令与基准指纹，不保存模型输出正文 |
| `evaluation_runs` | 候选版本、决策、指标、来源证据与原因 | 保留每次策略评测及规则变化的依据 |
| `evaluation_benchmarks` | 版本、两组样本 ID、各组内容哈希、两组生成时初稿指纹、Validation 覆盖摘要 `coverage_json`、整体指纹、Holdout 使用时间及领取运行 ID | 冻结基准成员、来源、终稿、初稿和 Validation 场景计数；旧行缺失的指纹与覆盖摘要保持空值，每个版本的 Holdout 只允许一次门禁 |
| `agent_runs` | 工作流、关联报告、状态、耗时、Token 累计用量与其中估算量、更新时间、失败类型、恢复次数、指令版本快照 | 记录日报生成、终稿审查或策略评测的运行；恢复沿用原运行 ID、累计预算和相应规则版本 |
| `agent_steps` | 角色、输入输出摘要、状态、指标、步骤耗时、角色执行模式／协议版本／进程 ID／调用耗时 | 保存 Planner/Fact/Writer/Critic/Gate 节点及返工轮次，不保存完整日报正文或子进程环境 |
| `agent_checkpoints` | 运行 ID、阶段、轮次、脱敏候选正文或空串、审查摘要、交接包、输入指纹、模型使用标记 | 日报生成从最近安全边界继续；终稿审查只保存来源与终稿指纹、警告确认状态及 Critic 摘要，不复制终稿正文 |
| `agent_step_snapshots` | 步骤 ID、阶段、脱敏候选正文、审查和交接快照、来源指纹、回放规则版本 | 与日报步骤和检查点同事务保存，仅用于本地只读回放，不经 Trace 接口返回正文 |
| `strategy_evaluation_checkpoints` | 运行 ID、阶段、冻结候选与基线配置、基准与 Validation 指纹、部分指标和证据摘要 | 格式策略评测从准备、Validation、Holdout 领取到门禁结论的同运行恢复；不保存样本正文 |

日报和终稿审查的 `agent_runs.instruction_json` 绑定 Critic 规则版本与指纹；恢复及回放读取绑定版本，不依赖当前活动版。旧版仅有 `version=1` 的快照按内置规则兼容。审查上线后监测读取最近切换之后新确认、写作 Validation 且有效人工标注的报告，排除所有审查基准成员；仅返回前后规则的汇总计数，不保存新的报告正文或网络遥测。

## Git 数据来源

- `git rev-parse --is-inside-work-tree`：项目路径验证。
- `git log --since --until --numstat`：当日提交与文件统计。
- `git branch --show-current`：当前分支。
- `git status --short`：未提交文件数量和路径。

`project_type=plain` 时不采集 Git 数据，不存储文件内容，报告事实仅来自用户手动填写。

`work_notes` 的内容最多 1000 个字符，仅在本地 SQLite 保存，并在生成报告时作为可追溯事实写入 `reports.source_json`。

自然语言快速整理优先按明确栏目标签归类，未标注句子按关键词归类；接口仅返回预填的三段文本，不写入任何数据表。只有用户后续生成并确认日报时，内容才会进入 `reports`。

`测试用随手记样例.md` 的占位段落有五条解析回归用例；接口测试另验证调用解析接口后 `reports` 与 `evaluation_cases` 均无新增。若在隔离库中确认占位日报，它仍会按 `origin=generated` 进入该隔离库的评测数据集。

自动暂存仅更新 `status=draft`、`origin=generated` 报告的 `reports.draft` 和 `updated_at`；刷新后按最近更新时间恢复未确认日报。暂存不会修改 `final_text` 或新增 `evaluation_cases`。已确认报告的草稿更新请求会被拒绝。

历史导入报告使用 `origin=historical`，正文同时保存为初稿与终稿，并生成对应的 `evaluation_cases` 样本。

模拟日报使用 `origin=synthetic`，正文同样可在历史页面浏览，但不创建 `evaluation_cases`，因此不会影响写作进化的 Validation/Holdout 数据集。

Agent Trace 的 `input_json` 与 `output_json` 仅存放计数、配置、任务/证据编号、缺失事实序号、缺失标题、疑似无来源事项行号、轮次和决策等摘要；评分指标存入 `metrics_json`，不重复存储报告正文。日报生成最多返工两轮，运行异常会把 `agent_runs.status` 标为 `failed`。终稿确认另存 `final_review` 运行，安全问题标为 `blocked`，事实、章节或来源警告在用户确认前标为 `needs_review`。

Writer 指令版本保存完整的固定约束与手动风格补充，报告运行仅在 `agent_runs.instruction_json` 绑定版本号和完整指令指纹，不重复保存指令正文；恢复继续读取绑定版本，回放核对版本存在且指纹与内容一致。终稿审查另记录 Critic 代码规则版本，策略评测记录评测规则版本；这些确定性规则尚不能通过页面切换。旧运行的指令快照为空，按内置 Writer v1 解释；旧库自动补充新列与内置版本。指令版本只影响配置模型后的润色调用。

Writer 指令监测通过 `evaluation_cases`、`reports`、`agent_runs` 和 Writer 步骤交叉核对样本：只读取 Validation、已确认的系统日报，运行快照版本须匹配，且至少一个 Writer 步骤明确记录 `llm_used=true`。切换前最近 5 份旧版样本作为基线；切换后样本按运行创建时间排序，最近两组各 5 份用于回滚判断。指标由保存的 `initial_draft` 对照 `expected_text` 计算，不重新调用模型。`writer_prompt_monitor_checks.evidence_json` 只保存版本、报告 ID、聚合指标和阈值。写入决定时再次核对最新切换来源、活动版本和末尾样本；回滚与新 `auto_rollback` 切换记录在同一事务完成。同一切换和末尾样本的重复判断返回既有记录。

指令评测读取最新冻结基准，按冻结场景确定性抽取最多 6 份 `origin=generated` 的 Validation 日报和 2 份 Holdout。`benchmark_prompt_cases` 先验证基准来源、终稿和全部成员初稿的分组指纹，再读取报告创建时的 `initial_draft`；两版指令使用相同的脱敏初稿。基准创建时仅保存初稿聚合指纹，不重复存储初稿正文；旧基准初稿指纹为 `NULL`，不会用迁移时现值回填，不能直接用于 Writer 指令评测。`progress_json` 每次调用后原子保存样本 ID、报告 ID、脱敏初稿 SHA-256、分数与 Critic 问题数量；`evidence_json` 记录版本指纹、模型标识及最终摘要，均不保存模型输出正文。正常完成需 8–16 次调用，调用前先增加 `model_calls`，因此中断中的调用也计数；最多 18 次尝试、累计 240 秒，外部调用本身无法强制中断。失败运行可立即恢复，超过 60 秒未更新的运行可接管，最多恢复两次；接管时对未记录的尾段最多补计 20 秒，恢复代次阻止旧执行者写回结果。Validation 未提升或候选审查未通过时不读取 Holdout；通过后同一事务核验 Holdout 初稿、设置共享的 `holdout_used_at` 和领取评测 ID，并进入 `holdout_claimed`。恢复时已保存的评分不重复计算，未落盘的调用可在预算内重试；Holdout 只能由领取它的原评测继续。快照变化或预算耗尽使评测不可恢复；旧行无检查点，不自动重测。首次启用候选需其最新评测通过、相对当前活动版本且使用最新基准；已启用过的旧版可手动切回。

检查点单独保存经脱敏的候选正文和 `handoff_json` 任务/证据交接包，不通过 Agent Trace 接口返回；交接包保存任务编号、证据编号、事实序号、来源索引、角色权限元数据及最多两轮 `plan_revisions`。计划修订节点只保存修复动作、依赖、关联任务／证据、状态和被替换节点，不复制事实或问题片段正文。新增 `replanned` 阶段用于恢复 Planner 已生成修订但 Writer 尚未执行的运行。恢复时用 `reports.source_json` 与生成时初稿计算 SHA-256 指纹，检查报告仍是未编辑的系统草稿；异常中断需超过 60 秒才允许接管，失败运行可立即恢复，每个运行最多恢复两次，每次执行限制 45 秒，跨恢复累计限制 90 秒。每步原子更新 `agent_runs.duration_ms`；非正常中断后将无法精确测得的尾段按最多 45 秒保守计入，预算耗尽标为 `budget_exhausted`。模型调用等待受单次 20 秒及剩余预算限制；后台请求无法强制终止，迟到结果不会写入。恢复代次用于阻止旧执行者在被接管后写入；Gate 后的报告草稿和运行终态在同一事务提交。旧库自动补充交接列；旧检查点交接包为空时从受指纹保护的来源快照重建，已有步骤但无检查点的旧运行仍不能安全恢复。

日报和指令评测共用每进程最多 4 个模型调用线程的限制；超时后线程仍占用名额，直到请求退出。等待名额也计入调用截止时间。日报超时以 `ModelCallTimeout` 记录失败并保留预留 Token；指令评测保留调用次数和已保存评分，在剩余时间、调用次数和恢复次数允许时可恢复。后台线程不操作存储，迟到结果不会提交；实际服务端耗时和费用无法由本地等待截止保证。

新日报交接包保存 `contract_version`、角色权限表、初始任务的 `action`／`depends_on`／`execution_order`、`task_states`、已形成证据和 `plan_revisions`。初始任务图须唯一覆盖全部事实，并拒绝未知依赖、自依赖和循环依赖。调度器按声明顺序选择依赖均完成的节点，每完成一个节点便原子保存 Fact Trace 与检查点；恢复核对来源后仅执行 `pending` 节点。Critic 反馈只含问题类型、事实／证据／任务编号、章节名、无来源片段位置和安全布尔值；Planner 据此生成固定白名单修复动作，Writer 按修订执行。Planner/Fact 的输入投影只有事实数量、来源位置、项目与备注 ID 等元数据；Writer 只取草稿、事实、证据、结构化反馈和返工计划，Critic 只取候选及证据，Gate 只取审查摘要。

角色协议版本 2 使用标准输入／输出传递单行 JSON，新增 Planner `replan_tasks` 及 Writer 分类返工输入；请求只接受固定角色、操作和字段组合，并限制正文、整体请求及响应大小。每个角色由一个按需启动的常驻 Python 子进程执行；主进程不向其环境传递 `ARK_API_KEY`、数据库路径或模型地址，Store 与模型客户端也只由主进程调用。单个角色调用默认限时 5 秒，并受日报剩余时间预算约束；超时后主进程杀掉对应角色进程并记录 `RoleProcessTimeout`，后续恢复会启动新进程。`agent_steps.output_json.execution` 保存 `mode=isolated_process`、协议版本、worker PID 和单次调用耗时，便于回放核验执行边界。子进程仍继承当前系统账户的文件和网络权限，这一机制不是操作系统沙箱。

日报模型 Writer 的整次运行 Token 限额为 12000。调用前原子预留按 UTF-8 字节估算的输入 Token 与最多 2048 输出 Token；返回后使用响应 `usage` 结算，缺失用量时按同一规则估算，调用中断时保留预留额度。`agent_runs.token_used` 和 `token_estimated` 跨恢复累计，Writer 步骤指标只保存用量和来源，不保存正文。服务端实际输入 Token 可能与估算不同，因而可能在调用返回后才发现超限；旧库新增列初始为 0，旧调用无法补算。

新日报运行的 `agent_step_snapshots` 保存每步脱敏候选与结构化结果；`agent_steps` 的 Writer 摘要只保存输入/候选 SHA-256 指纹、返工任务编号和修复动作。回放按来源指纹重新调度初始任务，重算项目记忆章节任务、每轮 Critic 反馈、Planner 计划修订及 Writer 确定性修复，核对节点状态、证据和角色进程执行元数据，再核验 Gate 决策；不重新启动角色进程或调用模型。`GET /api/agents/{run_id}/replay` 仅返回核验状态、角色和差异原因。当前日报回放规则版本为 11；版本不兼容、角色协议版本不兼容或旧运行没有逐步快照时返回 `unavailable`。

终稿审查的 `agent_steps.input_json` 保存来源与终稿 SHA-256、事实数、终稿字符数及用户是否确认警告；运行绑定 Critic 规则版本。仅当报告已确认且该次审查通过时，回放读取 `reports.final_text` 与 `reports.source_json`，核对指纹，并重算 Critic 输出/指标和 Gate 决策。未确认、旧记录或规则版本不兼容返回 `unavailable`；输入或步骤记录不一致返回 `diverged`。回放接口不返回正文。

终稿确认的新运行在同一事务建立 `final_review` 运行和 `agent_checkpoints` 的 `prepared` 阶段；Critic 步骤与 `critic_ready` 检查点原子写入。检查点交接包保存来源、终稿 SHA-256 与警告确认布尔值，候选正文为空串。失败运行立即可恢复，超过 60 秒未更新的运行可接管，最多两次；恢复请求须重交原终稿，并核对来源和 Critic 规则。完成时在同一事务写入 Gate、运行终态，若通过还写入确认报告、评测样本和修改反馈；事务失败不会留下半确认的报告。被拦截或待人工检查的运行不确认报告。

格式策略评测的 `agent_runs.instruction_json.evaluation` 记录 `evaluation_runs` ID、候选与基线策略版本、基准版本和指纹；`agent_steps` 继续保存数量、候选配置及指标摘要。策略评测回放只读取已保存的评测记录、策略版本和基准元数据，核对 Trace、指标及激活门禁，不调用 `benchmark_cases`、不重读 Holdout 样本或重新评分。旧运行缺少关联信息返回 `unavailable`，记录不一致返回 `diverged`。这属于决策一致性核验，不是模型输出重演。

格式策略评测开始时原子创建 `agent_runs` 和 `strategy_evaluation_checkpoints`，保存候选配置、基线版本、基准指纹及 Validation 样本指纹，不存正文。Validation 评分完成后写入部分指标；`claim_strategy_evaluation_holdout` 在同一事务设置 `evaluation_benchmarks.holdout_used_at`、`holdout_run_id` 和阶段 `holdout_claimed`。失败运行可立即恢复，超过 60 秒未更新的运行可接管，最多恢复两次；恢复沿用原候选和原运行，若 Holdout 评分未落盘则只在该运行内重算，不再领取门禁。`decision_ready` 保存最终指标，策略版本、激活事件、评测记录、学习来源、四步 Trace 与运行终态在同一事务提交；失败事务不留下部分激活。基线或 Validation 快照变化使旧运行失效。旧基准的领取运行 ID 为 `NULL`。Writer 指令模型评测使用自身的逐次评分检查点及评测 ID 恢复。

`writing_feedback.signals_json` 仅保存是否修改、相似度、字符数变化、列表样式变化、开发统计前后状态、列表项最大长度及固定章节增删；通过 `report_id` 追溯原报告，不复制终稿正文。Holdout 反馈可归档，但候选策略和个人记忆候选只读取 Validation 修改信号。旧数据库迁移时用现存草稿补齐 `initial_draft`，旧报告无法还原更早的生成时版本，也不会凭空补齐新增修改信号。

自进化只使用与 Validation 报告 ID 对应的修改反馈和 Critic 失败摘要提出固定格式参数候选。`evaluation_runs.evidence_json` 保存参与推导的报告 ID、审查运行 ID、参数前后值与来源类型，不复制正文；Holdout 仅参加最终评分门禁。Validation 综合分须严格提升，Holdout 综合分与安全性须不退化。旧库会自动补充 `evidence_json` 列。

基准集建立时读取样本并保存 Validation/Holdout 样本 ID、规范化内容指纹和由每份合格生成初稿 SHA-256 组成的分组指纹；整体指纹覆盖两组内容与初稿指纹。`coverage_json` 仅由 Validation 中 `generated` 或 `historical` 的日报计算，不存正文、项目名称或样本 ID；按事实来源（手工/Git/混合/未标注）、所选项目数（0/1/至少 2）、终稿字符数（小于 200、200–599、至少 600）及初稿修改情况保存计数与提示。生成日报比较前后正文时忽略首尾空白，历史导入标为无法比较。旧基准的 `coverage_json` 为 `NULL`，状态接口另算当前 Validation 覆盖并明确标为现值。读取冻结样本时核对分组与哈希，新样本不会自动进入旧版本。日常状态查询只读取 Holdout 编号和数量，覆盖计算不读取 Holdout 正文。提案先评当前版本的 Validation；候选未提升时 `evaluation_runs.metrics_json` 中的 Holdout 指标为 `null`，不运行保留集评分。通过 Validation 后原子领取该版本唯一一次 Holdout 门禁；格式策略评测中断后仅能手动恢复原运行，不能再次领取或让新候选使用它。下一版本沿用已有 Validation 并要求至少一份新增 Validation；Holdout 只能使用从未进入任何旧基准的新增样本，因此不能通过换版本重测旧保留集。

策略激活时在同一事务更新活动版本并写入 `strategy_activations`，重复启用当前版本不产生新事件。上线监测只查询最近一次激活后创建且确认的 `generated` 日报，其 `reports.strategy_version` 须等于当前活动版本，`evaluation_cases.split` 须为 Validation。达到 5 份后用相同报告来源及终稿分别回放前后策略，并对两者施加报告创建时保存的相同手动偏好；状态接口返回样本报告 ID、综合分和安全性。少于 5 份时不计算比较分数。评测自动激活的策略满 10 份后，每次新增 Validation 样本取最近两组各 5 份；若两组综合分均下降至少 0.05 或任一组安全性下降，事务内核对激活事件和活动版本，再切回直接上一版本并记录 `auto_rollback` 来源。判断证据不保存报告正文；同一窗口重复检查不新增事件。手动激活及 Holdout 确认不触发判断，终稿保存成功不受后续监测错误影响。

当前任务事实保存在 `reports.source_json.facts`；同序的 `fact_sources` 保存手工记录行号，或项目 ID、名称与 Git 提交哈希。创建新日报时读取个人偏好和所选项目的未到期备注，并在 `memory_context.project_guidance` 保存由备注派生的章节关注点、表达参数及来源记忆 ID。多个项目备注对同一参数值不一致时不生成表达参数；项目表达参数覆盖活动策略，显式个人偏好再覆盖项目参数。Planner 只接收备注 ID 和章节关注点，为关注章节创建无事实的 `preserve_section` 任务；Writer 使用已按表达参数生成的初稿。备注正文不进入任务交接包、事实列表、证据或 Critic 输入。项目备注的 `expires_on` 可空；按北京时间过滤，到期日当天仍有效，次日从新报告上下文排除，但可作为候选的历史过期匹配。纠正和清理只影响新报告，已有报告与检查点仍使用创建时快照。`memory_entries` 中个人偏好只接受 `numbered`、`include_statistics`、`max_item_length`；项目备注仅接受 `note`。

项目记忆候选只读取 Validation 中已确认、`origin='generated'` 的单项目日报来源快照。提取限手工完成、问题与计划字段；带约束词、长度 6–120 字且不含脱敏占位符的短句经过空白、标点和有限同义词归一，相似约束须在至少两个不同日期出现。个人候选读取同一范围内的 `writing_feedback`，编号、开发统计或最大列表项长度的同类修改也须跨两个日期重复。候选实时对照活动记忆，返回冲突、相似合并对象和过期匹配；候选指纹不包含正文之外的数据。项目决定保存在 `memory_suggestion_decisions`，个人决定保存在 `personal_memory_suggestion_decisions`。采纳时新记忆、被纠正状态和决定在同一事务写入，`source_type='archive_candidate'`、`source_id` 为候选指纹；忽略不写记忆。Holdout 不参与候选生成。

日报“今日完成”的无来源检查将列表项按分号、句号、逗号、顿号和“并／和”等连接词后出现的独立动作逐段匹配来源事实；对相似文字再比较去掉“完成”后的具体内容，并检查否定或动作差异。审查结果只保存片段的行号、零基列起止位置、最接近事实序号和文本匹配度。确认接口从本次提交的终稿按位置提取并脱敏后展示待核实片段，以及最接近事实的手工记录行号、Git 提交或旧报告事实序号；`agent_steps` 与 `agent_checkpoints.review_json` 不新增片段正文。规则回放版本升为 4，旧规则快照不可直接回放。缺少明确动作标记的复合句及复杂语义改写仍可能漏检，文本匹配度不是校准后的置信概率。

人工审查基准逐项保存 `supported`／`unsupported`，同一报告的全部可识别事项需一次性标注；再次保存会原子替换旧标注。读取时重新按当前切分规则生成事项并核对来源、终稿和事项位置／脱敏文本指纹，失效标注不参加统计。`GET /api/review-benchmark` 只汇总有效的 Validation 标注，对现行 Critic 重算命中、误报、漏报和正确放行；至少 3 篇报告、10 项标注时计算精确率与召回率，分母为零则返回空值。返回的误报和漏报片段经脱敏；未被自动识别、也未被人工补标的隐含事项仍不计入统计。

`review_claim_labels.scenarios_json` 和 `review_missed_claims.scenarios_json` 保存可选人工场景：`compound`、`paraphrase`、`implicit`；旧库迁移为空数组。只将非空标签纳入标注集指纹，使未标场景的旧冻结基准保持有效；更改冻结样本的场景使指纹变化并阻止旧基准门禁。Validation 的 `scenario_coverage` 以父事项为单位合并自身与补标子句标签，同场景计一次，独立补标单独计数；不同场景可以重叠。统计只返回数量，不重复保存正文；Holdout 场景指标不通过该接口暴露。

`review_missed_claims` 保存人工指定的“今日完成”无来源片段行列位置；保存和读取都核验章节、边界及与其他补标的重叠。完全位于单个自动事项内、但不等于整个事项的片段会派生 `parent_claim_index`；部分交叉和跨事项片段被拒绝，父项人工标签必须是 `unsupported`。片段正文只从受终稿指纹保护的 `reports.final_text` 派生，在基准结果中脱敏展示。统计时有补标子句的父项仍只计一次：Critic 已提示为命中，未提示为漏报；多个子句不另加样本数。独立补标单独计为漏报，即使自动事项为空也可形成一篇标注报告。来源、终稿或切分指纹不一致时，整篇标注失效。

Critic Skill 是固定字段的声明式 JSON：schema v1 包含匹配阈值和字符二元组重合度权重；schema v2 另含结构化语义权重和待核实区间；两者都要求顺序固定的四项强制检查，未知字段、非法范围或缺少检查均拒绝。指纹覆盖算法版本和完整 Skill；schema v0 使用原阈值指纹算法兼容旧运行。结构化事项包含上下文、动作、动作类别、完成状态和核心对象；审查综合顺序、字符、动作与对象分数，只在接口响应中从提交终稿派生并展示对象文本，Agent Trace 不保存该片段。人工基准统计有证据、待核实和无证据数量，并继续以二元人工标签计算误报、漏报、精确率和召回率。自动提案只读取最新冻结 Validation，在受限的阈值、字符权重、语义权重和待核实区间网格中评测，筛除误报或漏报增加的组合，再按总错误和待核实数量选择候选。`proposal_json` 保存 Validation 报告 ID、语料指纹、搜索数量、前后计数、场景覆盖和原因，不保存正文。候选随后沿用 `critic_rule_evaluations` 和一次性审查 Holdout 门禁；运行快照继续绑定版本与完整指纹。

个人写作画像不新增独立数据表，而是按需从 `evaluation_cases` 的 Validation 日报计算。Holdout 终稿不会参与画像或候选策略推导，只用于独立门禁。

火山方舟 API Key 仅从进程环境变量 `ARK_API_KEY` 读取，不写入 SQLite。启用模型后，仅将经本地脱敏的日报初稿发送至 `EVOREPORT_ARK_BASE_URL`。

导出 Markdown 文件仅包含报告正文；SQLite 备份通过数据库一致性备份接口创建，文件保存在 artifact 根目录的 `backups/` 中，不涉及已管理项目的任何文件。artifact 根目录由环境变量 `EVOREPORT_ARTIFACT_PATH` 指定，未设置时默认是 EvoReport 项目根目录；`EVOREPORT_DATABASE_PATH` 与 `EVOREPORT_ARTIFACT_PATH` 可分开配置，互不影响。

单篇 Word 导出从 `reports.final_text`（尚未确认时从 `reports.draft`）生成 `.docx`，在 `exports/` 保存可编辑文档，不修改报告原文或数据库记录。

恢复上传的数据库暂存于 `backups/pending/`，通过 `quick_check`、外键检查及核心表结构检查后返回预览计数。确认恢复前自动把当前数据库备份到 `backups/`；恢复成功后删除对应暂存文件。备份与导出目录均被 Git 忽略。

系统不读取 `.env` 内容，不保存文件正文或 Diff。Git 提交标题和手动备注会进入报告事实快照，生成前执行敏感字段脱敏。

项目路径验证的错误仅返回对用户有意义的中文说明，不将 Git 命令的完整输出写入数据库。

浏览器端到端测试将数据库、普通项目目录与 Word 下载目标放在系统临时目录，并显式关闭模型调用；测试结束后停止独立本地服务，不读写默认的 `evoreport.db`。
