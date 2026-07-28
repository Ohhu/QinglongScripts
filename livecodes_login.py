#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 5 9 * * *
new Env('直播源论坛每日登录')

环境变量：
  LIVECODES_ACCOUNTS    必填。每行一个账号，支持以下格式：
                         用户名|密码
                         用户名|密码|Cookie
                         纯 Cookie
                        Cookie 优先；失效后，配置了账号密码才会回退登录。
  LIVECODES_NOTIFY      是否发送通知，默认为 true。
  LIVECODES_PRIVACY_MODE 日志和通知中是否对用户名脱敏，默认为 true。
  TG_NOTIFY_CONFIG      可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                        配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN    是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT          单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY    多账号之间的等待秒数，默认为 3（所有任务共用）。

账密登录成功后会把 Cookie 持久化到青龙数据目录，后续运行优先复用，
避免每次都重新提交账号密码。
"""

from comm.discuz_login_common import DiscuzSiteConfiguration, execute_site_login


SITE_CONFIGURATION = DiscuzSiteConfiguration(
    site_key="livecodes",
    site_name="直播源论坛",
    task_name="直播源论坛每日登录",
    base_url="https://bbs.livecodes.vip/",
    verification_path="/",
    accounts_environment_name="LIVECODES_ACCOUNTS",
    notify_environment_name="LIVECODES_NOTIFY",
    privacy_environment_name="LIVECODES_PRIVACY_MODE",
    connection_error_message="无法连接直播源论坛",
)


if __name__ == "__main__":
    execute_site_login(SITE_CONFIGURATION)
