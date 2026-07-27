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

通知使用青龙注入的 QLAPI.systemNotify，直接复用面板通知设置。
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
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests


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

    def format_for_notification(self) -> str:
        result_lines = [
            f"账号 {self.account_number}（{self.account_label}）",
            f"结果：{self.status}",
            f"说明：{self.message}",
        ]
        detail_labels = {
            "nickname": "昵称",
            "ranking": "签到排名",
            "continuous_days": "连续签到",
            "reward": "积分奖励",
            "level": "签到等级",
            "total_days": "累计签到",
        }
        for detail_key, detail_label in detail_labels.items():
            detail_value = self.details.get(detail_key)
            if detail_value:
                result_lines.append(f"{detail_label}：{detail_value}")
        return "\n".join(result_lines)


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
            sign_page_text = self._login_and_get_sign_page()
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


def build_notification_content(
    results: list[CheckinResult],
    configuration_errors: list[str],
) -> str:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    content_sections = [
        f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"账号统计：成功 {successful_count}，失败 {failed_count}",
    ]

    if configuration_errors:
        content_sections.append("配置错误：\n" + "\n".join(configuration_errors))

    content_sections.extend(result.format_for_notification() for result in results)
    return "\n\n".join(content_sections)


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

    request_timeout_seconds = read_positive_float_environment(
        "TASK_TIMEOUT",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    account_delay_seconds = read_positive_float_environment(
        "TASK_ACCOUNT_DELAY",
        DEFAULT_ACCOUNT_DELAY_SECONDS,
    )

    # 启动前随机延迟，避免固定时间签到触发风控
    random_signin_enabled = read_boolean_environment("TASK_RANDOM_SIGNIN", True)
    if random_signin_enabled:
        max_random_delay = read_positive_float_environment(
            "TASK_RANDOM_DELAY_MAX",
            3600.0,
        )
        if max_random_delay > 0:
            delay_seconds = random.uniform(0, max_random_delay)
            print(f"🎲 随机延迟：{format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "MT论坛签到")

    results: list[CheckinResult] = []
    for account_index, credential in enumerate(credentials, start=1):
        masked_username = mask_identifier(credential.username)
        print(f"\n---- 账号 {account_index}（{masked_username}）开始 ----")
        client = MtForumCheckinClient(credential, request_timeout_seconds)
        result = client.checkin(account_index)
        results.append(result)
        print_result(result)

        has_next_account = account_index < len(credentials)
        if has_next_account and account_delay_seconds > 0:
            print(f"等待 {account_delay_seconds:g} 秒后处理下一个账号")
            time.sleep(account_delay_seconds)

    notification_content = build_notification_content(results, configuration_errors)
    print("\n==== MT论坛签到汇总 ====")
    print(notification_content)

    if read_boolean_environment("MT_NOTIFY", True):
        send_system_notification("MT论坛签到", notification_content)
    else:
        print("[通知] MT_NOTIFY 已关闭，跳过通知")

    all_accounts_succeeded = bool(results) and all(result.success for result in results)
    configuration_is_valid = not configuration_errors
    return 0 if all_accounts_succeeded and configuration_is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
