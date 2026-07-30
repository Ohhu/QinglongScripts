#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 25 9 * * *
new Env('RainFlow签到')

环境变量：
  RAINFLOW_TOKENS       必填。每行一个 token，格式如下：
                        备注|Token        带备注，便于日志区分
                        Token             纯 token，自动以哈希尾号做备注
                        Token 获取方式：浏览器登录 https://platform.rainflowtb.com
                        后，DevTools -> Application -> Local Storage 中复制
                        localapi_user_token 的值。
  RAINFLOW_BASE_URL     可选。站点地址，默认 https://platform.rainflowtb.com。
  RAINFLOW_NOTIFY       是否发送通知，默认为 true。
  RAINFLOW_PRIVACY_MODE 日志和通知中是否对账号备注脱敏，默认为 true。
  RAINFLOW_USER_AGENT   可选。覆盖默认浏览器 User-Agent。
  TG_NOTIFY_CONFIG      可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                        配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN    是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT          单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY    多账号之间的等待秒数，默认为 3（所有任务共用）。

环境变量中的 token 过期后，需要重新从浏览器复制并更新 RAINFLOW_TOKENS。

脚本先调用 GET /user/api/checkin 查询签到状态，已签到时不重复提交；
未签到时调用 POST /user/api/checkin 执行签到并复核状态。
通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import hashlib
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


DEFAULT_BASE_URL = "https://platform.rainflowtb.com"
CHECKIN_ENDPOINT = "/user/api/checkin"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class AccountConfiguration:
    label: str
    token: str


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    authentication_method: str = "未验证"
    details: dict[str, str] = field(default_factory=dict)


class RainFlowCheckinClient:
    """使用 x-user-token 请求头认证并完成 LocalAPI 平台每日签到。"""

    def __init__(
        self,
        configuration: AccountConfiguration,
        base_url: str,
        timeout_seconds: float,
        user_agent: str,
    ) -> None:
        self.configuration = configuration
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": self.base_url,
                "User-Agent": user_agent,
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = self.configuration.label
        authentication_method = "未验证"

        try:
            token, authentication_method, status_payload = self._authenticate()
            if is_already_checked_in(status_payload):
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=True,
                    status="今日已签到",
                    message="无需重复签到",
                    authentication_method=authentication_method,
                    details=build_checkin_details(status_payload),
                )

            checkin_payload = self._request_json("POST", token)
            refreshed_payload = self._fetch_checkin_status(token)
            if not is_already_checked_in(refreshed_payload):
                raise ValueError("签到接口调用后状态仍显示未签到")

            details = build_checkin_details(refreshed_payload)
            reward_message = extract_reward_message(checkin_payload)
            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                success=True,
                status="签到成功",
                message=reward_message or "签到状态已复核",
                authentication_method=authentication_method,
                details=details,
            )
        except requests.Timeout:
            failure_message = "网络请求超过设定时间"
        except requests.ConnectionError:
            failure_message = "无法连接目标站点"
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

    def _authenticate(self) -> tuple[str, str, dict[str, Any]]:
        status_payload = self._try_fetch_checkin_status(self.configuration.token)
        if status_payload is None:
            raise ValueError(
                "环境变量 Token 已失效，请重新从浏览器 localStorage "
                "复制 localapi_user_token 并更新 RAINFLOW_TOKENS",
            )
        return self.configuration.token, "环境变量 Token", status_payload

    def _try_fetch_checkin_status(self, token: str) -> dict[str, Any] | None:
        try:
            return self._fetch_checkin_status(token)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in {401, 403}:
                return None
            raise

    def _fetch_checkin_status(self, token: str) -> dict[str, Any]:
        return self._request_json("GET", token)

    def _request_json(self, method: str, token: str) -> dict[str, Any]:
        response = self.session.request(
            method,
            urljoin(f"{self.base_url}/", CHECKIN_ENDPOINT.lstrip("/")),
            headers={"x-user-token": token},
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            error_message = extract_api_error_message(response)
            print(
                f"[请求] {method} {CHECKIN_ENDPOINT} 失败："
                f"HTTP {response.status_code} {error_message}",
            )
            response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(f"{CHECKIN_ENDPOINT} 未返回有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError(f"{CHECKIN_ENDPOINT} 返回的数据结构无效")
        return payload


def is_already_checked_in(status_payload: dict[str, Any]) -> bool:
    for field_name in ("checked_in", "checkedIn", "is_checked_in", "has_checked_in", "today_checked"):
        field_value = status_payload.get(field_name)
        if isinstance(field_value, bool):
            return field_value
    return False


def build_checkin_details(status_payload: dict[str, Any]) -> dict[str, str]:
    details: dict[str, str] = {}
    field_labels = {
        "continuous_days": "连续签到",
        "continuousCheckinDays": "连续签到",
        "checkin_days": "累计签到",
        "total_checkin_days": "累计签到",
        "balance": "余额",
        "quota": "额度",
        "used_quota": "已用额度",
        "reward": "签到奖励",
        "last_checkin_time": "上次签到",
        "lastCheckinTime": "上次签到",
        "today_reward": "今日奖励",
    }
    for field_name, field_label in field_labels.items():
        field_value = status_payload.get(field_name)
        if isinstance(field_value, bool) or field_value is None:
            continue
        if isinstance(field_value, (int, float)):
            details[field_label] = format_detail_value(field_name, field_value)
        elif isinstance(field_value, str) and field_value.strip():
            details[field_label] = field_value.strip()
    return details


def format_detail_value(field_name: str, value: float) -> str:
    if "quota" in field_name or field_name == "balance":
        return f"{value / 500000:g} 美元" if value >= 500000 else str(value)
    if "days" in field_name:
        return f"{int(value)} 天"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def extract_reward_message(checkin_payload: dict[str, Any]) -> str:
    for field_name in ("message", "msg", "reward_message"):
        field_value = checkin_payload.get(field_name)
        if isinstance(field_value, str) and field_value.strip():
            return field_value.strip()
    reward = checkin_payload.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool):
        return f"获得奖励 {format_detail_value('reward', reward)}"
    return ""


def extract_api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error_value = payload.get("error")
    if isinstance(error_value, dict):
        error_value = error_value.get("message")
    if isinstance(error_value, str) and error_value.strip():
        return error_value.strip()
    for field_name in ("message", "detail"):
        field_value = payload.get(field_name)
        if isinstance(field_value, str) and field_value.strip():
            return field_value.strip()
    return ""


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


def parse_accounts(raw_accounts: str) -> tuple[list[AccountConfiguration], list[str]]:
    accounts: list[AccountConfiguration] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        account_line = raw_line.strip()
        if not account_line:
            continue

        if "|" in account_line:
            label, token = account_line.split("|", maxsplit=1)
            label = label.strip()
            token = token.strip()
        else:
            label = ""
            token = account_line

        if not token:
            configuration_errors.append(f"第 {line_number} 行 Token 为空")
            continue

        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not label:
            label = f"token:{token_digest[:12]}"
        accounts.append(AccountConfiguration(label=label, token=token))

    return accounts, configuration_errors


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
    title = f"RainFlow签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_sections = [
        "<b>RainFlow 每日签到</b>",
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
        "RainFlow 每日签到",
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
        for detail_name, detail_value in result.details.items():
            html_lines.append(
                f"• {escape_html_text(detail_name)}：{html_code(detail_value)}",
            )
            plain_lines.append(f"• {detail_name}：{detail_value}")
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
    for detail_name, detail_value in result.details.items():
        print(f"  {detail_name}：{detail_value}")


def main() -> int:
    print(f"==== RainFlow签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    accounts, configuration_errors = parse_accounts(os.getenv("RAINFLOW_TOKENS", ""))
    if not accounts and not configuration_errors:
        configuration_errors.append("未配置 RAINFLOW_TOKENS 环境变量")
    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    base_url = os.getenv("RAINFLOW_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    user_agent = os.getenv("RAINFLOW_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
    privacy_mode = read_boolean_environment("RAINFLOW_PRIVACY_MODE", True)
    apply_startup_random_delay(
        "RainFlow签到",
        runtime_settings,
        has_work=bool(accounts),
    )

    results: list[CheckinResult] = []
    for account_index, account in enumerate(accounts, start=1):
        display_label = (
            mask_identifier(account.label) if privacy_mode else account.label
        )
        print(f"\n---- 账号 {account_index}（{display_label}）开始 ----")
        client = RainFlowCheckinClient(
            account,
            base_url,
            runtime_settings.request_timeout_seconds,
            user_agent,
        )
        result = client.checkin(account_index)
        result.account_label = display_label
        results.append(result)
        print_result(result)
        wait_between_accounts(
            account_index,
            len(accounts),
            runtime_settings.account_delay_seconds,
        )

    title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
    )
    print("\n==== RainFlow签到汇总 ====")
    print(plain_content)
    if read_boolean_environment("RAINFLOW_NOTIFY", True):
        send_notifications(
            title,
            html_content,
            plain_content,
            runtime_settings.request_timeout_seconds,
        )
    else:
        print("[通知] RAINFLOW_NOTIFY 已关闭，跳过通知")

    all_succeeded = bool(results) and all(result.success for result in results)
    return 0 if all_succeeded and not configuration_errors else 1


if __name__ == "__main__":
    sys.exit(main())
