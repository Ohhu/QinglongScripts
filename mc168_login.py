#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 10 9 * * *
new Env('无损音乐论坛每日登录')

环境变量：
  MC168_ACCOUNTS        必填。每行一个账号，支持以下格式：
                         用户名|密码
                         用户名|密码|Cookie
                         纯 Cookie
                        Cookie 优先；失效后，配置了账号密码才会回退登录。
  OCR_KEY              账密登录遇到验证码时必填。EasyOCR 云端访问密钥
                       （console.easyocr.org 创建，eocr_ 开头），所有 OCR
                        任务共用。验证码识别失败时最多刷新重试 3 次。
  MC168_NOTIFY          是否发送通知，默认为 true。
  MC168_PRIVACY_MODE    日志和通知中是否对用户名脱敏，默认为 true。
  TG_NOTIFY_CONFIG      可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                        配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN    是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT          单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY    多账号之间的等待秒数，默认为 3（所有任务共用）。

账密登录成功后会把 Cookie 持久化到青龙数据目录，后续运行优先复用，
尽量避免重复识别登录验证码。
"""

from comm.discuz_login_common import (
    DiscuzCreditConfiguration,
    DiscuzSiteConfiguration,
    execute_site_login,
)


SITE_CONFIGURATION = DiscuzSiteConfiguration(
    site_key="mc168",
    site_name="无损音乐论坛",
    task_name="无损音乐论坛每日登录",
    base_url="https://mc168.fun/",
    verification_path="forum.php?gid=1&mobile=no",
    accounts_environment_name="MC168_ACCOUNTS",
    notify_environment_name="MC168_NOTIFY",
    privacy_environment_name="MC168_PRIVACY_MODE",
    connection_error_message="无法连接无损音乐论坛",
    credit=DiscuzCreditConfiguration(
        page_path=(
            "home.php?mod=spacecp&ac=credit&showcredit=1"
            "&inajax=1&ajaxtarget=extcreditmenu_menu"
        ),
        field_id="hcredit_2",
        display_name="金钱",
    ),
)


if __name__ == "__main__":
    execute_site_login(SITE_CONFIGURATION)
