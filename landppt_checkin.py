#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 20 9 * * *
new Env('LandPPT 签到')

环境变量：
  LANDPPT_COOKIE      必填。cloud.landppt.com 的登录 Cookie，
                      仅需 session_id=xxx 这一项即可（从浏览器 Cookie 复制）。
  LANDPPT_PRIVACY_MODE 日志和通知中是否对用户名脱敏，默认为 true。
  TG_NOTIFY_CONFIG    可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                      配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN  是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT        单次请求超时秒数，默认为 20（所有任务共用）。

站点为服务端渲染 + FastAPI 后端，登录态通过 session Cookie 维持。
签到每日 08:00 重置，奖励为 50~200 随机积分。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests
import urllib3

from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
)


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://cloud.landppt.com"
CURRENT_USER_PATH = "/api/auth/me"
CHECKIN_STATUS_PATH = "/credits/api/checkin-status"
CHECKIN_PATH = "/credits/api/checkin"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass
class CheckinResult:
    success: bool
    status: str
    message: str
    username: str = ""
    details: dict[str, str] = field(default_factory=dict)


class LandpptCheckinClient:
    """使用 session Cookie 调用 LandPPT 签到接口。"""

    def __init__(self, cookie: str, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": cookie,
                "User-Agent": DEFAULT_USER_AGENT,
            }
        )

    def checkin(self) -> CheckinResult:
        try:
            username = self._fetch_username()
            status_payload = self._fetch_checkin_status()
            if not status_payload.get("enabled", True):
                return CheckinResult(
                    success=False,
                    status="签到未开放",
                    message="站点未开启每日签到功能",
                    username=username,
                )

            if status_payload.get("already_checked_in"):
                details = {
                    "今日已获积分": str(status_payload.get("today_reward", "-")),
                    "签到日期": str(status_payload.get("today", "-")),
                }
                return CheckinResult(
                    success=True,
                    status="今日已签到",
                    message="当前账号今日已完成签到",
                    username=username,
                    details=details,
                )

            reward_preview = str(status_payload.get("reward_preview", "-"))
            checkin_payload = self._submit_checkin()
            if not checkin_payload.get("success"):
                raise ValueError(str(checkin_payload.get("message", "签到失败")))

            reward = checkin_payload.get("today_reward", "-")
            details = {
                "获得积分": str(reward),
                "当前余额": str(checkin_payload.get("new_balance", "-")),
                "奖励区间": reward_preview,
            }
            return CheckinResult(
                success=True,
                status="签到成功",
                message=str(checkin_payload.get("message", f"获得 {reward} 积分")),
                username=username,
                details=details,
            )

        except requests.Timeout:
            return self._failure_result("请求超时", "网络请求超过设定时间")
        except requests.ConnectionError:
            return self._failure_result("连接失败", "无法连接目标站点")
        except requests.RequestException as error:
            return self._failure_result("网络错误", describe_request_error(error))
        except ValueError as error:
            return self._failure_result("执行失败", str(error))
        except Exception as error:
            return self._failure_result(
                "执行异常",
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.session.close()

    def _fetch_username(self) -> str:
        payload = self._get_json(CURRENT_USER_PATH)
        user = payload.get("user")
        if isinstance(user, dict):
            return str(user.get("username", "") or "")
        return ""

    def _fetch_checkin_status(self) -> dict[str, Any]:
        return self._get_json(CHECKIN_STATUS_PATH)

    def _submit_checkin(self) -> dict[str, Any]:
        return self._post_json(CHECKIN_PATH)

    def _get_json(self, path: str) -> dict[str, Any]:
        response = self.session.get(
            f"{BASE_URL}{path}",
            timeout=self.timeout_seconds,
            verify=False,
        )
        return self._parse_response(response)

    def _post_json(self, path: str) -> dict[str, Any]:
        response = self.session.post(
            f"{BASE_URL}{path}",
            timeout=self.timeout_seconds,
            verify=False,
        )
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(
                f"接口返回非 JSON（HTTP {response.status_code}）",
            ) from error

        if response.status_code in (401, 403):
            raise ValueError("Cookie 已失效，请更新 LANDPPT_COOKIE")
        if response.status_code != 200:
            detail = payload.get("detail", "请求失败")
            raise ValueError(f"HTTP {response.status_code}：{detail}")
        return payload

    @staticmethod
    def _failure_result(status: str, message: str) -> CheckinResult:
        return CheckinResult(success=False, status=status, message=message)


def mask_username(username: str) -> str:
    if not username:
        return "未知账号"
    if len(username) <= 1:
        return "*"
    if len(username) == 2:
        return f"{username[0]}*"
    return f"{username[0]}***{username[-1]}"


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def send_system_notification(title: str, content: str) -> bool:
    qinglong_api: Any = getattr(builtins, "QLAPI", None)
    if qinglong_api is None:
        print("[通知] 当前不是青龙任务运行环境，跳过面板系统通知")
        return False

    try:
        response = qinglong_api.systemNotify({"title": title, "content": content})
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


def build_notification_content(result: CheckinResult) -> tuple[str, str, str]:
    status_icon = "✅" if result.success else "❌"
    title = f"LandPPT 签到 {status_icon}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_lines = [
        "<b>LandPPT 每日签到</b>",
        f"{status_icon} <b>{escape_html_text(result.status)}</b>",
        f"• 账号：{escape_html_text(result.username or '未知')}",
        f"• 说明：{escape_html_text(result.message)}",
    ]
    plain_lines = [
        "LandPPT 每日签到",
        f"{status_icon} {result.status}",
        f"• 账号：{result.username or '未知'}",
        f"• 说明：{result.message}",
    ]
    for detail_key, detail_value in result.details.items():
        html_lines.append(f"• {escape_html_text(detail_key)}：{html_code(detail_value)}")
        plain_lines.append(f"• {detail_key}：{detail_value}")
    html_lines.append(f"• 时间：{html_code(execution_time)}")
    plain_lines.append(f"• 时间：{execution_time}")

    return title, "\n".join(html_lines), "\n".join(plain_lines)


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


def main() -> int:
    print(f"==== LandPPT 签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    cookie = os.getenv("LANDPPT_COOKIE", "").strip()
    if not cookie:
        print("[配置错误] 未配置 LANDPPT_COOKIE 环境变量")
        return 1

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    timeout_seconds = runtime_settings.request_timeout_seconds
    privacy_mode = read_boolean_environment("LANDPPT_PRIVACY_MODE", True)

    apply_startup_random_delay("LandPPT 签到", runtime_settings, has_work=True)

    client = LandpptCheckinClient(cookie, timeout_seconds)
    result = client.checkin()
    if privacy_mode and result.username:
        result.username = mask_username(result.username)

    result_marker = "成功" if result.success else "失败"
    print(f"[{result_marker}] {result.status}")
    print(f"  说明：{result.message}")
    for detail_key, detail_value in result.details.items():
        print(f"  {detail_key}：{detail_value}")

    title, html_content, plain_content = build_notification_content(result)
    print("\n==== LandPPT 签到汇总 ====")
    print(plain_content)

    send_notifications(title, html_content, plain_content, timeout_seconds)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
