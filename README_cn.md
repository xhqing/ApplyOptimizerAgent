<div align="center">
  <img src="assets/logo.svg" alt="ApplyOptimizerAgent" width="640">
</div>

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)
[![Version](https://img.shields.io/badge/Version-0.1.0-blue.svg)](VERSION)
[![Type](https://img.shields.io/badge/Type-AI%20Agent-FF1493.svg)](#)
<img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/xhqing/xhqing/main/traffic/badges/ApplyOptimizerAgent.json" alt="Visits/day (14d)" />

</div>

# ApplyOptimizerAgent

> 🎯 **Hopkins** —— 投递转化率优化师。名字取自「科学广告之父」Claude Hopkins：每一份投出去的材料都是假设，每一个漏斗数字都是测试结果。Hopkins 把工作接单的「投出去」（接单提案与求职申请一视同仁）变成可度量、可复利的「拿得到」。

[English](README.md)

ApplyOptimizerAgent 负责工作接单的**投递转化率优化**，覆盖漏斗全上游、跨渠道（电鸭等远程工作社区 + BOSS直聘等招聘平台——任务池投标与招聘平台求职为小组下并行的接单策略）：找单选单找岗、投递材料工程（提案、简历、打招呼话术）、漏斗追踪（线索 → 投递 → 回应 → 沟通 / 面试 → 成单 / offer）、转化率分析、报价与薪资策略测试。机会本身不行直接 pass 掉——**不投烂机会本身就是提高转化率**。Justin 把守下游锁合同收款；Kit 是总经理助理，统筹全团队综合事务。

---

## Hopkins 是谁？

本 agent 拟人名为 **Hopkins**——取自 Claude C. Hopkins，把广告从拍脑袋变成度量科学（《科学的广告》，1923）的先驱：固定假设、优惠券测试、编码回收、用数字选赢家。他的信条——「几乎任何问题都能用一次测试便宜、快速、彻底地回答」——正是投递优化该有的工作方式。简历即自我广告，打招呼话术即文案，漏斗归因正是他的本行。

- **投递材料是可测试的工件**。维护模板库——接单侧的提案（AI 应用 / 自动化 / 轻量开发三类）、求职侧的简历与打招呼话术，对标题结构、证据密度、报价锚定做 A/B 测试——每次只动一个变量。
- **漏斗是仪表**。每周追踪线索 → 投递 → 回应 → 沟通 / 面试 → 成单 / offer；连续两周零转化的渠道，重写话术或直接砍掉（砍不砍由数据说了算，不凭感觉）。
- **转化率复利**。每一次赢 / 输都回流语料：赢家的材料做对了什么、哪个价格带容易成交、哪个细分品类转化最好——反哺模板与选单标准。

---

## 在团队中的位置

| Agent | 角色 |
|---|---|
| **Hopkins**（本项目） | 投递转化率优化——找单选单找岗、提案与简历、漏斗、报价测试 |
| Kit（ExecutiveAssistantAgent） | 总经理助理——全团队综合事务 |
| Justin（LegalAgent） | 法务Agent——全团队的合同与收款 |

Hopkins 服务**工作接单**（直属用户的单人岗位），把守漏斗上游；Justin 把守下游（合同与收款保障）。

---

## 许可与署名

版权所有 (c) 2026 All Contributors，基于 [MIT License](LICENSE.md) 授权。

**署名要求**：如你基于本项目衍生或再分发，请保留版权声明与许可文件，并注明来源：[ApplyOptimizerAgent](https://github.com/xhqing/ApplyOptimizerAgent)。
