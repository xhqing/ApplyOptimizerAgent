# TODO（Hopkins · 投标转化率优化师）

> 活跃待办清单。条目按紧急度分节，节内按记录时间排序。处理完毕移入 `TODO-archive.md`。
> 2026-10-22 状态：渠道收敛为电鸭 / Upwork / WWR；三平台抓取与匹配管道 `scripts/job_fetch.py` 已落地；事实清单（T19）与电鸭登录态通道（T16）完成并归档。
> 2026-10-23 状态：用户要求恢复 LinkedIn，渠道回到四个（电鸭 / LinkedIn / Upwork / WWR）；主动线已接回 `job_fetch.py`（T22 只剩被动线建档待做）。

## 🔴 红色紧急度

（暂无）

## 🟠 橙色紧急度

- [ ] **T10** 电鸭在途申请（outbox 定稿，单号 DY-260905-02）发出后记台账闭环（记录：2026-09-05 22:31；2026-09-05 23:44 更新：02/03 均已发出并记台账，本条收窄为「02 等微信回复期间若跨会话，由下一会话按台账 next_action 续跟」）
  **背景**：该申请走微信私聊形态，定稿文案与附件、投递路径已备在 `docs/outbox.md`（联系方式已于 2026-09-05 花 5 电量解锁）；但加上微信的时间不确定（可能跨会话、发出动作可能发生在无会话在场时）——**漏记 = 台账分母失真，T7 两周基线被污染**。
  **要做什么**：① ~~微信定稿消息发出后记台账~~（已完成：DY-260905-02 / 03 均已入账）；② outbox.md 状态已更新（三条清出）；③ 对方回复后 journal.md 记交互（雇主代号化）、台账补 connected_at（02 的已补）；④ 顺带刷新 outbox.md 的飞书云端副本（已过期）。

- [ ] **T7** 漏斗跑满两周后算基线，替换 Kit 侧 SOP 的拍脑袋数字（记录：2026-09-03 20:08；2026-09-08 20:29 更新：补期望值口径与时间成本列，SOP 已升 v0.2.2；2026-10-22 14:15 更新：渠道收敛为三平台，大厂渠道对比不再适用）
  **背景**：T1 的数据采集剩余部分。记录体系已就位（台账 `docs/ledger.csv` + 五级漏斗字段映射 + 期望值口径，见 `docs/bidding-record-sop.md` v0.2.2 第 6 节）；Kit 侧 `ExecutiveAssistantAgent/docs/task-pool-bidding-sop.md` 里的「保守中标率 3%」「每天投 15～20 标」等数字是无实测背书的假设（其平台推荐部分已被用户实测推翻，电鸭为新主渠道），需用本项目真实数据重新标定。
  **要做什么**：① 从台账第一条 `sent` 样本起算，满两周（14 个自然日）后按 SOP 第 6 节口径算五级漏斗各层转化率与整体转化率（won / (won+lost)）；② 用实测数字回写 Kit 侧 SOP 的假设数字（替换或标注「待实测」出处，与 Kit 会话确认后落地）；③ 基线数字同步进本项目模板库迭代日志，作为后续 A/B 测试的对照基线；④ 渠道复盘另按期望值口径横向对比（每投期望回报、每小时期望毛收入；一次性收入与持续性月薪折算同一时间窗），大厂渠道「低频高值」定位以此检验；存量在途单的 `hours_spent` 由用户补估一次（15 分钟档）。

- [ ] **T17** 核实 8 条超 30 天在途样本的真实状态，能确认已无推进的按 SOP 转 lost（记录：2026-10-21 12:38）
  **背景**：新工具 `scripts/ledger.py open` 实测发现 12 条在途样本里有 8 条自最后活动起已超 30 天（DY-260905-01/02/03、GW-260913-01～05），按 SOP 通用关闭规则应转 lost；但它们可能只是「有后续但没记台账」（如 DY-260905-02 计划 09-10 有腾讯会议初面），不能机械批量关单——在途不计入分母，拖着会让转化率分母偏小、渠道判断失真。
  **要做什么**：① 逐条向用户确认是否还有推进；② 已死的用 `python3 scripts/ledger.py update --id <ID> --set outcome=lost --set close_date=today --set dead_layer=<层> --set lost_reason_layer=<归因>` 关单；③ 有进展的补记 `connected_at` / `engaged_at` / 新 `next_action`；④ 关单后重跑 `ledger.py stats` 记基线。

- [ ] **T20** 三平台管道运维：Upwork 403 失败率、电鸭 profile 过期、每日定时跑（记录：2026-10-22 14:15）
  **背景**：`scripts/job_fetch.py` 首次打通三平台，但两处外部依赖会静默失效——① Upwork 走 Jina 公开搜索页，Cloudflare 偶发 403（重试后仍失败会记 failures）；② 电鸭详情依赖 `/tmp/elduck-profile` 登录态，profile 过期后详情抓不到、只剩 200 字摘要（`detail` 不是 `full`），匹配质量下降但不报错。
  **要做什么**：① 每次跑完看 failures 与 `detail != full` 的条数，连续两次异常就排查；② 电鸭 profile 过期时用 Chrome 登录一次刷新（/tmp 会被系统清理，必要时固化到项目外私有目录）；③ 用 launchd / cron 每日早晚各跑一次（早 9:30、晚 21:30），产出 `docs/jobs/`；④ 跑满两周用台账回应率校准打分权重（对齐 T7 / T9）。

- [ ] **T21** Upwork 账号注册 + Profile 建档（三平台核心渠道，用户执行；记录：2026-10-22 14:15）
  **背景**：渠道收敛后 Upwork 是三平台中唯一需要账号与 profile 的渠道；没有它，非全职线少一条主渠道。材料包 `docs/upwork-profile.md` 已备（Title 三选一 / Overview / 履历 / 作品集五条 / 费率带）；注册涉身份验证与收款，AI 不代注册。
  **要做什么**：① 注册（Work / freelancer）；② 按材料包逐字段填 profile + 作品集五条；③ ID 验证（24–48 小时）；④ 绑定 Payoneer；⑤ 拍板费率（材料包建议起步 $35–50）；⑥ 完成后移归档，投标进入日常节奏（source 记 `Upwork`，Connects 折算记 `cost_money`）。
  **完成标准**：profile 完整 + 作品集上线 + 收款绑定。

## 🟡 黄色紧急度

- [ ] **T18** 四个脚本的回归测试交由 Hopper 出题（记录：2026-10-21 12:38；2026-10-22 14:40 更新：新增 job_fetch.py）
  **背景**：`scripts/posting_guard.py` / `copy_check.py` / `ledger.py` / `job_fetch.py` 已按机械行为手工验证（隐藏块清洗、注入标记、报价红线、统计口径、台账写回均实跑通过），但用例定义权归 TestEngineerAgent（Hopper）——开发 agent 写测试文件被工具硬拦截，不绕行。
  **要做什么**：委派 Hopper 为四个脚本补 pytest 回归用例：① guard 隐藏块 / 零宽 / 指令语句 / 隐藏链接；② copy_check 首触达报价、提案放行报价、`--final` 占位符、版本号、字数、英文标点；③ ledger 转化率口径（won/(won+lost)，在途与选单层不进分母）、枚举校验、逗号挤列、update 保序、add 自动编号；④ job_fetch 的 Upwork 描述块 ↔ 链接配对、电鸭 `__NEXT_DATA__` 解析、角色 / 地域分类、全职 vs 非全职打分、`--from-jsonl` 重算。用例进 `tests/`，跑法 `python3 -m pytest tests -q`。

## 🟢 绿色紧急度

- [ ] **T12** LegalAgent（Justin）GitHub 仓库立项待建（记录：2026-09-08）
  **背景**：全局注册表 2026-09-08 口径更新——项目名已定 `LegalAgent`（原名 LegalCounselAgent）、职称「法务Agent」、纯法务直属用户（财务职能暂时空缺）；但 GitHub 上 `xhqing/LegalAgent` 仓库从未创建（`xhqing/LegalCounselAgent` 也不存在），xhqing README 与 CyberRipple org README 里的 Justin 链接目前均为死链（点开 404）。
  **要做什么**：按「新建 Agent 项目脚手架与开源约定」完整立项——`gh repo create xhqing/LegalAgent --public` + 双语 README（含 logo 与三徽章 + Visitors）+ `CLAUDE.md`（角色：纯法务——合同起草审查、收款结构设计、尽调、证据链、纠纷应对、合规备忘；直属用户、跨组服务全部小组）+ 根目录 `AGENTS.md` 软链 + LICENSE + VERSION + CHANGELOG；建仓后各处链接自然生效，无需回改。

- [ ] **T9** 跑满 10 个已判定样本后校准口径边界，SOP 升级 v0.3（记录：2026-09-03 20:08）
  **背景**：T6 的校准剩余部分。记录载体已建档启用（台账 `docs/ledger.csv`、日志 `docs/journal.md`），从下一条样本起按 SOP 全量记录。SOP 第 7 节列出四条待校准边界：①「pass / 观望不算动作」的边界是否符合体感；②订金全额退款出现频率；③30 天无推进统一转 lost 是否要按层分级；④`fit_score` 各分数段实际转化率分布。
  **要做什么**：① 期间每条样本按 11 个触发器全量记录（对话后 5 分钟内记日志，不许隔夜补记）；② 跑满 10 个已判定样本（won + lost）后逐条回答上面四个问题，把结论写进 SOP 第 7 节并升级 v0.3；③ 校准结论同步 T7 基线与 `docs/ab-test.md` 的变量清单（如 fit_score 分档被证实有效，可进入可测变量清单）。

- [ ] **T22** LinkedIn 被动线落地：profile 建档 + Open to Work + GitHub 徽章入口（用户执行；记录：2026-10-23 22:58）
  **背景**：用户 2026-10-23 要求恢复 LinkedIn（主动线拉岗已接回 `scripts/job_fetch.py`）。被动线是全组合成本结构最好的动作——一次性投入、零边际成本、价值随时间增长；原 T14 于 2026-10-22 随渠道收敛放弃，本次重新立条（原条目内容见 `TODO-archive.md` 的 T14，标记为「已更新」）。建档属用户手工操作（涉账号安全，不走自动化）。
  **要做什么**：① profile 建档到 All-star 档（头像、标题、所在地、现职位、≥2 段经历、教育、≥5 技能、≥50 人脉），关键词对齐三份现有简历方向，Featured 区挂 `github.com/xhqing` 作品集；② 开 Open to Work（仅猎头可见；连续两次不回 InMail 会被自动摘标记，每周扫一次消息）；③ **开工前截存 Analytics 快照**（资料浏览数有 90 天窗口、搜索曝光数取当周值、「被搜出的头衔」列表）——不为基线推迟动工；④ 完成后四周看曝光与关键词方向变化，**八周零猎头接触 → 降级为挂着不投入**（绝对判据）；⑤ 主动线首投按 SOP 记台账（source 记 `LinkedIn`），时区 / 签证 / US-only 限制终审看 JD；⑥ 建档完成后把 LinkedIn 入口挂上 GitHub profile README（EN/CN 双版同步改，徽章行形态、不开 open to work 横幅），履历全文不复制进 README。
