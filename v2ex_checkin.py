#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 0 9 * * *
new Env('V2EX签到')

环境变量：
  V2EX_COOKIE          必填。浏览器 F12 抓取的完整 Cookie，多账号换行分隔。
  V2EX_PRIVACY_MODE    通知中是否对用户名脱敏，默认为 true。
  TASK_RANDOM_SIGNIN   是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT         单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY   多账号间间隔秒数，默认为 3（所有任务共用）。
  TG_NOTIFY_CONFIG     可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                       配置后使用 HTML 直发，失败回退青龙纯文本通知。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests


BASE_URL = "https://www.v2ex.com/"
MISSION_DAILY_URL = "https://www.v2ex.com/mission/daily"
MISSION_DAILY_REDEEM_URL = "https://www.v2ex.com/mission/daily/redeem"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_RANDOM_DELAY_MAX_SECONDS = 3600.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# onclick 形如：location.href = '/mission/daily/redeem?once=1234567'
REDEEM_ONCE_PATTERN = re.compile(r"/mission/daily/redeem\?once=(\d+)")


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


class V2exCheckinClient:
    """用 Cookie 访问 V2EX 每日签到页，提取 once 并完成兑换。"""

    def __init__(self, cookie: str, timeout_seconds: float) -> None:
        self.cookie = cookie
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": USER_AGENT,
                "Referer": BASE_URL,
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = "Cookie 账号"
        try:
            daily_page_text = self._fetch_daily_page()
            if self._is_login_required(daily_page_text):
                return self._failure_result(
                    account_number,
                    account_label,
                    "Cookie 已失效，请重新抓取",
                )

            once_token = self._extract_once_token(daily_page_text)
            if once_token is None:
                # 没有按钮也没有 once，多半是今天已经签过
                if self._already_checked_in(daily_page_text):
                    details = self._extract_user_details(daily_page_text)
                    account_label = details.get("username") or account_label
                    return CheckinResult(
                        account_number=account_number,
                        account_label=account_label,
                        success=True,
                        status="今日已签到",
                        message="每日登录奖励已领取",
                        details=details,
                    )
                return self._failure_result(
                    account_number,
                    account_label,
                    "未找到签到按钮，页面结构可能已变化",
                )

            # 兑换 once；V2EX 此接口返回 302 或简洁 JSON，响应体不一定含成功字样
            self._submit_redeem(once_token)

            # 以兑换后重新拉取的签到页为准，判断是否真的领到
            refreshed_page_text = self._fetch_daily_page()
            if self._already_checked_in(refreshed_page_text):
                details = self._extract_user_details(refreshed_page_text)
                account_label = details.get("username") or account_label
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=True,
                    status="签到成功",
                    message="每日登录奖励已领取",
                    details=details,
                )

            return self._failure_result(
                account_number,
                account_label,
                "兑换 once 后签到页仍未显示已领取",
            )

        except requests.Timeout:
            return self._failure_result(account_number, account_label, "网络请求超时")
        except requests.ConnectionError:
            return self._failure_result(
                account_number,
                account_label,
                "无法连接 V2EX",
            )
        except requests.RequestException as error:
            return self._failure_result(
                account_number,
                account_label,
                f"网络请求失败：{describe_request_error(error)}",
            )
        except Exception as error:
            return self._failure_result(
                account_number,
                account_label,
                f"未预期异常：{type(error).__name__}: {error}",
            )
        finally:
            self.session.close()

    def _fetch_daily_page(self) -> str:
        response = self.session.get(
            MISSION_DAILY_URL,
            headers={"Cookie": self.cookie},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def _submit_redeem(self, once_token: str) -> str:
        response = self.session.get(
            MISSION_DAILY_REDEEM_URL,
            params={"once": once_token},
            headers={
                "Cookie": self.cookie,
                "Referer": MISSION_DAILY_URL,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _is_login_required(page_text: str) -> bool:
        # 未登录会被重定向到 /signin，页面里会出现这个标识
        return "You need to sign in first" in page_text or "/signin" in page_text

    @staticmethod
    def _extract_once_token(page_text: str) -> str | None:
        match = REDEEM_ONCE_PATTERN.search(page_text)
        return match.group(1) if match else None

    @staticmethod
    def _already_checked_in(page_text: str) -> bool:
        return "每日登录奖励已领取" in page_text

    @staticmethod
    def _extract_user_details(page_text: str) -> dict[str, str]:
        details: dict[str, str] = {}

        username_match = re.search(
            r'<a\s+href="/member/[^"]+"\s+class="top"[^>]*>([^<]+)</a>',
            page_text,
        )
        if username_match:
            details["username"] = username_match.group(1).strip()

        balance_match = re.search(r'每日登录奖励[^<]*?(\d+)\s*铜币', page_text)
        if balance_match:
            details["balance"] = balance_match.group(1)

        days_match = re.search(r'连续登录\s*(\d+)\s*天', page_text)
        if days_match:
            details["total_days"] = days_match.group(1)

        return details

    @staticmethod
    def _failure_result(
        account_number: int,
        account_label: str,
        message: str,
    ) -> CheckinResult:
        return CheckinResult(
            account_number=account_number,
            account_label=account_label,
            success=False,
            status="失败",
            message=message,
        )


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def parse_cookies(raw_cookies: str) -> list[str]:
    cookies = [
        cookie.strip()
        for cookie in raw_cookies.splitlines()
        if cookie.strip()
    ]
    return cookies


def mask_identifier(identifier: str) -> str:
    if not identifier:
        return "Cookie 账号"
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


def read_positive_float_environment(
    variable_name: str,
    default_value: float,
) -> float:
    raw_value = os.getenv(variable_name, str(default_value)).strip()
    try:
        parsed_value = float(raw_value)
    except ValueError:
        print(f"[配置] {variable_name}={raw_value!r} 无效，使用默认值 {default_value}")
        return default_value

    if parsed_value < 0:
        print(f"[配置] {variable_name} 不能为负数，使用默认值 {default_value}")
        return default_value
    return parsed_value


def read_boolean_environment(variable_name: str, default_value: bool) -> bool:
    raw_value = os.getenv(variable_name)
    if raw_value is None:
        return default_value
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def format_time_remaining(seconds: float) -> str:
    """将秒数格式化为人类可读的时长描述。"""
    total_seconds = int(seconds)
    if total_seconds <= 0:
        return "立即执行"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes > 0:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def wait_with_countdown(delay_seconds: float, task_name: str) -> None:
    """随机延迟等待，期间定期打印剩余时间倒计时。"""
    remaining = delay_seconds
    while remaining > 0:
        print(f"{task_name} 倒计时：{format_time_remaining(remaining)}")
        sleep_seconds = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(sleep_seconds)
        remaining -= sleep_seconds


def send_system_notification(title: str, content: str) -> bool:
    qinglong_api: Any = getattr(builtins, "QLAPI", None)
    if qinglong_api is None:
        print("[通知] 当前不是青龙任务运行环境，跳过面板系统通知")
        return False

    try:
        response = qinglong_api.systemNotify(
            {
                "title": title,
                "content": content,
            }
        )
    except Exception as error:
        print(f"[通知] 面板系统通知调用失败：{type(error).__name__}: {error}")
        return False

    if isinstance(response, dict) and response.get("code") != 200:
        print(f"[通知] 面板系统通知发送失败：{response}")
        return False

    print(f"[通知] 面板系统通知调用完成：{response}")
    return True


def escape_html_text(value: Any) -> str:
    return html.escape(str(value).replace("\n", " "), quote=False)


def html_code(value: Any) -> str:
    return f"<code>{escape_html_text(value)}</code>"


def build_notification_content(
    results: list[CheckinResult],
    configuration_errors: list[str],
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"V2EX签到 {status_icon} {successful_count}/{len(results)}"

    detail_label_map = {
        "username": "用户名",
        "balance": "铜币余额",
        "total_days": "连续签到",
    }

    html_sections = [
        "💬 <b>V2EX 每日签到</b>",
        "\n".join(
            [
                "📊 <b>执行概览</b>",
                f"• 成功：{html_code(successful_count)}",
                f"• 失败：{html_code(failed_count)}",
                f"• 时间：{html_code(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}",
            ]
        ),
    ]
    plain_sections = [
        "💬 V2EX 每日签到",
        "\n".join(
            [
                "📊 执行概览",
                f"• 成功：{successful_count}",
                f"• 失败：{failed_count}",
                f"• 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ]
        ),
    ]

    for result in results:
        result_icon = "✅" if result.success else "❌"
        html_account_lines = [
            f"{result_icon} <b>账号 {result.account_number} · "
            f"{escape_html_text(result.account_label)}</b>",
            f"• 状态：<b>{escape_html_text(result.status)}</b>",
            f"• 说明：{escape_html_text(result.message)}",
        ]
        plain_account_lines = [
            f"{result_icon} 账号 {result.account_number} · {result.account_label}",
            f"• 状态：{result.status}",
            f"• 说明：{result.message}",
        ]
        for detail_key, detail_value in result.details.items():
            detail_label = detail_label_map.get(detail_key, detail_key)
            html_account_lines.append(
                f"• {escape_html_text(detail_label)}：{html_code(detail_value)}",
            )
            plain_account_lines.append(
                f"• {detail_label}：{detail_value}",
            )
        html_sections.append("\n".join(html_account_lines))
        plain_sections.append("\n".join(plain_account_lines))

    if configuration_errors:
        html_error_lines = ["⚙️ <b>配置提示</b>"]
        html_error_lines.extend(
            f"• {escape_html_text(error)}" for error in configuration_errors
        )
        html_sections.append("\n".join(html_error_lines))

        plain_error_lines = ["⚙️ 配置提示"]
        plain_error_lines.extend(f"• {error}" for error in configuration_errors)
        plain_sections.append("\n".join(plain_error_lines))

    return title, "\n\n".join(html_sections), "\n\n".join(plain_sections)


def read_telegram_notify_configuration() -> tuple[str, str, str] | None:
    raw_configuration = os.getenv("TG_NOTIFY_CONFIG", "").strip()
    if not raw_configuration:
        return None

    configuration_parts = raw_configuration.split("|", maxsplit=2)
    if len(configuration_parts) != 3:
        print("[通知] TG_NOTIFY_CONFIG 格式错误，应为 BotToken|ChatID|APIHost")
        return None

    bot_token, chat_id, api_host = (part.strip() for part in configuration_parts)
    if not bot_token or not chat_id:
        print("[通知] TG_NOTIFY_CONFIG 缺少 BotToken 或 ChatID")
        return None

    api_host = (api_host or "https://api.telegram.org").rstrip("/")
    return bot_token, chat_id, api_host


def send_telegram_html_notification(content: str, timeout_seconds: float) -> bool:
    notify_configuration = read_telegram_notify_configuration()
    if notify_configuration is None:
        return False

    bot_token, chat_id, api_host = notify_configuration
    try:
        response = requests.post(
            f"{api_host}/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": content,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as error:
        print(f"[通知] Telegram HTML 直发网络错误：{describe_request_error(error)}")
        return False

    try:
        response_data = response.json()
    except ValueError:
        print(f"[通知] Telegram HTML 直发返回异常：HTTP {response.status_code}")
        return False

    if response.status_code != 200 or not response_data.get("ok"):
        error_description = str(response_data.get("description", "未知错误"))
        print(f"[通知] Telegram HTML 直发失败：{error_description}")
        return False

    message_result = response_data.get("result", {})
    if isinstance(message_result, dict):
        print(
            "[通知] Telegram HTML 直发成功，"
            f"message_id={message_result.get('message_id')}"
        )
    else:
        print("[通知] Telegram HTML 直发成功")
    return True


def send_notifications(
    title: str,
    html_content: str,
    plain_content: str,
    timeout_seconds: float,
) -> None:
    if send_telegram_html_notification(html_content, timeout_seconds):
        return
    print("[通知] 使用青龙纯文本通知回退")
    send_system_notification(title, plain_content)


def print_result(result: CheckinResult) -> None:
    marker = "成功" if result.success else "失败"
    print(f"[{marker}] 账号 {result.account_number}（{result.account_label}）")
    print(f"  状态：{result.status}")
    print(f"  说明：{result.message}")
    for key, value in result.details.items():
        print(f"  {key}：{value}")


def main() -> int:
    print(f"==== V2EX签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw_cookies = os.getenv("V2EX_COOKIE", "")
    privacy_mode = read_boolean_environment("V2EX_PRIVACY_MODE", True)
    cookies = parse_cookies(raw_cookies)

    configuration_errors: list[str] = []
    if not cookies:
        configuration_errors.append("未配置 V2EX_COOKIE 环境变量")
    for error in configuration_errors:
        print(f"[配置错误] {error}")

    request_timeout_seconds = read_positive_float_environment(
        "TASK_TIMEOUT",
        20.0,
    )

    # 启动前随机延迟，避免固定时间签到
    random_signin_enabled = read_boolean_environment("TASK_RANDOM_SIGNIN", True)
    if random_signin_enabled:
        max_random_delay = read_positive_float_environment(
            "TASK_RANDOM_DELAY_MAX",
            3600.0,
        )
        if max_random_delay > 0:
            delay_seconds = random.uniform(0, max_random_delay)
            print(f"🎲 随机延迟：{format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "V2EX签到")

    results: list[CheckinResult] = []
    for account_index, cookie in enumerate(cookies, start=1):
        masked_label = f"账号{account_index}"
        print(f"\n---- 账号 {account_index} 开始 ----")
        client = V2exCheckinClient(cookie, request_timeout_seconds)
        result = client.checkin(account_index)

        # 隐私脱敏
        if privacy_mode and result.details.get("username"):
            result.details["username"] = mask_identifier(result.details["username"])
            result.account_label = result.details["username"]

        results.append(result)
        print_result(result)

        has_next_account = account_index < len(cookies)
        if has_next_account:
            account_gap = read_positive_float_environment("TASK_ACCOUNT_DELAY", 3.0)
            if account_gap > 0:
                print(f"等待 {account_gap:g} 秒后处理下一个账号")
                time.sleep(account_gap)

    notification_title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
    )
    print("\n==== V2EX签到汇总 ====")
    print(plain_content)

    send_notifications(
        notification_title,
        html_content,
        plain_content,
        request_timeout_seconds,
    )

    all_succeeded = bool(results) and all(r.success for r in results)
    config_valid = not configuration_errors
    return 0 if all_succeeded and config_valid else 1


if __name__ == "__main__":
    sys.exit(main())
