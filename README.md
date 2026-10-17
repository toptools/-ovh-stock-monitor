# OVH / Kimsufi / So you Start 独服库存 + 低价监控 v3

无第三方依赖，Python 3.8+。不需要 OVH API Key，不会自动下单。

1. 复制 `.env.example` 为 `.env`，填邮箱（Gmail 用"应用专用密码"）和/或 Telegram。
2. 看当前有货最便宜的机器：`python3 ovh_monitor.py --top`
3. 测试通知：`python3 ovh_monitor.py --test-notify`
4. 推荐：常驻抢补货（像 alerts.ovh 一样）
   `nohup python3 ovh_monitor.py --loop 120 >> monitor.log 2>&1 &`
   或者 cron 每 5 分钟：`*/5 * * * * cd /path/ovh-monitor && python3 ovh_monitor.py >> monitor.log 2>&1`
   每天 / 每周也行，但 10 刀机器补货往往几分钟到几小时就没了。

提醒条件（都要求 1 小时内可交付的现货）：
- 价格 <= 阈值 且是新补货 / 新上架
- 目录带促销
- 比上次降价
价格按具体配置算（基础价 + 内存 + 硬盘 + 带宽加价），欧区为不含税价。
建议在家里电脑或自己的服务器上跑，原作者提醒高频请求别从 VPS 发。

v3 新增（参考 buy_ovh）：型号/硬盘/内存正则过滤、可选加增值税、年付/两年付折算月价、首月合计（月租+安装费）、新型号上架提醒。
