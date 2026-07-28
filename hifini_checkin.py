#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 20 9 * * *
new Env('HiFi 音乐站签到')

环境变量：
  HIFI_ACCOUNTS         必填。每行一个账号，格式如下：
                        域名|Cookie|用户名|密码
                        Cookie 优先；Cookie 失效后，配置了用户名密码才会回退登录。
                        仅 Cookie：域名|Cookie
                        仅账号密码：域名||用户名|密码
                        hifiii.com 当前强制滑块，密码回退通常会失败，应优先维护 Cookie。
  HIFI_NOTIFY           是否发送青龙面板系统通知，默认为 true。
  HIFI_PRIVACY_MODE     日志和通知中是否对用户名脱敏，默认为 true。
  HIFI_USER_AGENT       可选。覆盖默认浏览器 User-Agent。
  TASK_RANDOM_SIGNIN    是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT          单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY    多账号之间的等待秒数，默认为 3（所有任务共用）。

通知复用青龙注入的 QLAPI.systemNotify。内容使用 Telegram Markdown 风格排版。
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
DEFAULT_RANDOM_DELAY_MAX_SECONDS = 3600.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

LOGIN_PATH = "/user-login.htm"
SIGN_PATH = "/sg_sign.htm"

USER_ID_PATTERN = re.compile(r"\bvar\s+uid\s*=\s*(\d+)\s*;", re.IGNORECASE)
USERNAME_LINK_PATTERN = re.compile(
    r'<a\b[^>]*href=["\'][^"\']*user-\d+\.htm[^"\']*["\'][^>]*>'
    r"\s*(.*?)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class AccountConfiguration:
    domain: str
    cookie: str = ""
    username: str = ""
    password: str = ""


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    domain: str
    success: bool
    status: str
    message: str
    login_method: str = "未登录"
    details: dict[str, str] = field(default_factory=dict)

    def format_for_console(self) -> list[str]:
        marker = "成功" if self.success else "失败"
        lines = [
            f"[{marker}] 账号 {self.account_number}（{self.account_label}）",
            f"  站点：{self.domain}",
            f"  登录方式：{self.login_method}",
            f"  状态：{self.status}",
            f"  说明：{self.message}",
        ]
        for detail_name, detail_value in self.details.items():
            lines.append(f"  {detail_name}：{detail_value}")
        return lines


@dataclass(frozen=True)
class AuthenticationResult:
    success: bool
    login_method: str
    username: str = ""
    message: str = ""


class HifiCheckinClient:
    """使用 Cookie 或账号密码登录 Xiuno 音乐站并执行每日签到。"""

    def __init__(
        self,
        configuration: AccountConfiguration,
        timeout_seconds: float,
        user_agent: str,
        privacy_mode: bool,
    ) -> None:
        self.configuration = configuration
        self.timeout_seconds = timeout_seconds
        self.privacy_mode = privacy_mode
        self.base_url = normalize_base_url(configuration.domain)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": user_agent,
            }
        )

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = self._configured_account_label()
        authentication_result = AuthenticationResult(
            success=False,
            login_method="未登录",
            message="尚未尝试登录",
        )

        try:
            authentication_result = self._authenticate()
            if not authentication_result.success:
                return self._failure_result(
                    account_number=account_number,
                    account_label=account_label,
                    status="登录失败",
                    message=authentication_result.message,
                    login_method=authentication_result.login_method,
                )

            authenticated_username = authentication_result.username
            if authenticated_username:
                account_label = self._display_identifier(authenticated_username)

            sign_response = self._submit_checkin()
            success, status, message = classify_sign_response(sign_response)

            details: dict[str, str] = {}
            if authenticated_username:
                details["用户名"] = self._display_identifier(
                    authenticated_username,
                )

            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                domain=urlsplit(self.base_url).netloc,
                success=success,
                status=status,
                message=message,
                login_method=authentication_result.login_method,
                details=details,
            )

        except requests.Timeout:
            return self._failure_result(
                account_number,
                account_label,
                "请求超时",
                "网络请求超过设定时间",
                authentication_result.login_method,
            )
        except requests.ConnectionError:
            return self._failure_result(
                account_number,
                account_label,
                "连接失败",
                "无法连接目标站点",
                authentication_result.login_method,
            )
        except requests.RequestException as error:
            return self._failure_result(
                account_number,
                account_label,
                "网络错误",
                describe_request_error(error),
                authentication_result.login_method,
            )
        except ValueError as error:
            return self._failure_result(
                account_number,
                account_label,
                "执行失败",
                str(error),
                authentication_result.login_method,
            )
        except Exception as error:
            return self._failure_result(
                account_number,
                account_label,
                "执行异常",
                f"{type(error).__name__}: {error}",
                authentication_result.login_method,
            )
        finally:
            self.session.close()

    def _authenticate(self) -> AuthenticationResult:
        cookie_failure_message = ""

        if self.configuration.cookie:
            load_cookie_header(self.session, self.configuration.cookie)
            authenticated, username, message = self._verify_session()
            if authenticated:
                return AuthenticationResult(
                    success=True,
                    login_method="Cookie",
                    username=username,
                    message="Cookie 登录状态有效",
                )
            cookie_failure_message = message or "Cookie 已失效"
            print(f"[登录] Cookie 不可用：{cookie_failure_message}")

        if not self.configuration.username or not self.configuration.password:
            if self.configuration.cookie:
                return AuthenticationResult(
                    success=False,
                    login_method="Cookie",
                    message=(
                        f"{cookie_failure_message}；未配置账号密码，无法回退登录"
                    ),
                )
            return AuthenticationResult(
                success=False,
                login_method="未配置",
                message="未配置 Cookie，也未配置完整账号密码",
            )

        self.session.cookies.clear()
        self._visit_homepage()
        login_result = self._login_with_password()
        if not login_result.success and cookie_failure_message:
            return AuthenticationResult(
                success=False,
                login_method=login_result.login_method,
                message=(
                    f"Cookie 不可用（{cookie_failure_message}）；"
                    f"账号密码回退失败（{login_result.message}）"
                ),
            )
        return login_result

    def _verify_session(self) -> tuple[bool, str, str]:
        response = self._visit_homepage()
        if response.status_code == 403:
            return (
                False,
                "",
                "HTTP 403，站点拒绝当前网络或请求特征",
            )
        response.raise_for_status()

        username = extract_authenticated_username(response.text)
        if is_authenticated_page(response.text):
            return True, username, ""
        return False, "", "Cookie 已失效或未建立登录会话"

    def _visit_homepage(self) -> requests.Response:
        response = self.session.get(
            self.base_url,
            timeout=self.timeout_seconds,
            verify=False,
            allow_redirects=True,
        )
        self.base_url = origin_from_url(response.url)
        return response

    def _login_with_password(self) -> AuthenticationResult:
        login_url = build_url(self.base_url, LOGIN_PATH)
        password_digest = hashlib.md5(
            self.configuration.password.encode("utf-8"),
        ).hexdigest()
        response = self.session.post(
            login_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self.base_url.rstrip("/"),
                "Referer": login_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            data={
                "email": self.configuration.username,
                "password": password_digest,
            },
            timeout=self.timeout_seconds,
            verify=False,
        )

        if response.status_code == 403:
            return AuthenticationResult(
                success=False,
                login_method="账号密码",
                message="HTTP 403，站点拒绝当前网络或请求特征",
            )
        response.raise_for_status()

        response_data = parse_json_response(response.text)
        response_code = response_data.get("code")
        response_message = clean_message(response_data.get("message"))

        if response_code == "captcha" or contains_any(
            response_message,
            ("人机验证", "验证码", "滑块"),
        ):
            return AuthenticationResult(
                success=False,
                login_method="账号密码",
                message="站点要求滑动人机验证，无法使用纯 HTTP 账号密码回退",
            )

        if not is_success_code(response_code):
            return AuthenticationResult(
                success=False,
                login_method="账号密码",
                message=response_message or f"登录接口返回 code={response_code!r}",
            )

        authenticated, username, verification_message = self._verify_session()
        if not authenticated:
            return AuthenticationResult(
                success=False,
                login_method="账号密码",
                message=(
                    "登录接口返回成功，但会话验证失败："
                    f"{verification_message}"
                ),
            )

        return AuthenticationResult(
            success=True,
            login_method="账号密码回退",
            username=username or self.configuration.username,
            message=response_message or "登录成功",
        )

    def _submit_checkin(self) -> dict[str, Any]:
        sign_url = build_url(self.base_url, SIGN_PATH)
        response = self.session.post(
            sign_url,
            headers={
                "Accept": "text/plain, */*; q=0.01",
                "Origin": self.base_url.rstrip("/"),
                "Referer": self.base_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout_seconds,
            verify=False,
        )

        if response.status_code == 403:
            raise ValueError("签到接口返回 HTTP 403，站点拒绝当前网络或请求特征")
        response.raise_for_status()
        return parse_json_response(response.text)

    def _configured_account_label(self) -> str:
        if self.configuration.username:
            return self._display_identifier(self.configuration.username)
        return "Cookie 账号"

    def _display_identifier(self, identifier: str) -> str:
        if not self.privacy_mode:
            return identifier
        return mask_identifier(identifier)

    def _failure_result(
        self,
        account_number: int,
        account_label: str,
        status: str,
        message: str,
        login_method: str,
    ) -> CheckinResult:
        return CheckinResult(
            account_number=account_number,
            account_label=account_label,
            domain=urlsplit(self.base_url).netloc,
            success=False,
            status=status,
            message=message,
            login_method=login_method,
        )


def normalize_base_url(domain: str) -> str:
    normalized_domain = domain.strip()
    if not normalized_domain:
        raise ValueError("站点域名不能为空")

    if "://" not in normalized_domain:
        normalized_domain = f"https://{normalized_domain}"

    parsed_url = urlsplit(normalized_domain)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError(f"无效站点地址：{domain!r}")

    netloc = parsed_url.netloc
    return urlunsplit((parsed_url.scheme, netloc, "/", "", ""))


def origin_from_url(url: str) -> str:
    parsed_url = urlsplit(url)
    return urlunsplit((parsed_url.scheme, parsed_url.netloc, "/", "", ""))


def build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    session.cookies.clear()
    normalized_cookie_header = re.sub(
        r"^\s*cookie\s*:\s*",
        "",
        cookie_header,
        flags=re.IGNORECASE,
    )
    for raw_cookie_part in normalized_cookie_header.split(";"):
        cookie_part = raw_cookie_part.strip()
        if not cookie_part or "=" not in cookie_part:
            continue
        cookie_name, cookie_value = cookie_part.split("=", maxsplit=1)
        cookie_name = cookie_name.strip()
        if cookie_name:
            session.cookies.set(cookie_name, cookie_value.strip())


def parse_json_response(response_text: str) -> dict[str, Any]:
    stripped_response = response_text.strip()
    try:
        parsed_response = json.loads(stripped_response)
    except json.JSONDecodeError as error:
        response_preview = clean_message(stripped_response)[:200]
        raise ValueError(
            f"接口未返回有效 JSON：{response_preview or '空响应'}",
        ) from error

    if not isinstance(parsed_response, dict):
        raise ValueError("接口 JSON 结构异常，预期为对象")
    return parsed_response


def classify_sign_response(response_data: dict[str, Any]) -> tuple[bool, str, str]:
    response_code = response_data.get("code")
    response_message = clean_message(response_data.get("message"))

    if is_success_code(response_code):
        return True, "签到成功", response_message or "每日签到完成"

    if contains_any(
        response_message,
        ("今天已经签过", "已经签到", "已签到", "请勿重复"),
    ):
        return True, "今日已签到", response_message

    if contains_any(response_message, ("请登录", "登录后再签到")):
        return False, "登录状态失效", response_message

    if contains_any(response_message, ("人机验证", "验证码", "滑块")):
        return False, "需要人机验证", response_message

    return (
        False,
        "签到失败",
        response_message or f"签到接口返回 code={response_code!r}",
    )


def is_success_code(response_code: Any) -> bool:
    return response_code == 0 or response_code == "0"


def is_authenticated_page(page_text: str) -> bool:
    user_id_match = USER_ID_PATTERN.search(page_text)
    if user_id_match and int(user_id_match.group(1)) > 0:
        return True
    return "user-logout" in page_text or "退出登录" in page_text


def extract_authenticated_username(page_text: str) -> str:
    username_match = USERNAME_LINK_PATTERN.search(page_text)
    if not username_match:
        return ""
    return clean_message(username_match.group(1))


def clean_message(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def parse_account_configurations(
    raw_accounts: str,
) -> tuple[list[AccountConfiguration], list[str]]:
    configurations: list[AccountConfiguration] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        account_line = raw_line.strip()
        if not account_line:
            continue

        account_parts = account_line.split("|", maxsplit=3)
        if len(account_parts) < 2:
            configuration_errors.append(
                f"第 {line_number} 行格式错误，应至少包含“域名|Cookie”",
            )
            continue

        domain = account_parts[0].strip()
        cookie = account_parts[1].strip()
        username = account_parts[2].strip() if len(account_parts) >= 3 else ""
        password = account_parts[3].strip() if len(account_parts) >= 4 else ""

        if not domain:
            configuration_errors.append(f"第 {line_number} 行域名为空")
            continue
        if not cookie and not (username and password):
            configuration_errors.append(
                f"第 {line_number} 行未配置 Cookie 或完整账号密码",
            )
            continue
        if bool(username) != bool(password):
            configuration_errors.append(
                f"第 {line_number} 行账号和密码必须同时配置",
            )
            continue

        try:
            normalize_base_url(domain)
        except ValueError as error:
            configuration_errors.append(f"第 {line_number} 行：{error}")
            continue

        configurations.append(
            AccountConfiguration(
                domain=domain,
                cookie=cookie,
                username=username,
                password=password,
            )
        )

    return configurations, configuration_errors


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


def read_boolean_environment(variable_name: str, default_value: bool) -> bool:
    raw_value = os.getenv(variable_name)
    if raw_value is None:
        return default_value
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


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


def format_time_remaining(seconds: float) -> str:
    total_seconds = int(seconds)
    if total_seconds <= 0:
        return "立即执行"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{remaining_seconds}秒"
    if minutes > 0:
        return f"{minutes}分{remaining_seconds}秒"
    return f"{remaining_seconds}秒"


def wait_with_countdown(delay_seconds: float, task_name: str) -> None:
    remaining_seconds = delay_seconds
    while remaining_seconds > 0:
        print(
            f"{task_name} 倒计时："
            f"{format_time_remaining(remaining_seconds)}",
        )
        sleep_seconds = 1 if remaining_seconds <= 10 else min(10, remaining_seconds)
        time.sleep(sleep_seconds)
        remaining_seconds -= sleep_seconds


def markdown_code(value: Any) -> str:
    safe_value = (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("\n", " ")
    )
    return f"`{safe_value}`"


def markdown_text(value: Any) -> str:
    text = str(value).replace("\n", " ")
    return re.sub(r"([\\_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


def build_markdown_notification(
    results: list[CheckinResult],
    configuration_errors: list[str],
) -> tuple[str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"🎵 HiFi 签到 | {status_icon} {successful_count}/{len(results)}"

    sections = [
        "🎵 *HiFi 音乐站每日签到*",
        "\n".join(
            [
                "📊 *执行概览*",
                f"• 成功：{markdown_code(successful_count)}",
                f"• 失败：{markdown_code(failed_count)}",
                f"• 时间：{markdown_code(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}",
            ]
        ),
    ]

    for result in results:
        result_icon = "✅" if result.success else "❌"
        account_lines = [
            f"{result_icon} *账号 {result.account_number} · "
            f"{markdown_text(result.account_label)}*",
            f"• 站点：{markdown_code(result.domain)}",
            f"• 登录：{markdown_code(result.login_method)}",
            f"• 状态：*{markdown_text(result.status)}*",
            f"• 说明：{markdown_text(result.message)}",
        ]
        for detail_name, detail_value in result.details.items():
            account_lines.append(
                f"• {markdown_text(detail_name)}：{markdown_code(detail_value)}",
            )
        sections.append("\n".join(account_lines))

    if configuration_errors:
        error_lines = ["⚙️ *配置提示*"]
        error_lines.extend(
            f"• {markdown_text(error)}" for error in configuration_errors
        )
        sections.append("\n".join(error_lines))

    return title, "\n\n".join(sections)


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


def print_result(result: CheckinResult) -> None:
    for result_line in result.format_for_console():
        print(result_line)


def main() -> int:
    print(
        "==== HiFi 音乐站每日签到开始 "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====",
    )

    raw_accounts = os.getenv("HIFI_ACCOUNTS", "")
    configurations, configuration_errors = parse_account_configurations(
        raw_accounts,
    )
    if not configurations and not configuration_errors:
        configuration_errors.append("未配置 HIFI_ACCOUNTS 环境变量")

    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    timeout_seconds = read_positive_float_environment(
        "TASK_TIMEOUT",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    account_delay_seconds = read_positive_float_environment(
        "TASK_ACCOUNT_DELAY",
        DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    privacy_mode = read_boolean_environment("HIFI_PRIVACY_MODE", True)
    notification_enabled = read_boolean_environment("HIFI_NOTIFY", True)
    user_agent = os.getenv("HIFI_USER_AGENT", DEFAULT_USER_AGENT).strip()
    if not user_agent:
        user_agent = DEFAULT_USER_AGENT

    random_signin_enabled = read_boolean_environment("TASK_RANDOM_SIGNIN", True)
    if random_signin_enabled:
        maximum_random_delay = read_positive_float_environment(
            "TASK_RANDOM_DELAY_MAX",
            DEFAULT_RANDOM_DELAY_MAX_SECONDS,
        )
        if maximum_random_delay > 0:
            delay_seconds = random.uniform(0, maximum_random_delay)
            print(f"🎲 随机延迟：{format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "HiFi 签到")

    results: list[CheckinResult] = []
    for account_number, configuration in enumerate(configurations, start=1):
        configured_label = (
            mask_identifier(configuration.username)
            if configuration.username and privacy_mode
            else configuration.username or "Cookie 账号"
        )
        print(
            f"\n---- 账号 {account_number}（{configured_label}）"
            f"· {urlsplit(normalize_base_url(configuration.domain)).netloc} ----",
        )

        client = HifiCheckinClient(
            configuration=configuration,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            privacy_mode=privacy_mode,
        )
        result = client.checkin(account_number)
        results.append(result)
        print_result(result)

        has_next_account = account_number < len(configurations)
        if has_next_account and account_delay_seconds > 0:
            print(f"等待 {account_delay_seconds:g} 秒后处理下一个账号")
            time.sleep(account_delay_seconds)

    notification_title, notification_content = build_markdown_notification(
        results,
        configuration_errors,
    )
    print("\n==== HiFi 音乐站每日签到汇总 ====")
    print(notification_content)

    if notification_enabled:
        send_system_notification(notification_title, notification_content)
    else:
        print("[通知] HIFI_NOTIFY 已关闭，跳过通知")

    all_succeeded = bool(results) and all(result.success for result in results)
    configuration_valid = not configuration_errors
    return 0 if all_succeeded and configuration_valid else 1


if __name__ == "__main__":
    sys.exit(main())
