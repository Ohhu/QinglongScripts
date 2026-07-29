#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 15 9 * * *
new Env('MiraiEmby签到')

环境变量：
  MIRAIEMBY_ACCOUNTS      必填。账号和密码，格式为“账号|密码”，多账号换行分隔。
  MIRAIEMBY_NOTIFY        是否发送通知，默认为 true。
  MIRAIEMBY_PRIVACY_MODE  日志和通知中是否对账号脱敏，默认为 true。
  TG_NOTIFY_CONFIG        可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                          配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN      是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX   随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT            单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY      多账号之间的等待秒数，默认为 3（所有任务共用）。

登录成功后会把 Access Token 和 Refresh Token 保存到
/ql/data/scripts_data/miraiemby_tokens.json。后续运行优先复用 Access Token；
Access Token 过期后自动刷新，刷新失败才回退账号密码登录。

脚本先读取首页签到状态。今日已签到时不重复提交；未签到时调用签到接口，
随后重新读取首页状态复核结果。通知优先使用 Telegram HTML 直发；失败或
未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)
from comm.token_store import TokenStore


BASE_URL = "https://www.miraiemby.com/"
API_BASE_URL = urljoin(BASE_URL, "api/")
LOGIN_ENDPOINT = "auth/login"
REFRESH_ENDPOINT = "auth/refresh"
HOME_ENDPOINT = "client/home"
CHECKIN_ENDPOINT = "client/checkin"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TOKEN_STORE = TokenStore("miraiemby_tokens")


@dataclass(frozen=True)
class AccountCredential:
    username: str
    password: str


@dataclass(frozen=True)
class AuthenticationResult:
    method: str
    access_token: str
    home_payload: dict[str, Any]


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    authentication_method: str = "未登录"
    details: dict[str, str] = field(default_factory=dict)


class MiraiEmbyCheckinClient:
    """复用令牌会话登录 MiraiEmby 并完成每日签到。"""

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
                "Referer": urljoin(BASE_URL, "dashboard"),
                "User-Agent": USER_AGENT,
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = mask_identifier(self.credential.username)
        authentication_method = "未登录"

        try:
            authentication = self._authenticate()
            authentication_method = authentication.method
            checkin_state = extract_checkin_state(authentication.home_payload)
            before_balance = read_number(checkin_state.get("current_balance"))

            if checkin_state.get("already_checked_in") is True:
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=True,
                    status="今日已签到",
                    message="无需重复签到",
                    authentication_method=authentication_method,
                    details=build_checkin_details(checkin_state),
                )

            if checkin_state.get("can_check_in") is not True:
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=False,
                    status="签到不可用",
                    message="首页状态显示当前不能签到",
                    authentication_method=authentication_method,
                    details=build_checkin_details(checkin_state),
                )

            checkin_payload = self._request_json(
                "POST",
                CHECKIN_ENDPOINT,
                access_token=authentication.access_token,
            )
            refreshed_home = self._fetch_home(authentication.access_token)
            refreshed_state = extract_checkin_state(refreshed_home)
            if refreshed_state.get("already_checked_in") is not True:
                raise ValueError("签到接口返回成功，但首页状态仍显示未签到")

            after_balance = read_number(refreshed_state.get("current_balance"))
            reward = calculate_reward(before_balance, after_balance)
            details = build_checkin_details(refreshed_state)
            if reward is not None:
                details["reward"] = format_number(reward)

            transaction = checkin_payload.get("transaction")
            if isinstance(transaction, dict) and transaction.get("created_at"):
                details["checkin_time"] = str(transaction["created_at"])

            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                success=True,
                status="签到成功",
                message=(
                    f"获得 {format_number(reward)} 金币"
                    if reward is not None
                    else "签到状态已复核"
                ),
                authentication_method=authentication_method,
                details=details,
            )
        except requests.Timeout:
            failure_message = "网络请求超时"
        except requests.ConnectionError:
            failure_message = "无法连接 MiraiEmby"
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
        stored_tokens = TOKEN_STORE.read(self.credential.username)
        stored_access_token = stored_tokens.get("access_token", "").strip()
        stored_refresh_token = stored_tokens.get("refresh_token", "").strip()

        if stored_access_token:
            home_payload = self._try_fetch_home(stored_access_token)
            if home_payload is not None:
                print("[会话] 本地 Access Token 有效")
                return AuthenticationResult(
                    "本地 Access Token",
                    stored_access_token,
                    home_payload,
                )
            print("[会话] 本地 Access Token 已失效")

        if stored_refresh_token:
            refreshed_tokens = self._try_refresh_tokens(stored_refresh_token)
            if refreshed_tokens is not None:
                access_token, refresh_token = refreshed_tokens
                home_payload = self._try_fetch_home(access_token)
                if home_payload is not None:
                    self._save_tokens(access_token, refresh_token)
                    print("[会话] Refresh Token 刷新成功")
                    return AuthenticationResult(
                        "Refresh Token",
                        access_token,
                        home_payload,
                    )
            print("[会话] Refresh Token 已失效，回退账号密码登录")

        TOKEN_STORE.remove(self.credential.username)
        access_token, refresh_token = self._login_with_password()
        home_payload = self._fetch_home(access_token)
        self._save_tokens(access_token, refresh_token)
        print("[登录] 账号密码登录成功，已保存令牌会话")
        return AuthenticationResult("账号密码", access_token, home_payload)

    def _login_with_password(self) -> tuple[str, str]:
        payload = self._request_json(
            "POST",
            LOGIN_ENDPOINT,
            json_body={
                "username": self.credential.username,
                "password": self.credential.password,
            },
        )
        access_token = str(payload.get("token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise ValueError("登录响应缺少 Access Token 或 Refresh Token")
        return access_token, refresh_token

    def _try_refresh_tokens(self, refresh_token: str) -> tuple[str, str] | None:
        try:
            payload = self._request_json(
                "POST",
                REFRESH_ENDPOINT,
                json_body={"refresh_token": refresh_token},
            )
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in {400, 401, 403}:
                return None
            raise

        access_token = str(payload.get("token") or "").strip()
        updated_refresh_token = str(
            payload.get("refresh_token") or refresh_token,
        ).strip()
        if not access_token:
            return None
        return access_token, updated_refresh_token

    def _try_fetch_home(self, access_token: str) -> dict[str, Any] | None:
        try:
            return self._fetch_home(access_token)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in {401, 403}:
                return None
            raise

    def _fetch_home(self, access_token: str) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            HOME_ENDPOINT,
            access_token=access_token,
        )
        extract_checkin_state(payload)
        return payload

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        response = self.session.request(
            method,
            urljoin(API_BASE_URL, endpoint),
            json=json_body,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            error_message = extract_api_error_message(response)
            print(
                f"[请求] {method} /api/{endpoint} 失败："
                f"HTTP {response.status_code} {error_message}",
            )
            response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(f"/api/{endpoint} 未返回有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError(f"/api/{endpoint} 返回的数据结构无效")
        return payload

    def _save_tokens(self, access_token: str, refresh_token: str) -> None:
        TOKEN_STORE.write(
            self.credential.username,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
        )

def extract_checkin_state(home_payload: dict[str, Any]) -> dict[str, Any]:
    checkin_state = home_payload.get("checkin")
    if not isinstance(checkin_state, dict):
        raise ValueError("首页响应缺少签到状态")
    return checkin_state


def build_checkin_details(checkin_state: dict[str, Any]) -> dict[str, str]:
    details: dict[str, str] = {}
    current_balance = read_number(checkin_state.get("current_balance"))
    reward_minimum = read_number(checkin_state.get("next_reward_min"))
    reward_maximum = read_number(checkin_state.get("next_reward_max"))
    if current_balance is not None:
        details["balance"] = format_number(current_balance)
    if reward_minimum is not None and reward_maximum is not None:
        details["reward_range"] = (
            f"{format_number(reward_minimum)}-{format_number(reward_maximum)}"
        )
    return details


def read_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def calculate_reward(
    before_balance: float | None,
    after_balance: float | None,
) -> float | None:
    if before_balance is None or after_balance is None:
        return None
    return max(0.0, after_balance - before_balance)


def format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def extract_api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for field_name in ("error", "message", "detail"):
        field_value = payload.get(field_name)
        if isinstance(field_value, str) and field_value.strip():
            return field_value.strip()
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
    title = f"MiraiEmby签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail_labels = {
        "balance": "金币余额",
        "reward_range": "奖励范围",
        "reward": "本次奖励",
        "checkin_time": "签到时间",
    }

    html_sections = [
        "<b>MiraiEmby 每日签到</b>",
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
        "MiraiEmby 每日签到",
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
    print(f"==== MiraiEmby签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    credentials, configuration_errors = parse_accounts(
        os.getenv("MIRAIEMBY_ACCOUNTS", ""),
    )
    if not credentials and not configuration_errors:
        configuration_errors.append("未配置 MIRAIEMBY_ACCOUNTS 环境变量")
    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    privacy_mode = read_boolean_environment("MIRAIEMBY_PRIVACY_MODE", True)
    apply_startup_random_delay(
        "MiraiEmby签到",
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
        client = MiraiEmbyCheckinClient(
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
    print("\n==== MiraiEmby签到汇总 ====")
    print(plain_content)
    if read_boolean_environment("MIRAIEMBY_NOTIFY", True):
        send_notifications(
            title,
            html_content,
            plain_content,
            runtime_settings.request_timeout_seconds,
        )
    else:
        print("[通知] MIRAIEMBY_NOTIFY 已关闭，跳过通知")

    all_succeeded = bool(results) and all(result.success for result in results)
    return 0 if all_succeeded and not configuration_errors else 1


if __name__ == "__main__":
    sys.exit(main())
