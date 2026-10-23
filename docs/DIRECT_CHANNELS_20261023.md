# OPS 直接渠道连接 — 2026-10-23

## 已确认的用户要求

OPS独立实现PMS能力，不依赖Hostaway账户、Hostaway API或Hostaway作为转发服务。Hostaway只作为产品功能参照。

用户已确认：目前没有Booking.com Connectivity测试machine account，也没有Airbnb API项目资格。真实接入按待审批处理；本地独立模块继续开发，不将普通房东登录当作供应方API授权。

现有历史Hostaway研究/影子适配器不删除，不在应用启动、定时同步或本批新功能中调用。新增代码没有从该适配器导入模块。无需配置任何HOSTAWAY变量即可运行、创建直订、设置本地费率、生成报表和查看直接渠道页。

## 官方资料核对

本次另用Exa进行四次直接渠道检索，共18条搜索结果，优先采用官方页面。没有提交渠道申请、接受合作协议、配置真实凭证或进行任何真实外部接口调用。

| 渠道 | 官方要求/能力 | OPS当前状态 | 下一步所需资料 |
|---|---|---|---|
| Booking.com | Connectivity Partner入驻、房源连接类型、端点权限及适用认证；当前使用machine account短期token | 官方token鉴权和只读房型发现已实现并完成合成契约测试；尚未实接。非Reservations读写、非消息连接、非价格发布 | Connectivity Provider状态、测试machine account、测试酒店ID、房源连接授权及房型/费率映射 |
| Airbnb | 根据API Program授予Scopes，项目要求包含协议和安全审核；不能把普通房东账号视作PMS API授权 | 未实现猜测或非公开接口；等待获批项目文档和凭证 | API项目申请状态、获准的scope/端点/回调规范、测试房源 |
| Expedia Group | Provider流程包含接洽、相关要求核对、开发测试及soft launch；其公开说明不接受单体物业直接连接 | 待评估OPS是否按软件提供商资格申请；尚未接通 | Connectivity Provider资格、测试环境与官方接口授权 |
| 其他渠道（如Ctrip/Vrbo） | 必须分别取得供应方接口规范，不能将其他渠道或消费端搜索API当PMS连接 | 保留现有iCal日历能力；正式接口接入待核对 | 对应供应方合作入口、授权范围、测试账户和房源映射 |

### 当前Booking.com实现范围

- `POST https://connectivity-authentication.booking.com/token-based-authentication/exchange`：发送machine account的client_id/client_secret，读取jwt。
- `POST https://supply-xml.booking.com/hotels/xml/rooms`：XML请求只包含hotel_id，返回active room types。这是官方定义的读取操作，虽使用HTTP POST但不修改房源。
- token在进程内复用，提前刷新；固定官方HTTPS主机，不跟随重定向；响应大小上限；房型返回范围检查；外部异常只映射为受控错误码。
- 没有提交价格、库存、消息、订单确认或付款；没有读取付款卡信息。
- 只读连接测试不会把“房型读取成功”标为“所有渠道功能可用”。

### 凭证配置（尚未配置真实值）

在获批测试账户及端点权限后，由部署环境的秘密配置功能设置以下变量：

```
BOOKING_CONNECTIVITY_APPROVED=1
BOOKING_CONNECTIVITY_CLIENT_ID=<Booking.com测试machine-account client ID>
BOOKING_CONNECTIVITY_CLIENT_SECRET=<对应测试secret>
```

`APPROVED=1`只是运维登记，不替代渠道方批准。页面只有在上述登记和凭证存在时才显示主动只读测试表单；不会自动调用。测试/生产账户必须分开。密钥不进入源码、交付包、页面或日志。不要把密钥发在聊天里。

## 直接连接的实现顺序

1. 获得供应方资格及测试文档，建立外部property/room/rate-plan与OPS物理资源映射。
2. 订单新增/修改/取消的事件持久化、确认机制、游标/补拉与重放；每次只产生一次业务效果。
3. 根据渠道要求建立库存/价格/限制发布队列；最后一间并发、共享资源、日期时区、失败结果和断线补偿通过测试后才实际发布。
4. 接收/发送消息、评价和内容更新，按渠道实际scope逐项上线。没有回执时不得显示已发送/已同步。

本地共享库存已补充：显式物理资源与房型容量、直订/来源同步/来源取消复核/封锁/申请批准统一数据库锁，以及按完整容量导出iCal。它保证经过这些本地路径的并发，不能锁定外部OTA正在接受的订单。尚未完成实体房间分配、跨渠道原子预留或外部实时回执，不能声称已全面防止跨平台超卖。

## 官方来源

- [Booking.com Connectivity概述](https://developers.booking.com/connectivity/docs)
- [认证方式及旧凭证方案淘汰说明](https://developers.booking.com/connectivity/docs/authentication)
- [Token认证规范](https://developers.booking.com/connectivity/docs/token-based-authentication)
- [端点、房源连接和认证权限](https://developers.booking.com/connectivity/docs/managing-endpoint-access)
- [房型读取请求和响应](https://developers.booking.com/connectivity/docs/room-type-and-rate-plan-management/managing-room-types)
- [ARI价格、库存和限制](https://developers.booking.com/connectivity/docs/ari)
- [Airbnb API项目要求](https://www.airbnb.com/help/article/3418)
- [Expedia Lodging Connectivity](https://developers.expediagroup.com/supply/lodging)
