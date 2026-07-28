#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 15 8 * * *
new Env('MT论坛签到')

环境变量：
  mt              账号和密码，格式为“账号|密码”，多账号使用换行分隔。
  MT_ACCOUNTS     与 mt 格式相同；设置后优先于 mt。
  MT_NOTIFY       是否发送青龙面板系统通知，默认为 true。
  TASK_RANDOM_SIGNIN 是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT    单次网络请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY 多账号之间的等待秒数，默认为 3（所有任务共用）。
  TG_NOTIFY_CONFIG 可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                   配置后使用 HTML 直发，失败回退青龙纯文本通知。

账号密码登录成功后，会把会话保存到 /ql/data/scripts_data/mt_cookies.json，
后续运行优先复用，失效后自动回退账号密码。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from comm.cookie_store import CookieStore
from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)


BASE_URL = "https://bbs.binmt.cc/"
LOGIN_FORM_URL = urljoin(
    BASE_URL,
    "member.php?mod=logging&action=login&infloat=yes&handlekey=login"
    "&inajax=1&ajaxtarget=fwin_content_login",
)
SIGN_PAGE_URL = urljoin(BASE_URL, "k_misign-sign.html")
SIGN_ENDPOINT_URL = urljoin(BASE_URL, "plugin.php")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
COOKIE_STORE = CookieStore("mt_cookies")


class LoginFormParser(HTMLParser):
    """Extract the active Discuz login form without relying on tag spacing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.hidden_fields: dict[str, str] = {}
        self._inside_login_form = False

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        attribute_map = dict(attributes)
        if tag == "form" and attribute_map.get("name") == "login":
            self._inside_login_form = True
            self.action = attribute_map.get("action") or ""
            return

        if not self._inside_login_form or tag != "input":
            return

        field_name = attribute_map.get("name")
        field_type = (attribute_map.get("type") or "").lower()
        if field_name and field_type == "hidden":
            self.hidden_fields[field_name] = attribute_map.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._inside_login_form:
            self._inside_login_form = False


class FormHashParser(HTMLParser):
    """Extract the first Discuz formhash input from an authenticated page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_hash = ""

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        if self.form_hash or tag != "input":
            return

        attribute_map = dict(attributes)
        if attribute_map.get("name") == "formhash":
            self.form_hash = attribute_map.get("value") or ""


@dataclass(frozen=True)
class AccountCredential:
    username: str
    password: str


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


class MtForumCheckinClient:
    """Log in to the Discuz forum and invoke its k_misign endpoint."""

    def __init__(
        self,
        credential: AccountCredential,
        request_timeout_seconds: float,
    ) -> None:
        self.credential = credential
        self.request_timeout_seconds = request_timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": USER_AGENT,
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = mask_identifier(self.credential.username)
        try:
            sign_page_text = self._authenticate_and_get_sign_page()
            form_hash = extract_form_hash(sign_page_text)
            sign_response_text = self._submit_checkin(form_hash)
            success, status, message = classify_checkin_response(sign_response_text)

            details: dict[str, str] = {}
            if success:
                refreshed_sign_page = self._get_sign_page()
                details = extract_checkin_details(refreshed_sign_page)

            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                success=success,
                status=status,
                message=message,
                details=details,
            )
        except requests.Timeout:
            return self._failure_result(account_number, account_label, "网络请求超时")
        except requests.ConnectionError:
            return self._failure_result(account_number, account_label, "无法连接 MT 论坛")
        except requests.RequestException as error:
            return self._failure_result(
                account_number,
                account_label,
                f"网络请求失败：{describe_request_error(error)}",
            )
        except ValueError as error:
            return self._failure_result(account_number, account_label, str(error))
        except Exception as error:
            return self._failure_result(
                account_number,
                account_label,
                f"未预期异常：{type(error).__name__}: {error}",
            )
        finally:
            self.session.close()

    def _authenticate_and_get_sign_page(self) -> str:
        account_key = self.credential.username
        stored_cookie = COOKIE_STORE.read(account_key)
        if stored_cookie:
            try:
                load_cookie_header(self.session, stored_cookie)
            except ValueError as error:
                print(f"[登录] {error}，回退账号密码")
                COOKIE_STORE.remove(account_key)
            else:
                sign_page_text = self._get_sign_page()
                if not is_login_required_page(sign_page_text):
                    extract_form_hash(sign_page_text)
                    refreshed_cookie = export_session_cookie_header(self.session)
                    if refreshed_cookie:
                        COOKIE_STORE.write(account_key, refreshed_cookie)
                    print("[登录] 本地 Cookie 有效")
                    return sign_page_text
                print("[登录] 本地 Cookie 已失效，回退账号密码")
                COOKIE_STORE.remove(account_key)
            self.session.cookies.clear()

        sign_page_text = self._login_and_get_sign_page()
        extract_form_hash(sign_page_text)
        current_cookie = export_session_cookie_header(self.session)
        if current_cookie:
            COOKIE_STORE.write(account_key, current_cookie)
            print("[Cookie存储] 已保存最新 Cookie")
        return sign_page_text

    def _login_and_get_sign_page(self) -> str:
        login_form_response = self.session.get(
            LOGIN_FORM_URL,
            timeout=self.request_timeout_seconds,
        )
        login_form_response.raise_for_status()

        login_parser = LoginFormParser()
        login_parser.feed(extract_cdata_content(login_form_response.text))
        if not login_parser.action:
            raise ValueError("登录页面结构已变化，未找到登录表单")

        form_hash = login_parser.hidden_fields.get("formhash")
        if not form_hash:
            raise ValueError("登录页面结构已变化，未找到 formhash")

        login_url = add_query_parameters(
            urljoin(LOGIN_FORM_URL, html.unescape(login_parser.action)),
            {"inajax": "1"},
        )
        login_data = {
            **login_parser.hidden_fields,
            "formhash": form_hash,
            "referer": urljoin(BASE_URL, "index.php"),
            "loginfield": "username",
            "username": self.credential.username,
            "password": self.credential.password,
            "questionid": "0",
            "answer": "",
        }
        login_response = self.session.post(
            login_url,
            data=login_data,
            headers={"Referer": LOGIN_FORM_URL},
            timeout=self.request_timeout_seconds,
        )
        login_response.raise_for_status()

        login_message = extract_response_message(login_response.text)
        if contains_any(login_message, ("登录失败", "密码错误", "密码不正确")):
            raise ValueError(f"登录失败：{login_message}")
        if contains_any(login_message, ("验证码", "安全提问")):
            raise ValueError(f"登录需要人工验证：{login_message}")

        sign_page_text = self._get_sign_page()
        if is_login_required_page(sign_page_text):
            failure_reason = login_message or "服务器未建立登录会话"
            raise ValueError(f"登录失败：{failure_reason}")
        return sign_page_text

    def _get_sign_page(self) -> str:
        response = self.session.get(
            SIGN_PAGE_URL,
            headers={"Referer": urljoin(BASE_URL, "index.php")},
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def _submit_checkin(self, form_hash: str) -> str:
        response = self.session.get(
            SIGN_ENDPOINT_URL,
            params={
                "id": "k_misign:sign",
                "operation": "qiandao",
                "format": "text",
                "formhash": form_hash,
            },
            headers={"Referer": SIGN_PAGE_URL},
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.text

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


def add_query_parameters(url: str, parameters: dict[str, str]) -> str:
    parsed_url = urlsplit(url)
    query_parameters = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    query_parameters.update(parameters)
    return urlunsplit(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            urlencode(query_parameters),
            parsed_url.fragment,
        )
    )


def extract_form_hash(page_text: str) -> str:
    parser = FormHashParser()
    parser.feed(page_text)
    if not parser.form_hash:
        raise ValueError("签到页面结构已变化，未找到 formhash")
    return parser.form_hash


def extract_response_message(response_text: str) -> str:
    message_source = extract_cdata_content(response_text)
    message_source = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        message_source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    message_source = re.sub(r"<[^>]+>", " ", message_source)
    return normalize_text(html.unescape(message_source))


def extract_cdata_content(response_text: str) -> str:
    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", response_text, re.DOTALL)
    return cdata_match.group(1) if cdata_match else response_text


def classify_checkin_response(response_text: str) -> tuple[bool, str, str]:
    response_message = extract_response_message(response_text)
    if not response_message:
        return False, "失败", "签到接口返回空内容"

    if contains_any(
        response_message,
        ("已经签到", "已签到", "今日已签", "无需重复签到"),
    ):
        return True, "今日已签到", response_message

    if contains_any(response_message, ("签到成功", "恭喜签到", "签到领奖成功")):
        return True, "签到成功", response_message

    if contains_any(response_message, ("需要先登录", "请先登录")):
        return False, "失败", "登录状态已失效"

    return False, "失败", response_message


def extract_checkin_details(page_text: str) -> dict[str, str]:
    details: dict[str, str] = {}

    nickname_match = re.search(
        r'<div\s+id=["\']comiis_key["\'][^>]*>.*?'
        r"<span[^>]*>(.*?)</span>.*?</div>",
        page_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if nickname_match:
        details["nickname"] = clean_html_value(nickname_match.group(1))

    detail_patterns = {
        "ranking": ("签到排名",),
        "continuous_days": ("连续签到",),
        "reward": ("积分奖励", "签到奖励"),
        "level": ("签到等级",),
        "total_days": ("总签到天数", "累计签到", "签到总天数"),
    }
    for detail_key, labels in detail_patterns.items():
        detail_value = extract_labeled_value(page_text, labels)
        if detail_value:
            details[detail_key] = detail_value

    return details


def extract_labeled_value(page_text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        escaped_label = re.escape(label)
        patterns = (
            rf"{escaped_label}\s*[：:]\s*(.*?)</(?:div|span|p|li)>",
            rf"{escaped_label}</h4>.{{0,600}}?value=[\"']([^\"']+)[\"']",
            rf"{escaped_label}.{{0,300}}?<span[^>]*>(.*?)</span>",
        )
        for pattern in patterns:
            value_match = re.search(
                pattern,
                page_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if value_match:
                cleaned_value = clean_html_value(value_match.group(1))
                if cleaned_value:
                    return cleaned_value
    return ""


def clean_html_value(value: str) -> str:
    return normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


def is_login_required_page(page_text: str) -> bool:
    return contains_any(page_text, ("您需要先登录", "请先登录后继续"))


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    parsed_cookie = SimpleCookie()
    try:
        parsed_cookie.load(cookie_header)
    except Exception as error:
        raise ValueError("本地 Cookie 格式无效") from error
    if not parsed_cookie:
        raise ValueError("本地 Cookie 未包含有效字段")
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
            configuration_errors.append(
                f"第 {line_number} 行账号或密码为空",
            )
            continue

        credentials.append(AccountCredential(username=username, password=password))

    return credentials, configuration_errors


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


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
    title = f"MT论坛签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    detail_labels = {
        "nickname": "昵称",
        "ranking": "签到排名",
        "continuous_days": "连续签到",
        "reward": "积分奖励",
        "level": "签到等级",
        "total_days": "累计签到",
    }
    html_sections = [
        "<b>MT 论坛每日签到</b>",
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
        "MT 论坛每日签到",
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
            detail_label = detail_labels.get(detail_key, detail_key)
            html_account_lines.append(
                f"• {escape_html_text(detail_label)}：{html_code(detail_value)}",
            )
            plain_account_lines.append(f"• {detail_label}：{detail_value}")
        html_sections.append("\n".join(html_account_lines))
        plain_sections.append("\n".join(plain_account_lines))

    if configuration_errors:
        html_error_lines = ["<b>配置提示</b>"]
        html_error_lines.extend(
            f"• {escape_html_text(error)}" for error in configuration_errors
        )
        html_sections.append("\n".join(html_error_lines))

        plain_error_lines = ["配置提示"]
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
            f"message_id={message_result.get('message_id')}",
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
    result_marker = "成功" if result.success else "失败"
    print(f"[{result_marker}] 账号 {result.account_number}（{result.account_label}）")
    print(f"  状态：{result.status}")
    print(f"  说明：{result.message}")
    for detail_key, detail_value in result.details.items():
        print(f"  {detail_key}：{detail_value}")


def main() -> int:
    print(f"==== MT论坛签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw_accounts = os.getenv("MT_ACCOUNTS") or os.getenv("mt", "")
    credentials, configuration_errors = parse_accounts(raw_accounts)
    if not credentials and not configuration_errors:
        configuration_errors.append("未配置 MT_ACCOUNTS 或 mt 环境变量")

    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    request_timeout_seconds = runtime_settings.request_timeout_seconds

    apply_startup_random_delay(
        "MT论坛签到",
        runtime_settings,
        has_work=bool(credentials),
    )

    results: list[CheckinResult] = []
    for account_index, credential in enumerate(credentials, start=1):
        masked_username = mask_identifier(credential.username)
        print(f"\n---- 账号 {account_index}（{masked_username}）开始 ----")
        client = MtForumCheckinClient(credential, request_timeout_seconds)
        result = client.checkin(account_index)
        results.append(result)
        print_result(result)

        wait_between_accounts(
            account_index,
            len(credentials),
            runtime_settings.account_delay_seconds,
        )

    notification_title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
    )
    print("\n==== MT论坛签到汇总 ====")
    print(plain_content)

    if read_boolean_environment("MT_NOTIFY", True):
        send_notifications(
            notification_title,
            html_content,
            plain_content,
            request_timeout_seconds,
        )
    else:
        print("[通知] MT_NOTIFY 已关闭，跳过通知")

    all_accounts_succeeded = bool(results) and all(result.success for result in results)
    configuration_is_valid = not configuration_errors
    return 0 if all_accounts_succeeded and configuration_is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
