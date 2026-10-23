# Hostaway → OPS 功能对标与实施状态

日期：2026-10-23（Australia/Brisbane）。本次使用 Exa 从三个方向检索 24 条结果，并读取官方功能页、使用手册目录、渠道目录及直订说明。整合之前的 44 个功能主题，补充 20 个细项，共 64 个对标条目；细项与主题存在父子关系，不能当作 64 个互不重叠的模块。此目录不是 Hostaway 所有套餐、市场插件及未公开功能的完整性保证。

**实际状态：多项独立功能已实现和测试，尚未发布到 ops.omiholiday.com；全功能对齐尚未完成。**

**用户确认的架构：OPS独立运行，不依赖Hostaway账户或API，直接申请并连接各OTA。** Hostaway文档和旧研究仅用于功能对标；旧Hostaway影子读取代码保留为历史兼容，不在启动时调用，不是任何新功能的前置依赖。

## 已做的代码变更

- 新增直订订单列表、详情、创建、改期、取消与恢复；本地库存锁、冲突检查、过期/重复/旧版本提交保护、操作人/原因/前后快照；保留渠道来源的控制权。
- 新增按日期批量价格覆盖、最短/最长入住、禁止到达/离开、停售和恢复默认；变更历史与范围版本保护。公开报价、公开日历和管理员直订使用这些本地规则。
- 清洁任务与本地直订创建/改期/取消联动；锁定或已完成任务不被静默改期。
- 新增按物业/日期的房晚与订单金额分摊报表、数据缺口提示和CSV导出。金额是既有订单金额按夜平均分摊，不代表住宿收入、银行到账或完整ADR/RevPAR；旧模型没有币种字段，明确标注AUD假设。
- 新增接入检查页，区分“未连接”“有同步错误”“缺少具名员工”等情况。
- 新增直接渠道页；实现Booking.com官方短期token认证与房型只读查询代码，已做合成契约测试，尚无真实账户连通验证。Airbnb/Expedia等待渠道资格及授权资料，没有猜测私有接口。
- 新员工角色默认选择staff，导航与新表单适配窄屏。
- 新增显式币种订单账本、费用/减免、外部收付款记录、余额、历史对账单和客人查询；重复交易参考号与超额退款拦截，更正保留凭证。没有实际扣款/退款、税务发票或自动业主结算。
- 新增自有协议文本版本、按预订固定快照、门户姓名/时间确认、撤回重发、历史与下载；未接入外部身份核验或认证签署服务，不预填实际合同条款。
- 新增显式共享资源/房型容量、整租分租联动，统一直订、来源同步和封锁的本地库存锁；满房日期才输出iCal。具体实体房间分配及跨OTA实时原子预留仍缺。
- 新增不可变检查模板、固定天数周期计划、暂停/恢复/补生成，以及任务完成一次性生成费用。没有付款或员工通知。
- 新增在线到达资料、规则快照确认、私密链接创建/撤销，以及客人与运营的门户会话、内部备注、负责人和未读状态。没有OTA/邮箱消息接入。
- 新增限时报价转待审核预订申请、审批前再验价/库存及原子建单；新增物业定价上下限。没有实际支付或自动收款。
- PostgreSQL上线快照使用新的ops_backup_20261023命名，保留旧版快照行为兼容性；新数据表为增量创建。

## 核对方法与边界

OPS核对：已查看线上Bookings、Overview、Properties、Tasks、Cleaning、Finance、Staff、Calendar、Pricing、Message Templates，并检查既有源码。无订单、无真实接口连接时，不能把“没有数据显示”判断为“没有功能”。“未发现”表示当前源码/页面中未定位，不是对不可访问的全部系统作绝对断言。

Hostaway核对：使用官方公开资料，未登录付费账户逐屏复现。官方资料有功能不等于其Public API、所有地区或所有渠道都支持同样操作。渠道合作资格、支付、门锁、短信、签署、保险、市场数据及模型服务需分别确认，不能用占位页面或模拟成功冒充实接。

## 实施顺序

1. 订单与库存基础：本地直订/按日规则/共享容量已补充；后续实体分房、币种及直接渠道事件幂等和回执。
2. 运营闭环：门户会话、任务模板/周期、在线到达资料已补充；后续OTA/邮箱统一收件箱、细粒度权限、通知和外勤附件。
3. 交易闭环：报价审核转单已补充；协议版本与本地确认已补充；后续完整费用明细、支付、押金、退款、发票、加购及外部身份签署服务。
4. 经营与生态：业主权限/结算、会计、评价、动态定价、AI与硬件。

每个条目的完成标准均需包括可见页面、真实持久化、适当权限、错误/重放测试，以及涉及外部提供商时的真实回执。目录状态“部分已有”不计为Hostaway全量同等完成。

## 对标矩阵

| ID | 能力 | 当前状态 | 优先级 | 验收条件与剩余工作 | 依赖 | 官方依据 |
|---|---|---|---|---|---|---|
| F01 | 房源主档 | 部分已有 | P1 | 房源详情包含设施、政策、媒体及版本；渠道字段映射可预览并验证回执 | 房源数据与渠道接口 | [官方资料](https://www.hostaway.com/features/channel-manager/) |
| F02 | 房型与实体房间 | 本批共享容量已实现，实体分房待做 | P0 | 显式物理资源、房型容量、每单消耗量及最后一间并发拦截已验证；单笔预订的具体实体房间分配、迁移仍待做 | 库存模型迁移 | [官方资料](https://support.hostaway.com/hc/en-us/articles/6563444445595-Listings-Multi-Unit-Listings) |
| F03 | 跨房源关联 | 本批本地共享库存已实现 | P1 | 整租绑定多个房间资源，分租绑定各自资源；创建/取消/改期/来源同步/封锁统一重算冲突；渠道实时回写未接 | 共享库存基础 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F04 | 渠道管理 | 未实接 | P0 | 直接连接OTA，实现订单/改期/取消、房价、限制和库存双向同步；验证渠道回执与断线补偿 | 各OTA官方接入资格/授权；不依赖Hostaway | [官方资料](https://www.hostaway.com/features/channel-manager/) |
| F05 | 统一日历 | 部分已有，本批增强 | P0 | 已提供按房源日期查看订单、封锁和停售信息；横向多房源网格与拖动改期仍待做 | 真实订单；当前仅本地库存 | [官方资料](https://www.hostaway.com/features/channel-manager/) |
| F06 | 可售限制 | 本批本地直订已实现 | P1 | 按夜设置最短/最长入住、禁止到达/离开、停售；公开报价与管理员直订均拒绝违规日期 | 渠道回写尚未连接 | [官方资料](https://support.hostaway.com/hc/en-us/articles/6052802939163-Calendar-Manage-dates) |
| F07 | 封锁与业主自住 | 部分已有 | P0 | 既有封锁保留；补业主自住申请、审批、原因与撤销记录 | 业主身份及资源模型 | [官方资料](https://www.hostaway.com/features/owner-portal/) |
| F08 | 预订生命周期 | 本地生命周期已实现，渠道待实接 | P0 | 直订创建/改期/取消/恢复、审计、清洁联动及共享库存锁通过测试；外部订单以来源为准并保留冲突记录 | 渠道事件与共享库存 | [官方资料](https://api.hostaway.com/documentation) |
| F09 | 询价与报价 | 本批报价转待审核订单已实现 | P1 | 签名限时报价、再次检查价格/限制/容量、客人提交、运营批准或拒绝、批准原子建单及去重；尚无付款/短时库存保留 | 直订订单及支付流程 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F10 | 渠道费率差异 | 未发现实现 | P1 | 按渠道设置加价/减价、字段差异；发布预览与渠道结果可追踪 | 渠道定价写入权限 | [官方资料](https://www.hostaway.com/features/channel-manager/) |
| F11 | 统一收件箱 | 本批门户会话已实现，OTA/邮箱待接 | P1 | 门户客人问题/运营回复、内部备注、负责人、状态、逐用户未读、分页、私密链接撤销已实现；OTA及邮件统一会话和通知仍待做 | 渠道消息API/邮箱接收接口 | [官方资料](https://www.hostaway.com/features/communication/) |
| F12 | 消息自动化 | 部分已有 | P0 | 保留生命周期模板与发送去重；补自定义条件、时差、改期重排和预览 | 统一消息适配器 | [官方资料](https://www.hostaway.com/features/communication/) |
| F13 | 翻译与多语言 | 部分已有 | P1 | 中英文模板已有；补更多语言、保留原文的翻译与人工修订 | 翻译服务与语言策略 | [官方资料](https://www.hostaway.com/features/communication/) |
| F14 | AI 回复 | 未发现实现 | P1 | 根据房源事实生成可核对的回复草稿；事实缺失时转人工 | 模型提供商与知识库 | [官方资料](https://www.hostaway.com/features/hostaway-ai/) |
| F15 | 情绪与升级 | 未验证 | P1 | 负面/紧急会话分类、分配、时限、升级和人工接管可追踪 | 真实消息源与分类评估 | [官方资料](https://www.hostaway.com/features/hostaway-ai/) |
| F16 | SMS / WhatsApp | 未发现实现 | P2 | SMS/WhatsApp双向会话、模板、语言、失败回执与退订状态 | 服务商账户、号码及模板授权 | [官方资料](https://www.hostaway.com/features/communication/) |
| F17 | 评价自动化 | 部分已有 | P2 | 已有退房后评价邀请模板；补渠道评价导入、回复、排程与渠道规则 | 渠道评价API | [官方资料](https://www.hostaway.com/features/communication/) |
| F18 | 客人门户 | 本批增加入住、会话、协议及账务查询 | P1 | 每单门户、在线到达资料、规则版本确认、会话、协议确认和外部收付款账务查询已实现；在线付款、加购及第三方身份核验待接 | 支付/签署/加购模块 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360036731213-Reservations-Guest-Portal) |
| F19 | 在线入住 | 本批本地已实现 | P2 | 每单到达资料、人数/格式校验、提交历史、日期/规则变化后重提、私密链接访问隔离；不等同实际入住或身份核验 | 表单存储与保留规则 | [官方资料](https://support.hostaway.com/hc/en-us/articles/17027928676123-Listings-Manage-the-Online-Check-In-Form) |
| F20 | 租赁协议 | 本批本地协议确认已实现，身份核验待接 | P2 | 运营自有文本版本、按单不可变快照、姓名/时间/文档摘要确认、撤回/重发、历史与下载已实现；不是第三方身份核验或签署服务认证 | 电子签署方案与业务协议 | [官方资料](https://support.hostaway.com/hc/en-us/articles/6512180199451-Rental-Agreements-Overview-and-FAQs) |
| F21 | 加购服务 | 未发现实现 | P2 | 可选服务、库存/时段、价格/税费、购买回执；付款后创建准备任务 | 支付服务及服务履约 | [官方资料](https://support.hostaway.com/hc/en-us/articles/6112311144219-Listings-Manage-Prices-Fees-and-Discounts) |
| F22 | 直订网站 | 部分已有 | P1 | 已有房源发布字段及公开查询API；完整官网搜索和预订转化需单独实测 | 官网代码与域名部署 | [官方资料](https://www.hostaway.com/features/direct-booking/) |
| F23 | 直订结账 | 未发现完整实现 | P0 | 结账接受报价、短时库存保留、付款回调和确认订单；重复回调不重复建单 | 支付账户与库存事务 | [官方资料](https://www.hostaway.com/features/direct-booking/) |
| F24 | 优惠与营销 | 部分已有 | P2 | 已有停用的再营销模板；补优惠券限制、有效期、使用次数、营销同意和退订 | 直订/消息模块 | [官方资料](https://www.hostaway.com/features/direct-booking/) |
| F25 | 付款自动化 | 未发现实现 | P1 | 费用计划、分期、尾款、支付回调与取消后的停止扣款 | 支付账户与业务扣款规则 | [官方资料](https://www.hostaway.com/features/operations/) |
| F26 | 付款异常 | 本批人工外部收付款记录已实现，支付事件待接 | P1 | 明确币种、费用/减免/外部收款/外部退款、未付/部分付/余额状态及更正历史已实现；未接提供商成功/失败/争议事件 | 支付事件接口 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360047273714-Settings-Notifications) |
| F27 | 发票与收据 | 本批固定对账单和外部收付款记录已实现 | P2 | 按订单固定历史对账单、逐笔收付款记录、快照摘要校验和作废标记已实现；税务发票、电子发票及税率编号合规待做 | 费用模型与发票编号策略 | [官方资料](https://www.hostaway.com/features/operations/) |
| F28 | 清洁派单 | 部分已有，本批联动 | P0 | 已有退房派单；本批直订改期保留锁定/完成任务并提示复核，取消保留任务历史 | 后续需员工通知与外勤权限 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360036506093-Tasks-Create-Manage-Assign-Tasks) |
| F29 | 检查清单 | 本批增加不可变模板 | P1 | 必做/选做检查项、不可变可复用模板及任务/计划快照已实现；附件照片和清洁外勤门户联动待做 | 文件存储与模板模型 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360036506093-Tasks-Create-Manage-Assign-Tasks) |
| F30 | 维修和周期任务 | 本批增加固定日间隔周期计划 | P1 | 周期派单、暂停/恢复、补生成、去重和历史保留已实现；月历规则、通知与可靠外部调度待做 | 可靠的后台执行环境 | [官方资料](https://support.hostaway.com/hc/en-us/articles/18693666619163-Tasks-Recurring-Tasks) |
| F31 | 工时与任务成本 | 本批任务完成入费用已实现，工时待做 | P2 | 已知任务成本在完成事务中只生成一次关联费用，保留历史；实际工时、材料明细及付款结算仍待做 | 成本规则与财务模型 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360036506093-Tasks-Create-Manage-Assign-Tasks) |
| F32 | 智能门锁 | 未实接 | P2 | 门锁提供商状态、按入住期生成门码、改期更新、取消撤销与失败复核 | 门锁服务商、设备及API | [官方资料](https://www.hostaway.com/features/smart-locks/) |
| F33 | 收入及费用报表 | 部分已有，本批新增报表 | P0 | 保留对账/费用；新增逐物业已订/封锁/可售房晚、跨月订单金额分摊与CSV；完整会计总账仍缺 | 币种、费用及收入确认口径 | [官方资料](https://www.hostaway.com/features/analytics-and-reporting/) |
| F34 | 业主结算单 | 部分已有 | P0 | 已有草稿/复核/定稿锁定；补业主模板、修订版本、多币种与付款关联 | 完整财务数据和会计口径 | [官方资料](https://www.hostaway.com/features/owner-statement/) |
| F35 | 业主门户 | 部分已有 | P1 | 已有业主门户；补多房源授权、自住、可见字段和报表访问矩阵 | 业主实体与细粒度授权 | [官方资料](https://www.hostaway.com/features/owner-portal/) |
| F36 | 动态定价 | 本批增加按日覆盖和定价上下限 | P2 | 本地规则、按日覆盖、按物业最低/最高价已统一用于公开报价及日历；市场数据、竞品和自动渠道发布未接 | 市场定价数据供应商 | [官方资料](https://www.hostaway.com/features/dynamic-pricing/) |
| F37 | 经营指标 | 部分已有，本批新增报表 | P1 | 提供本地已订房晚比例与渠道房晚；真实入住率、ADR/RevPAR需住宿费、币种和实际入住数据 | 计量定义与完整订单金额 | [官方资料](https://www.hostaway.com/features/analytics-and-reporting/) |
| F38 | 质量及评价洞察 | 未发现实现 | P2 | 评价主题/投诉关联房源和工单；跟踪同类问题复发和修复后的变化 | 渠道评价、反馈与质量指标 | [官方资料](https://www.hostaway.com/features/hostaway-ai/) |
| F39 | 用户与权限 | 部分已有，本批修正默认角色 | P0 | 员工角色已有，新增账户默认staff；房源范围、动作/财务字段权限与所有接口覆盖仍缺 | 权限模型及回归测试 | [官方资料](https://www.hostaway.com/features/operations/) |
| F40 | 移动与通知 | 部分已有 | P1 | 已有响应式页面与清洁门户；补可靠通知、深链接、未读和外勤任务体验 | 通知提供商及移动端策略 | [官方资料](https://support.hostaway.com/hc/en-us/articles/360047273714-Settings-Notifications) |
| F41 | API / Webhooks | 部分已有，直连基础新增 | P0 | 历史影子层保留但非依赖；新增Booking.com官方token鉴权与只读房型发现（契约测试，未实接）；各OTA webhook接收、幂等、失序和重放待做 | 各OTA官方接入权限与事件源；无需Hostaway账户/API | [官方资料](https://api.hostaway.com/documentation) |
| F42 | 合作生态 | 未形成通用生态 | P2 | 按能力暴露可替换适配器；每个会计/门锁/支付提供商单独通过契约测试 | 对应第三方账户和接口 | [官方资料](https://www.hostaway.com/features/) |
| F43 | AI CoHost | 部分已有 | P1 | 已有4个只读OPS工具；完整经营问答、证据、草稿变更与权限内操作未接 | 模型、工具授权与评估 | [官方资料](https://www.hostaway.com/features/ai-cohost/) |
| F44 | 保险等服务 | 未实接 | P3 | 按适用地区显示真实承保/理赔/服务状态；不将普通表单当有效保单 | 服务商资格、地区及业务合同 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F45 | 按日期批量房价 | 本批本地已实现 | P1 | 批量设置和恢复默认；回显覆盖范围，保存历史并拒绝旧版本覆盖 | 渠道发布未连接 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F46 | 定价上下限 | 本批本地已实现 | P1 | 按物业最低/最高价、签名审计、旧版本拒绝；最终报价夹取，覆盖价在界面明确为上下限前值 | 本地定价策略 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F47 | 客人付款状态和余额 | 本批本地人工账务已实现 | P0 | 显式选择币种、精确小数费用/减免、外部收款/退款、余额和客人查询已实现；不替代支付提供商确认、汇兑或跨订单拆分 | 支付流水和费用明细 | [官方资料](https://support.hostaway.com/hc/en-us/articles/14677408175131-Guest-Payments-Overview-Management) |
| F48 | 分期与动态尾款 | 未发现实现 | P1 | 支持预订时比例付款及入住前剩余尾款；改价/线下付款后重新计算 | 支付授权与调度 | [官方资料](https://support.hostaway.com/hc/en-us/articles/14677408175131-Guest-Payments-Overview-Management) |
| F49 | 押金预授权与释放 | 未发现实现 | P1 | 按入住日期授权、到期释放、取消清理，显示提供商回执 | 支持预授权的支付提供商 | [官方资料](https://support.hostaway.com/hc/en-us/articles/14677408175131-Guest-Payments-Overview-Management) |
| F50 | 退款与拒付处理 | 本批外部退款记账已实现，实际退款/拒付待接 | P1 | 退款绑定本单有效收款、累计不超过原收款、更正保留原凭证及并发保护已实现；实际转账、拒付与提供商回执待接 | 支付提供商与退款流程 | [官方资料](https://support.hostaway.com/hc/en-us/articles/14677408175131-Guest-Payments-Overview-Management) |
| F51 | 电子发票与地区接口 | 未发现实现 | P3 | 按适用地区验证发票字段、编号、税率及提供商结果 | 地区需求与电子发票服务 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F52 | QuickBooks会计集成 | 未实接 | P2 | 费用/订单/收款映射、外部ID去重、同步错误和复核记录 | QuickBooks账户与会计科目 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F53 | 双因素认证 | 未发现实现 | P1 | 具名员工登录支持2FA设置和恢复；会话与账户状态一致 | 身份验证与恢复策略 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F54 | 用户组与财务字段权限 | 未发现完整实现 | P0 | 用户组/个人权限覆盖房源、页面、动作、API、导出及财务公式 | 授权模型改造 | [官方资料](https://support.hostaway.com/hc/en-us/articles/52115466343963-Financial-Reporting-User-Permissions) |
| F55 | Google Vacation Rentals分发 | 未实接 | P2 | 将合格房源分发到Google，验证价格/可用性/直订链接 | Google接入资格或渠道合作方 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000588654-Channels-and-Integrations) |
| F56 | Expedia与Marriott等渠道 | 未实接 | P2 | 逐渠道核对房源、预订、消息、费率支持边界并完成实接 | 各渠道账户和接入资格 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000588654-Channels-and-Integrations) |
| F57 | 自定义报表视图 | 未发现完整实现 | P1 | 可保存列、日期口径、物业/渠道过滤和导出；不泄露受限财务字段 | 报表与字段权限 | [官方资料](https://support.hostaway.com/hc/en-us/articles/1260801381250-Financial-Reporting-Rental-Activity) |
| F58 | 直订网站模板/多语言/SEO | 未验证 | P2 | 官网编辑、房源分类、搜索、品牌、多语言、SEO及分析配置可验收 | 官网代码与分析配置 | [官方资料](https://get.hostaway.com/direct-booking/) |
| F59 | 移动应用 | 未验证 | P2 | 区分响应式网页与原生移动应用；核心日历/任务/消息流程在目标设备验收 | 移动交付范围与推送服务 | [官方资料](https://www.hostaway.com/features/) |
| F60 | 检查清单模板 | 本批本地已实现 | P1 | 不可变模板复制到任务和周期计划形成快照；必做项未完成不得结单；已有任务历史保持原样 | 任务/清洁检查项模型 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F61 | 任务完成生成费用 | 本批本地已实现 | P1 | 完成事务只生成一条关联费用；重开/重放不重复计费；入账后成本冻结，旧费用删除接口拒绝删除关联项 | 任务成本与财务对账 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F62 | 渠道评价管理与回复 | 未实接 | P2 | 导入各渠道评价、回复并验证成功状态；遵守渠道可改/可撤期限 | 渠道评价API权限 | [官方资料](https://support.hostaway.com/hc/en-us/categories/360000589513-Using-Hostaway) |
| F63 | 促销码与自定义加购费 | 未发现实现 | P2 | 适用日期/渠道/次数、税费和独立付款；购买服务后产生履约任务 | 直订/费用/支付模块 | [官方资料](https://support.hostaway.com/hc/en-us/articles/19918681033755-Listings-Manage-Custom-Fees-and-Upsells) |
| F64 | Webhook重试与乱序补偿 | 未发现完整实现 | P0 | 直接接入各渠道的正式事件；重复、乱序、依赖未到和断线时可恢复，不重复触发消息/费用 | 持久化事件队列和各OTA真实事件样本；Hostaway资料仅作功能参照 | [官方资料](https://api.hostaway.com/documentation) |
