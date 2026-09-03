# CHANGELOG

本文件记录本项目（BidOptimizerAgent / Hopkins）每次文件增删改查的变更，写清「为什么改」和「改了什么」。版本号以项目根 `VERSION` 文件为唯一权威（当前 0.1.0）。

## [0.1.0] - 2026-08-23

### 变更（术语统一：标书 → 提案，贴合自由职业接单语境）

- **为什么改**：用户指出「标书」指向招投标体系的正式术语（投标文件），本项目是自由职业接单形式的「投标」，反复用「标书」会错位、误导读者；经确认以「提案」统一替换（与英文 `proposal` 对应）。
- **改了什么**：`README.md`（删 `proposal (标书)` 中文注脚，保留 `proposal engineering`）；`README_cn.md`、`CLAUDE.md`（与 `AGENTS.md` 为同链文件）——「提案工程 / 提案模板库 / 提案 A/B 测试 / 提案是可测试的工件」等表述；`TODO.md`（T1 接收与迁入「提案模板」、漏斗表「提案版本号」、T3「改提案」、T5「提案 A/B 测试」）；`references/outbound-sales.md`（「提案投出去只是开始」「提案准备成本」「用的提案模板」「下一批提案/私信」）。外部引用路径 `ExecutiveAssistantAgent/docs/bidding/bid-templates.md` 与 `task-pool-bidding-sop.md` 属他项目文件名，保持原样不断链。

### 变更（分工修正：找单归 Hopkins，Kit 定位为全团队综合事务）

- **为什么改**：用户澄清分工——Kit 不是专职找单，她作为总经理助理负责全团队综合性事务（整个团队任何事情她都可以管也可以做）；找单专职归 Hopkins，且找单本身就是转化率的一部分：单子本身不行，直接 pass 掉也是提高转化率。原分工描述「Kit（总经理助理）找单投单」与事实不符，不改会误导职责边界。
- **改了什么**：`AGENTS.md` / `CLAUDE.md`（职责边界改为「Kit 统筹全团队综合事务、Hopkins 负责从找单到成交前的全漏斗转化」，「具体负责」新增「找单选单」条目，漏斗由四级扩为五级「线索 → 投单 → 回应 → 谈单 → 成交」，「你的位置」同步）；`README.md` / `README_cn.md`（简介段、漏斗链路、roster 表 Hopkins / Kit 职责行同步）；`TODO.md` T1 漏斗表规格随五级漏斗联动更新。

### 新增（外展销售纪律参考文档：评分制名单 + 3 次触达节奏）

- **为什么加**：Kit 在分析开源赞助变现仓库 [awesome-oss-sponsorship](https://github.com/Lxcardoza993/awesome-oss-sponsorship)（CC-BY-4.0）时发现，其外展销售方法论（评分制名单、分批发送、3 次触达关单、首单让利换案例、社会证明滚动）与本项目投标漏斗（投单 → 回应 → 谈单 → 成交）高度互补，经用户确认提取为参考文档，供漏斗追踪与报价策略工作参考。
- **加了什么**：新建 `references/outbound-sales.md`——原文方法论整理（Prospect Fit Score 七维评分模型、滚动 50 家名单与 3-touch 节奏、成交技巧、KPI 指标、纪律红线）+ 七维度映射到投标场景的迁移演绎（与原文整理明确区分标注）。

### 变更（Kit 主项目更名联动：README 双语 roster 与 TODO T1 路径引用更新）

- **为什么改**：Kit 主项目由 PersonalAssistantAgent 更名为 ExecutiveAssistantAgent（Title「总经理助理」定名后的名字对齐，详见该项目 CHANGELOG）——本项目 README roster 表与 TODO T1 背景里指向该项目 `docs/` 的路径引用随源更名，不改则指路失效。
- **改了什么**：`README.md`（roster 行 Kit 仓库名，英文职称 GM's assistant 顺带统一为 Executive assistant）；`README_cn.md`（roster 行 Kit 仓库名）；`TODO.md` T1 背景两处路径（task-pool-bidding-sop.md / bid-templates.md），时间戳随正文改写同步更新至 2026-08-23 23:19。


### 新增（项目立项：投标转化率优化 Agent BidOptimizerAgent）

- **为什么建**：用户确定任务池投标路线后，小组分工细化——Kit 找单、Justin 保障合同收款、缺一个专职把「投出去的标」变成「中得了的单」的转化率优化角色。新建专职 agent 负责提案工程、漏斗追踪、中标率分析、报价策略测试。
- **建了什么**：完整脚手架——`README.md` / `README_cn.md`（中英双语，含拟人名 Hopkins 出典：Claude Hopkins《科学的广告》）、`assets/logo.svg`（橙绿渐变 🎯 主题）、`VERSION`（0.1.0）、`LICENSE.md`（MIT）、`CHANGELOG.md`、`.gitignore`、`.claude/`（CLAUDE.md 角色定义 + settings 三件套）。`TODO.md` 首批待办（T1～T5）从 2026-08-23 接单路线讨论与电鸭渠道调研转写而来。同日用户裁定团队重组为五小组，本项目归任务池投标小组（漏斗上游，Justin 把守下游）。
