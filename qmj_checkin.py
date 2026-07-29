#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 25 9 * * *
new Env('阡陌居签到')

环境变量：
  QMJ_ACCOUNTS          必填。每行一个账号，支持以下格式：
                        用户名|密码
                        用户名|密码|Cookie
                        纯 Cookie
                        Cookie 优先；失效后，配置了用户名密码才会回退登录。
  QMJ_NOTIFY            是否发送通知，默认为 true。
  QMJ_PRIVACY_MODE      日志和通知中是否对用户名脱敏，默认为 true。
  QMJ_SIGN_MOOD         签到心情代码，默认为 wl。
  TG_NOTIFY_CONFIG      可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                        配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN    是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT          单次请求超时秒数，默认为 90（所有任务共用）。
  TASK_ACCOUNT_DELAY    多账号之间的等待秒数，默认为 3（所有任务共用）。

账号密码登录通过 Discuz 移动 API 完成，不经过网页端顶象滑块。登录成功或
Cookie 验证成功后，会把刷新后的会话保存到
/ql/data/scripts_data/qmj_cookies.json，后续运行优先复用。

任务依次执行每日签到和申请 ID 为 1 的每日威望红包任务，不自动领取任务
奖励。通知展示任务完成后的当前铜币和威望余额。通知优先使用 Telegram HTML
直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import hashlib
import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from comm.cookie_store import CookieStore
from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)


BASE_URL = "https://www.1000qm.vip/"
MOBILE_LOGIN_API_URL = urljoin(BASE_URL, "api/mobile/index.php")
SIGN_PAGE_URL = urljoin(BASE_URL, "plugin.php?id=dsu_paulsign:sign")
SIGN_ENDPOINT_URL = urljoin(BASE_URL, "plugin.php")
TASK_APPLY_URL = urljoin(BASE_URL, "home.php")
CREDIT_PAGE_URL = urljoin(BASE_URL, "home.php?mod=spacecp&ac=credit")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
DEFAULT_SIGN_MOOD = "wl"
TASK_IDENTIFIER = "1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)
COOKIE_STORE = CookieStore("qmj_cookies")


class FormHashParser(HTMLParser):
    """从 Discuz 页面提取第一个 formhash 隐藏字段。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_hash = ""

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        if self.form_hash or tag.lower() != "input":
            return

        attribute_map = {
            name.lower(): value or ""
            for name, value in attributes
        }
        if attribute_map.get("name") == "formhash":
            self.form_hash = attribute_map.get("value", "")


class VisibleTextParser(HTMLParser):
    """提取页面可见文本，排除脚本中的积分名称映射。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self._hidden_element_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        del attributes
        if tag.lower() in {"script", "style"}:
            self._hidden_element_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._hidden_element_depth:
            self._hidden_element_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_element_depth and data.strip():
            self.text_parts.append(data)


@dataclass(frozen=True)
class AccountConfiguration:
    identifier: str = ""
    password: str = ""
    cookie: str = ""

    @property
    def login_field(self) -> str:
        return "email" if "@" in self.identifier else "username"

    @property
    def storage_key(self) -> str:
        if self.identifier:
            return self.identifier
        cookie_digest = hashlib.sha256(
            self.cookie.encode("utf-8"),
        ).hexdigest()[:16]
        return f"cookie:{cookie_digest}"


@dataclass(frozen=True)
class AuthenticationResult:
    login_method: str
    sign_page_response: requests.Response


@dataclass(frozen=True)
class OperationResult:
    success: bool
    status: str
    message: str


@dataclass(frozen=True)
class AccountResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    login_method: str
    sign_result: OperationResult
    task_result: OperationResult
    copper_balance: str = ""
    reputation_balance: str = ""


class QianmojuCheckinClient:
    """使用 Cookie 或移动 API 登录阡陌居并执行每日任务。"""

    def __init__(
        self,
        configuration: AccountConfiguration,
        request_timeout_seconds: float,
        sign_mood: str,
        privacy_mode: bool,
    ) -> None:
        self.configuration = configuration
        self.request_timeout_seconds = request_timeout_seconds
        self.sign_mood = sign_mood
        self.privacy_mode = privacy_mode
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

    def run(self, account_number: int) -> AccountResult:
        account_label = self._account_label()
        login_method = "未登录"

        try:
            authentication_result = self._authenticate()
            login_method = authentication_result.login_method
            form_hash = extract_form_hash(
                authentication_result.sign_page_response.text,
            )
            sign_result = self._submit_checkin(form_hash)
            task_result = self._apply_reputation_task()
            (
                copper_balance,
                reputation_balance,
            ) = self._fetch_credit_balances_safely()
            self._save_current_cookie()
            all_operations_succeeded = sign_result.success and task_result.success
            status = "执行成功" if all_operations_succeeded else "部分或全部失败"
            return AccountResult(
                account_number=account_number,
                account_label=account_label,
                success=all_operations_succeeded,
                status=status,
                login_method=login_method,
                sign_result=sign_result,
                task_result=task_result,
                copper_balance=copper_balance,
                reputation_balance=reputation_balance,
            )
        except requests.Timeout:
            failure_message = "网络请求超过设定时间"
        except requests.ConnectionError:
            failure_message = "无法连接阡陌居"
        except requests.RequestException as error:
            failure_message = f"网络请求失败：{describe_request_error(error)}"
        except ValueError as error:
            failure_message = str(error)
        except Exception as error:
            failure_message = f"未预期异常：{type(error).__name__}: {error}"
        finally:
            self.session.close()

        skipped_result = OperationResult(
            success=False,
            status="未执行",
            message=failure_message,
        )
        return AccountResult(
            account_number=account_number,
            account_label=account_label,
            success=False,
            status="执行失败",
            login_method=login_method,
            sign_result=skipped_result,
            task_result=skipped_result,
        )

    def _authenticate(self) -> AuthenticationResult:
        cookie_candidates = (
            ("本地 Cookie", COOKIE_STORE.read(self.configuration.storage_key)),
            ("环境变量 Cookie", self.configuration.cookie),
        )
        attempted_cookie_values: set[str] = set()

        for login_method, cookie_value in cookie_candidates:
            normalized_cookie = cookie_value.strip()
            if not normalized_cookie or normalized_cookie in attempted_cookie_values:
                continue
            attempted_cookie_values.add(normalized_cookie)

            try:
                load_cookie_header(self.session, normalized_cookie)
            except ValueError as error:
                print(f"[登录] {login_method} 格式无效：{error}")
                if login_method == "本地 Cookie":
                    COOKIE_STORE.remove(self.configuration.storage_key)
                continue

            sign_page_response = self._get_sign_page()
            if is_authenticated_sign_page(sign_page_response):
                extract_form_hash(sign_page_response.text)
                self._save_current_cookie()
                print(f"[登录] {login_method} 有效")
                return AuthenticationResult(login_method, sign_page_response)

            print(f"[登录] {login_method} 已失效")
            if login_method == "本地 Cookie":
                COOKIE_STORE.remove(self.configuration.storage_key)
            self.session.cookies.clear()

        if not self.configuration.identifier or not self.configuration.password:
            raise ValueError("Cookie 已失效，且未配置完整账号密码")

        self.session.cookies.clear()
        sign_page_response = self._login_with_mobile_api()
        self._save_current_cookie()
        print("[登录] Discuz 移动 API 账号密码登录成功")
        return AuthenticationResult("移动 API 账号密码", sign_page_response)

    def _login_with_mobile_api(self) -> requests.Response:
        bootstrap_response = self.session.get(
            MOBILE_LOGIN_API_URL,
            params={"version": "4", "module": "login"},
            timeout=self.request_timeout_seconds,
        )
        bootstrap_response.raise_for_status()

        try:
            bootstrap_payload = bootstrap_response.json()
        except ValueError as error:
            raise ValueError("移动登录初始化接口未返回有效 JSON") from error

        variables = bootstrap_payload.get("Variables")
        if not isinstance(variables, dict):
            raise ValueError("移动登录初始化响应缺少 Variables")
        form_hash = str(variables.get("formhash") or "")
        if not form_hash:
            raise ValueError("移动登录初始化响应缺少 formhash")

        login_response = self.session.post(
            MOBILE_LOGIN_API_URL,
            params={
                "version": "4",
                "module": "login",
                "loginsubmit": "yes",
            },
            data={
                "formhash": form_hash,
                "loginfield": self.configuration.login_field,
                "username": self.configuration.identifier,
                "password": self.configuration.password,
                "questionid": "0",
                "answer": "",
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL.rstrip("/"),
                "Referer": BASE_URL,
            },
            timeout=self.request_timeout_seconds,
        )
        login_response.raise_for_status()
        login_failure_message = extract_mobile_login_failure(login_response)

        sign_page_response = self._get_sign_page()
        if not is_authenticated_sign_page(sign_page_response):
            failure_reason = login_failure_message or "服务器未建立登录会话"
            raise ValueError(f"账号密码登录失败：{failure_reason}")
        extract_form_hash(sign_page_response.text)
        return sign_page_response

    def _get_sign_page(self) -> requests.Response:
        response = self.session.get(
            SIGN_PAGE_URL,
            headers={"Referer": BASE_URL},
            timeout=self.request_timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response

    def _submit_checkin(self, form_hash: str) -> OperationResult:
        response = self.session.post(
            SIGN_ENDPOINT_URL,
            params={
                "id": "dsu_paulsign:sign",
                "operation": "qiandao",
                "infloat": "1",
                "inajax": "1",
            },
            data={
                "formhash": form_hash,
                "qdxq": self.sign_mood,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL.rstrip("/"),
                "Referer": SIGN_PAGE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        return classify_sign_response(response.text)

    def _apply_reputation_task(self) -> OperationResult:
        response = self.session.get(
            TASK_APPLY_URL,
            params={
                "mod": "task",
                "do": "apply",
                "id": TASK_IDENTIFIER,
            },
            headers={
                "Referer": urljoin(BASE_URL, "home.php?mod=task"),
            },
            timeout=self.request_timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        if is_login_required_response(response):
            return OperationResult(False, "申请失败", "登录状态已失效")
        return classify_task_apply_response(response.text)

    def _fetch_credit_balances(self) -> tuple[str, str]:
        response = self.session.get(
            CREDIT_PAGE_URL,
            headers={"Referer": urljoin(BASE_URL, "home.php?mod=task")},
            timeout=self.request_timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        if is_login_required_response(response):
            raise ValueError("读取积分余额时登录状态已失效")
        return extract_credit_balances(response.text)

    def _fetch_credit_balances_safely(self) -> tuple[str, str]:
        try:
            return self._fetch_credit_balances()
        except requests.RequestException as error:
            print(f"[积分] 读取当前余额失败：{describe_request_error(error)}")
        except ValueError as error:
            print(f"[积分] 读取当前余额失败：{error}")
        return "", ""

    def _save_current_cookie(self) -> None:
        current_cookie = export_session_cookie_header(self.session)
        if current_cookie:
            COOKIE_STORE.write(self.configuration.storage_key, current_cookie)

    def _account_label(self) -> str:
        if not self.configuration.identifier:
            return "Cookie 账号"
        if not self.privacy_mode:
            return self.configuration.identifier
        return mask_identifier(self.configuration.identifier)


def extract_form_hash(page_text: str) -> str:
    parser = FormHashParser()
    parser.feed(page_text)
    if parser.form_hash:
        return parser.form_hash

    link_match = re.search(
        r"formhash=([0-9A-Za-z]{8})(?:&|&amp;)",
        page_text,
        flags=re.IGNORECASE,
    )
    if link_match:
        return link_match.group(1)
    raise ValueError("签到页面结构已变化，未找到 formhash")


def extract_credit_balances(page_text: str) -> tuple[str, str]:
    parser = VisibleTextParser()
    parser.feed(page_text)
    visible_text = normalize_text(" ".join(parser.text_parts))

    copper_match = re.search(r"铜币\s*[：:]\s*(-?\d+)", visible_text)
    reputation_match = re.search(r"威望\s*[：:]\s*(-?\d+)", visible_text)
    missing_balances: list[str] = []
    if not copper_match:
        missing_balances.append("铜币")
    if not reputation_match:
        missing_balances.append("威望")
    if missing_balances:
        raise ValueError(
            "积分页面结构已变化，未找到" + "和".join(missing_balances) + "余额",
        )
    return copper_match.group(1), reputation_match.group(1)


def extract_mobile_login_failure(response: requests.Response) -> str:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" not in content_type:
        return ""

    try:
        payload = response.json()
    except ValueError:
        return "登录接口返回了无效 JSON"
    if not isinstance(payload, dict):
        return "登录接口 JSON 结构异常"

    message = payload.get("Message")
    if not isinstance(message, dict):
        return ""
    message_code = normalize_text(str(message.get("messageval") or ""))
    message_text = normalize_text(str(message.get("messagestr") or ""))
    if message_code == "login_succeed":
        return ""
    return message_text or message_code


def is_authenticated_sign_page(response: requests.Response) -> bool:
    return not is_login_required_response(response) and bool(
        extract_form_hash_if_present(response.text),
    )


def is_login_required_response(response: requests.Response) -> bool:
    parsed_url = urlsplit(response.url)
    redirected_to_login = (
        parsed_url.path.endswith("/member.php")
        and "action=login" in parsed_url.query
    )
    if redirected_to_login:
        return True

    page_text = response.text
    login_form_present = re.search(
        r"<form\b[^>]*(?:id|name)=[\"']login",
        page_text,
        flags=re.IGNORECASE,
    )
    login_message_present = contains_any(
        page_text,
        ("您需要先登录", "请登录后使用快捷导航", "请先登录"),
    )
    return bool(login_form_present or login_message_present)


def extract_form_hash_if_present(page_text: str) -> str:
    try:
        return extract_form_hash(page_text)
    except ValueError:
        return ""


def classify_sign_response(response_text: str) -> OperationResult:
    response_message = extract_discuz_message(response_text)
    searchable_text = normalize_text(html.unescape(response_text))

    if contains_any(
        searchable_text,
        ("已经签到", "已签到", "今日已签", "无需重复签到"),
    ):
        return OperationResult(
            True,
            "今日已签到",
            response_message or "今日已经签到，无需重复操作",
        )
    if contains_any(
        searchable_text,
        ("签到成功", "恭喜签到", "签到领奖成功"),
    ):
        return OperationResult(
            True,
            "签到成功",
            response_message or "每日签到完成",
        )
    if contains_any(searchable_text, ("需要先登录", "请先登录", "请登录")):
        return OperationResult(False, "签到失败", "登录状态已失效")
    return OperationResult(
        False,
        "签到失败",
        response_message or "签到接口返回了无法识别的结果",
    )


def classify_task_apply_response(response_text: str) -> OperationResult:
    response_message = extract_discuz_message(response_text)
    searchable_text = normalize_text(html.unescape(response_text))

    already_applied_keywords = (
        "已申请过此任务",
        "已经申请过此任务",
        "已申请了此任务",
        "已经申请了此任务",
        "正在执行此任务",
        "已经完成过此任务",
        "任务已完成",
        "本期您已申请",
        "请勿重复申请",
    )
    if contains_any(searchable_text, already_applied_keywords):
        return OperationResult(
            True,
            "任务已申请",
            response_message or "威望红包任务已经申请",
        )
    if contains_any(
        searchable_text,
        ("任务申请成功", "成功申请", "申请任务成功"),
    ):
        return OperationResult(
            True,
            "申请成功",
            response_message or "威望红包任务申请成功",
        )
    if contains_any(
        searchable_text,
        ("任务已成功完成", "任务成功完成"),
    ):
        return OperationResult(
            True,
            "任务已完成",
            response_message or "威望红包任务已成功完成",
        )
    if contains_any(
        searchable_text,
        ("任务不存在", "任务已关闭", "无权申请", "不能申请", "申请失败"),
    ):
        return OperationResult(
            False,
            "申请失败",
            response_message or "威望红包任务当前不可申请",
        )
    return OperationResult(
        False,
        "申请结果未知",
        response_message or "任务接口返回了无法识别的结果",
    )


def extract_discuz_message(response_text: str) -> str:
    cdata_match = re.search(
        r"<!\[CDATA\[(.*?)\]\]>",
        response_text,
        flags=re.DOTALL,
    )
    message_source = cdata_match.group(1) if cdata_match else response_text
    message_patterns = (
        r'<div\b[^>]*(?:id|class)=["\'][^"\']*messagetext[^"\']*["\'][^>]*>'
        r".*?<p\b[^>]*>(.*?)</p>",
        r'<div\b[^>]*class=["\'][^"\']*\bc\b[^"\']*["\'][^>]*>(.*?)</div>',
    )
    for pattern in message_patterns:
        message_match = re.search(
            pattern,
            message_source,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if message_match:
            message = clean_html_text(message_match.group(1))
            if message:
                return truncate_message(message)

    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        message_source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return truncate_message(clean_html_text(without_scripts))


def clean_html_text(value: str) -> str:
    return normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def truncate_message(value: str, maximum_length: int = 240) -> str:
    if len(value) <= maximum_length:
        return value
    return f"{value[:maximum_length - 3]}..."


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    normalized_cookie_header = re.sub(
        r"^\s*cookie\s*:\s*",
        "",
        cookie_header,
        flags=re.IGNORECASE,
    )
    parsed_cookie = SimpleCookie()
    try:
        parsed_cookie.load(normalized_cookie_header)
    except Exception as error:
        raise ValueError("Cookie 格式无法解析") from error
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


def parse_account_configurations(
    raw_accounts: str,
) -> tuple[list[AccountConfiguration], list[str]]:
    configurations: list[AccountConfiguration] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        account_line = raw_line.strip()
        if not account_line:
            continue

        account_parts = account_line.split("|", maxsplit=2)
        if len(account_parts) == 1:
            cookie = account_parts[0].strip()
            if cookie and "=" in cookie:
                configurations.append(AccountConfiguration(cookie=cookie))
            else:
                configuration_errors.append(
                    f"第 {line_number} 行格式错误，应为用户名|密码或纯 Cookie",
                )
            continue

        identifier = account_parts[0].strip()
        password = account_parts[1].strip()
        cookie = account_parts[2].strip() if len(account_parts) == 3 else ""
        if not identifier or not password:
            configuration_errors.append(
                f"第 {line_number} 行用户名或密码为空",
            )
            continue
        configurations.append(
            AccountConfiguration(
                identifier=identifier,
                password=password,
                cookie=cookie,
            )
        )

    return configurations, configuration_errors


def normalize_sign_mood(raw_mood: str) -> str:
    sign_mood = raw_mood.strip().lower() or DEFAULT_SIGN_MOOD
    if not re.fullmatch(r"[a-z0-9_-]{1,20}", sign_mood):
        print(
            f"[配置] QMJ_SIGN_MOOD={raw_mood!r} 无效，"
            f"使用默认值 {DEFAULT_SIGN_MOOD}",
        )
        return DEFAULT_SIGN_MOOD
    return sign_mood


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


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


def build_notification_content(
    results: list[AccountResult],
    configuration_errors: list[str],
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"阡陌居签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_sections = [
        "<b>阡陌居每日签到</b>",
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
        "阡陌居每日签到",
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
            f"• 登录：{html_code(result.login_method)}",
            f"• 状态：<b>{escape_html_text(result.status)}</b>",
            f"• 当前铜币：{html_code(result.copper_balance or '未获取')}",
            f"• 当前威望：{html_code(result.reputation_balance or '未获取')}",
        ]
        plain_account_lines = [
            f"{result_icon} 账号 {result.account_number} · {result.account_label}",
            f"• 登录：{result.login_method}",
            f"• 状态：{result.status}",
            f"• 当前铜币：{result.copper_balance or '未获取'}",
            f"• 当前威望：{result.reputation_balance or '未获取'}",
        ]
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


def print_result(result: AccountResult) -> None:
    result_marker = "成功" if result.success else "失败"
    print(f"[{result_marker}] 账号 {result.account_number}（{result.account_label}）")
    print(f"  登录方式：{result.login_method}")
    print(f"  状态：{result.status}")
    print(f"  签到：{result.sign_result.status} — {result.sign_result.message}")
    print(f"  威望任务：{result.task_result.status} — {result.task_result.message}")
    if result.copper_balance:
        print(f"  当前铜币：{result.copper_balance}")
    if result.reputation_balance:
        print(f"  当前威望：{result.reputation_balance}")


def main() -> int:
    print(f"==== 阡陌居签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    configurations, configuration_errors = parse_account_configurations(
        os.getenv("QMJ_ACCOUNTS", ""),
    )
    if not configurations and not configuration_errors:
        configuration_errors.append("未配置 QMJ_ACCOUNTS 环境变量")
    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    privacy_mode = read_boolean_environment("QMJ_PRIVACY_MODE", True)
    sign_mood = normalize_sign_mood(os.getenv("QMJ_SIGN_MOOD", DEFAULT_SIGN_MOOD))

    apply_startup_random_delay(
        "阡陌居签到",
        runtime_settings,
        has_work=bool(configurations),
    )

    results: list[AccountResult] = []
    for account_number, configuration in enumerate(configurations, start=1):
        account_label = (
            mask_identifier(configuration.identifier)
            if configuration.identifier and privacy_mode
            else configuration.identifier or "Cookie 账号"
        )
        print(f"\n---- 账号 {account_number}（{account_label}）开始 ----")
        client = QianmojuCheckinClient(
            configuration=configuration,
            request_timeout_seconds=runtime_settings.request_timeout_seconds,
            sign_mood=sign_mood,
            privacy_mode=privacy_mode,
        )
        result = client.run(account_number)
        results.append(result)
        print_result(result)

        wait_between_accounts(
            account_number,
            len(configurations),
            runtime_settings.account_delay_seconds,
        )

    notification_title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
    )
    print("\n==== 阡陌居签到汇总 ====")
    print(plain_content)

    if read_boolean_environment("QMJ_NOTIFY", True):
        send_notifications(
            notification_title,
            html_content,
            plain_content,
            runtime_settings.request_timeout_seconds,
        )
    else:
        print("[通知] QMJ_NOTIFY 已关闭，跳过通知")

    all_accounts_succeeded = bool(results) and all(result.success for result in results)
    configuration_is_valid = not configuration_errors
    return 0 if all_accounts_succeeded and configuration_is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
