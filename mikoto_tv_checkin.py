#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 20 9 * * *
new Env('美琴台签到')

环境变量：
  MIKOTO_TV_ACCOUNTS      必填。账号和密码，格式为“账号|密码”，多账号换行分隔。
  MIKOTO_TV_NOTIFY        是否发送通知，默认为 true。
  MIKOTO_TV_PRIVACY_MODE  日志和通知中是否对账号脱敏，默认为 true。
  TG_NOTIFY_CONFIG        可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                          配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN      是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX   随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT            单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY      多账号之间的等待秒数，默认为 3（所有任务共用）。

“美琴台”是“一之濑美琴的电视台”的任务简称。登录成功后会把
twilight_session 等会话 Cookie 保存到
/ql/data/scripts_data/mikoto_tv_cookies.json。后续运行优先验证并复用本地
Cookie，失效后自动清理并回退账号密码登录。

脚本先读取当前签到状态。今日已签到时不重复提交；未签到时调用签到接口，
随后重新读取状态复核结果。通知优先使用 Telegram HTML 直发；失败或未配置
时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urljoin

import requests

from comm.cookie_store import CookieStore
from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)


BASE_URL = "https://embymb.ichinosekotomi.com/"
API_BASE_URL = urljoin(BASE_URL, "api/v1/")
LOGIN_ENDPOINT = "auth/login"
CURRENT_USER_ENDPOINT = "auth/me"
SIGNIN_STATE_ENDPOINT = "signin/me"
SIGNIN_ENDPOINT = "signin"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
COOKIE_STORE = CookieStore("mikoto_tv_cookies")


@dataclass(frozen=True)
class AccountCredential:
    username: str
    password: str


@dataclass(frozen=True)
class AuthenticationResult:
    method: str
    signin_state: dict[str, Any]


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    authentication_method: str = "未登录"
    details: dict[str, str] = field(default_factory=dict)


class MikotoTvCheckinClient:
    """复用 Cookie 会话登录美琴台并完成每日签到。"""

    def __init__(
        self,
        credential: AccountCredential,
        timeout_seconds: float,
    ) -> None:
        self.credential = credential
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
                "Origin": BASE_URL.rstrip("/"),
                "Referer": urljoin(BASE_URL, "score"),
                "User-Agent": USER_AGENT,
                "X-Twilight-Client": "webui",
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = mask_identifier(self.credential.username)
        authentication_method = "未登录"

        try:
            authentication = self._authenticate()
            authentication_method = authentication.method
            signin_state = authentication.signin_state

            if signin_state.get("today_signed") is True:
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=True,
                    status="今日已签到",
                    message="无需重复签到",
                    authentication_method=authentication_method,
                    details=build_signin_details(signin_state),
                )

            if signin_state.get("today_signed") is not False:
                raise ValueError("签到状态响应缺少 today_signed 字段")

            signin_data = self._request_data(
                "POST",
                SIGNIN_ENDPOINT,
                json_body={},
            )
            refreshed_state = self._fetch_signin_state()
            if refreshed_state.get("today_signed") is not True:
                raise ValueError("签到接口返回成功，但状态接口仍显示未签到")

            self._save_current_cookie()
            details = build_signin_details(refreshed_state)
            reward = extract_signin_reward(signin_data)
            if reward is not None:
                details["reward"] = format_number(reward)

            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                success=True,
                status="签到成功",
                message=(
                    f"获得 {format_number(reward)} {currency_name(refreshed_state)}"
                    if reward is not None
                    else "签到状态已复核"
                ),
                authentication_method=authentication_method,
                details=details,
            )
        except requests.Timeout:
            failure_message = "网络请求超时"
        except requests.ConnectionError:
            failure_message = "无法连接美琴台"
        except requests.RequestException as error:
            failure_message = f"网络请求失败：{describe_request_error(error)}"
        except ValueError as error:
            failure_message = str(error)
        except Exception as error:
            failure_message = f"未预期异常：{type(error).__name__}: {error}"
        finally:
            self.session.close()

        return CheckinResult(
            account_number=account_number,
            account_label=account_label,
            success=False,
            status="签到失败",
            message=failure_message,
            authentication_method=authentication_method,
        )

    def _authenticate(self) -> AuthenticationResult:
        stored_cookie = COOKIE_STORE.read(self.credential.username)
        if stored_cookie:
            try:
                load_cookie_header(self.session, stored_cookie)
            except ValueError as error:
                print(f"[会话] 本地 Cookie 格式无效：{error}")
                COOKIE_STORE.remove(self.credential.username)
            else:
                if self._is_session_valid():
                    signin_state = self._fetch_signin_state()
                    self._save_current_cookie()
                    print("[会话] 本地 Cookie 有效")
                    return AuthenticationResult("本地 Cookie", signin_state)
                print("[会话] 本地 Cookie 已失效，回退账号密码登录")
                COOKIE_STORE.remove(self.credential.username)

        self.session.cookies.clear()
        self._login_with_password()
        if not self._is_session_valid():
            raise ValueError("账号密码登录后未建立有效会话")
        signin_state = self._fetch_signin_state()
        self._save_current_cookie()
        print("[登录] 账号密码登录成功，已保存 Cookie 会话")
        return AuthenticationResult("账号密码", signin_state)

    def _login_with_password(self) -> None:
        login_data = self._request_data(
            "POST",
            LOGIN_ENDPOINT,
            json_body={
                "username": self.credential.username,
                "password": self.credential.password,
            },
        )
        login_token = login_data.get("token")
        has_session_cookie = any(
            cookie.name == "twilight_session"
            for cookie in self.session.cookies
        )
        if not login_token and not has_session_cookie:
            raise ValueError("登录响应未建立令牌或 Cookie 会话")

    def _is_session_valid(self) -> bool:
        try:
            self._request_data("GET", CURRENT_USER_ENDPOINT)
            return True
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in {401, 403}:
                return False
            raise

    def _fetch_signin_state(self) -> dict[str, Any]:
        signin_state = self._request_data("GET", SIGNIN_STATE_ENDPOINT)
        if "today_signed" not in signin_state:
            raise ValueError("签到状态响应缺少 today_signed 字段")
        return signin_state

    def _request_data(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.session.request(
            method,
            urljoin(API_BASE_URL, endpoint),
            json=json_body,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            error_message = extract_api_error_message(response)
            print(
                f"[请求] {method} /api/v1/{endpoint} 失败："
                f"HTTP {response.status_code} {error_message}",
            )
            response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(f"/api/v1/{endpoint} 未返回有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError(f"/api/v1/{endpoint} 返回的数据结构无效")
        if payload.get("success") is not True:
            message = str(payload.get("message") or "接口返回业务失败")
            raise ValueError(f"/api/v1/{endpoint}：{message}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"/api/v1/{endpoint} 响应缺少 data")
        return data

    def _save_current_cookie(self) -> None:
        cookie_header = export_session_cookie_header(self.session)
        if cookie_header:
            COOKIE_STORE.write(self.credential.username, cookie_header)


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    parsed_cookie = SimpleCookie()
    try:
        parsed_cookie.load(cookie_header)
    except Exception as error:
        raise ValueError("Cookie 无法解析") from error
    if not parsed_cookie:
        raise ValueError("Cookie 未包含有效字段")
    session.cookies.clear()
    for cookie_name, cookie_value in parsed_cookie.items():
        session.cookies.set(cookie_name, cookie_value.value)


def export_session_cookie_header(session: requests.Session) -> str:
    cookie_values: dict[str, str] = {}
    for cookie in session.cookies:
        cookie_values[cookie.name] = cookie.value
    return "; ".join(
        f"{cookie_name}={cookie_value}"
        for cookie_name, cookie_value in cookie_values.items()
    )


def build_signin_details(signin_state: dict[str, Any]) -> dict[str, str]:
    details: dict[str, str] = {}
    detail_fields = (
        ("current_points", "balance"),
        ("current_streak", "current_streak"),
        ("longest_streak", "longest_streak"),
    )
    for source_field, detail_field in detail_fields:
        field_value = read_number(signin_state.get(source_field))
        if field_value is not None:
            details[detail_field] = format_number(field_value)
    details["currency"] = currency_name(signin_state)
    return details


def extract_signin_reward(signin_data: dict[str, Any]) -> float | None:
    for field_name in ("total_today", "points", "daily_points"):
        reward = read_number(signin_data.get(field_name))
        if reward is not None:
            return reward
    return None


def currency_name(signin_state: dict[str, Any]) -> str:
    value = signin_state.get("currency_name")
    return str(value).strip() if value else "小兔"


def read_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def extract_api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for field_name in ("message", "error", "detail", "code"):
        field_value = payload.get(field_name)
        if isinstance(field_value, (str, int)) and str(field_value).strip():
            return str(field_value).strip()
    return ""


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def parse_accounts(raw_accounts: str) -> tuple[list[AccountCredential], list[str]]:
    credentials: list[AccountCredential] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        account_line = raw_line.strip()
        if not account_line:
            continue
        if "|" not in account_line:
            configuration_errors.append(
                f"第 {line_number} 行格式错误，应为“账号|密码”",
            )
            continue

        username, password = account_line.split("|", maxsplit=1)
        username = username.strip()
        password = password.strip()
        if not username or not password:
            configuration_errors.append(f"第 {line_number} 行账号或密码为空")
            continue
        credentials.append(AccountCredential(username=username, password=password))

    return credentials, configuration_errors


def mask_identifier(identifier: str) -> str:
    local_part, separator, domain_part = identifier.partition("@")
    if len(local_part) <= 1:
        masked_local_part = "*"
    elif len(local_part) == 2:
        masked_local_part = f"{local_part[0]}*"
    else:
        masked_local_part = f"{local_part[0]}***{local_part[-1]}"
    return (
        f"{masked_local_part}{separator}{domain_part}"
        if separator
        else masked_local_part
    )


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


def build_notification_content(
    results: list[CheckinResult],
    configuration_errors: list[str],
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"美琴台签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail_labels = {
        "balance": "小兔余额",
        "current_streak": "连续签到",
        "longest_streak": "最长连续",
        "currency": "积分名称",
        "reward": "本次奖励",
    }

    html_sections = [
        "<b>美琴台每日签到</b>",
        "\n".join(
            [
                "<b>执行概览</b>",
                f"• 成功：{html_code(successful_count)}",
                f"• 失败：{html_code(failed_count)}",
                f"• 时间：{html_code(execution_time)}",
            ]
        ),
    ]
    plain_sections = [
        "美琴台每日签到",
        "\n".join(
            [
                "执行概览",
                f"• 成功：{successful_count}",
                f"• 失败：{failed_count}",
                f"• 时间：{execution_time}",
            ]
        ),
    ]

    for result in results:
        result_icon = "✅" if result.success else "❌"
        html_lines = [
            f"{result_icon} <b>账号 {result.account_number} · "
            f"{escape_html_text(result.account_label)}</b>",
            f"• 状态：<b>{escape_html_text(result.status)}</b>",
            f"• 说明：{escape_html_text(result.message)}",
            f"• 会话：{escape_html_text(result.authentication_method)}",
        ]
        plain_lines = [
            f"{result_icon} 账号 {result.account_number} · {result.account_label}",
            f"• 状态：{result.status}",
            f"• 说明：{result.message}",
            f"• 会话：{result.authentication_method}",
        ]
        for detail_key, detail_value in result.details.items():
            detail_label = detail_labels.get(detail_key, detail_key)
            html_lines.append(
                f"• {escape_html_text(detail_label)}：{html_code(detail_value)}",
            )
            plain_lines.append(f"• {detail_label}：{detail_value}")
        html_sections.append("\n".join(html_lines))
        plain_sections.append("\n".join(plain_lines))

    if configuration_errors:
        html_sections.append(
            "\n".join(
                ["<b>配置提示</b>"]
                + [f"• {escape_html_text(error)}" for error in configuration_errors]
            )
        )
        plain_sections.append(
            "\n".join(["配置提示"] + [f"• {error}" for error in configuration_errors])
        )

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
    return bot_token, chat_id, (api_host or "https://api.telegram.org").rstrip("/")


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
        print(f"[通知] Telegram HTML 直发失败：{response_data.get('description', '未知错误')}")
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


def print_result(result: CheckinResult) -> None:
    result_marker = "成功" if result.success else "失败"
    print(f"[{result_marker}] 账号 {result.account_number}（{result.account_label}）")
    print(f"  状态：{result.status}")
    print(f"  说明：{result.message}")
    print(f"  会话：{result.authentication_method}")
    for detail_key, detail_value in result.details.items():
        print(f"  {detail_key}：{detail_value}")


def main() -> int:
    print(f"==== 美琴台签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    credentials, configuration_errors = parse_accounts(
        os.getenv("MIKOTO_TV_ACCOUNTS", ""),
    )
    if not credentials and not configuration_errors:
        configuration_errors.append("未配置 MIKOTO_TV_ACCOUNTS 环境变量")
    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    privacy_mode = read_boolean_environment("MIKOTO_TV_PRIVACY_MODE", True)
    apply_startup_random_delay(
        "美琴台签到",
        runtime_settings,
        has_work=bool(credentials),
    )

    results: list[CheckinResult] = []
    for account_index, credential in enumerate(credentials, start=1):
        account_label = (
            mask_identifier(credential.username)
            if privacy_mode
            else credential.username
        )
        print(f"\n---- 账号 {account_index}（{account_label}）开始 ----")
        client = MikotoTvCheckinClient(
            credential,
            runtime_settings.request_timeout_seconds,
        )
        result = client.checkin(account_index)
        result.account_label = account_label
        results.append(result)
        print_result(result)
        wait_between_accounts(
            account_index,
            len(credentials),
            runtime_settings.account_delay_seconds,
        )

    title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
    )
    print("\n==== 美琴台签到汇总 ====")
    print(plain_content)
    if read_boolean_environment("MIKOTO_TV_NOTIFY", True):
        send_notifications(
            title,
            html_content,
            plain_content,
            runtime_settings.request_timeout_seconds,
        )
    else:
        print("[通知] MIKOTO_TV_NOTIFY 已关闭，跳过通知")

    all_succeeded = bool(results) and all(result.success for result in results)
    return 0 if all_succeeded and not configuration_errors else 1


if __name__ == "__main__":
    sys.exit(main())
