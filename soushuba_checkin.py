#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 0 9 * * *
new Env('搜书吧每日登录')

环境变量：
  SOUSHUBA_ACCOUNTS    必填。账号和密码，格式为“账号|密码”，多账号使用换行分隔。
  SOUSHUBA_HOSTNAME    可选。入口域名，默认 www.soushu2035.com；脚本会自动跟随跳转发现真实域名。
  TASK_RANDOM_SIGNIN   是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT         单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY   多账号之间的等待秒数，默认为 3（所有任务共用）。
  TG_NOTIFY_CONFIG     可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                       配置后使用 HTML 直发，失败回退青龙纯文本通知。

账号密码登录成功后，会把会话保存到
/ql/data/scripts_data/soushuba_login_cookies.json，后续运行优先复用。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import html
import os
import re
import sys
import urllib3
from dataclasses import dataclass, field
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
    wait_between_accounts,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_ENTRY_HOSTNAME = "www.soushu2035.com"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
MAX_DISCOVERY_HOPS = 10
DEFAULT_HTML_ENCODING = "utf-8"
FALLBACK_HTTP_ENCODINGS = {"iso-8859-1", "latin-1"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Discuz! formhash
FORMHASH_PATTERN = re.compile(
    r'<input\s+type="hidden"\s+name="formhash"\s+value="([^"]+)"',
    re.IGNORECASE,
)
# Discuz! loginhash（模板里 main_messaqge 是历史拼写，兼容 main_message）
LOGINHASH_PATTERN = re.compile(
    r'<div\s+id="main_messa\w*e_([^"]+)"',
    re.IGNORECASE,
)
COOKIE_STORE = CookieStore("soushuba_login_cookies")


@dataclass(frozen=True)
class AccountCredential:
    username: str
    password: str


@dataclass
class LoginResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


class DiscoveryPageParser(HTMLParser):
    """提取导航页中的 meta refresh 和链接，避免依赖固定属性顺序。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_refresh_contents: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.current_link_href: str | None = None
        self.current_link_text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_attributes = {
            name.lower(): value or ""
            for name, value in attrs
        }

        if tag.lower() == "meta":
            http_equiv = normalized_attributes.get("http-equiv", "").lower()
            refresh_content = normalized_attributes.get("content", "")
            if http_equiv == "refresh" and refresh_content:
                self.meta_refresh_contents.append(refresh_content)

        if tag.lower() == "a":
            self.current_link_href = normalized_attributes.get("href")
            self.current_link_text_parts = []

    def handle_data(self, data: str) -> None:
        if self.current_link_href is not None:
            self.current_link_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self.current_link_href is None:
            return

        link_text = " ".join(self.current_link_text_parts)
        link_text = re.sub(r"\s+", " ", link_text).strip()
        self.links.append((self.current_link_href, link_text))
        self.current_link_href = None
        self.current_link_text_parts = []


def parse_discovery_page(page_text: str) -> DiscoveryPageParser:
    parser = DiscoveryPageParser()
    parser.feed(page_text)
    return parser


def decode_html_response(response: requests.Response) -> str:
    """按响应头或 HTML meta 声明解码，避免无 charset 时中文被按 Latin-1 解析。"""
    candidate_encodings: list[str] = []

    response_encoding = response.encoding
    if (
        response_encoding
        and response_encoding.lower() not in FALLBACK_HTTP_ENCODINGS
    ):
        candidate_encodings.append(response_encoding)

    document_prefix = response.content[:4096]
    meta_charset_match = re.search(
        br'<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9._-]+)',
        document_prefix,
        re.IGNORECASE,
    )
    if meta_charset_match:
        candidate_encodings.append(
            meta_charset_match.group(1).decode("ascii", errors="ignore"),
        )

    if response.apparent_encoding:
        candidate_encodings.append(response.apparent_encoding)
    candidate_encodings.append(DEFAULT_HTML_ENCODING)

    attempted_encodings: set[str] = set()
    for candidate_encoding in candidate_encodings:
        normalized_encoding = candidate_encoding.strip().lower()
        if not normalized_encoding or normalized_encoding in attempted_encodings:
            continue

        attempted_encodings.add(normalized_encoding)
        try:
            return response.content.decode(candidate_encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return response.content.decode(DEFAULT_HTML_ENCODING, errors="replace")


def extract_forum_hostname(
    parser: DiscoveryPageParser,
    current_url: str,
) -> str | None:
    for link_href, link_text in parser.links:
        if "搜书吧" not in link_text:
            continue

        resolved_url = urljoin(current_url, link_href)
        parsed_url = urlsplit(resolved_url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.hostname:
            return parsed_url.hostname

    return None


def extract_meta_refresh_url(
    parser: DiscoveryPageParser,
    current_url: str,
) -> str | None:
    for refresh_content in parser.meta_refresh_contents:
        redirect_match = re.search(
            r"(?:^|;)\s*url\s*=\s*(.+?)\s*$",
            refresh_content,
            re.IGNORECASE,
        )
        if not redirect_match:
            continue

        redirect_url = redirect_match.group(1).strip("'\"")
        if redirect_url:
            return urljoin(current_url, redirect_url)

    return None


def discover_forum_hostname(
    entry_hostname: str,
    timeout_seconds: float,
) -> str:
    """跟随 meta refresh 跳转，直到找到「搜书吧」链接，返回其 hostname。"""
    current_url = f"http://{entry_hostname}"
    last_status_code: int | None = None

    with requests.Session() as discovery_session:
        discovery_session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": USER_AGENT,
            }
        )

        for hop_number in range(1, MAX_DISCOVERY_HOPS + 1):
            try:
                response = discovery_session.get(
                    current_url,
                    timeout=timeout_seconds,
                    verify=False,
                    allow_redirects=True,
                )
            except requests.RequestException as error:
                raise ValueError(
                    f"域名发现第 {hop_number} 跳请求失败："
                    f"{describe_request_error(error)}（{current_url}）",
                ) from error

            last_status_code = response.status_code
            current_url = response.url
            parser = parse_discovery_page(decode_html_response(response))

            forum_hostname = extract_forum_hostname(parser, current_url)
            if forum_hostname:
                return forum_hostname

            refresh_url = extract_meta_refresh_url(parser, current_url)
            if refresh_url:
                current_url = refresh_url
                continue

            break

    raise ValueError(
        f"未能从入口域名 {entry_hostname} 发现搜书吧真实域名，"
        f"最后访问 {current_url}（HTTP {last_status_code or '未知'}）；"
        "请检查导航页结构或更新 SOUSHUBA_HOSTNAME 环境变量",
    )


class SouShuBaClient:
    """登录搜书吧 Discuz! 论坛，完成每日登录。"""

    def __init__(
        self,
        hostname: str,
        credential: AccountCredential,
        timeout_seconds: float,
    ) -> None:
        self.hostname = hostname
        self.credential = credential
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
            }
        )

    def login(self, account_number: int) -> LoginResult:
        account_label = mask_identifier(self.credential.username)
        try:
            stored_cookie = COOKIE_STORE.read(self.credential.username)
            if stored_cookie:
                try:
                    load_cookie_header(self.session, stored_cookie)
                except ValueError as error:
                    print(f"[登录] {error}，回退账号密码")
                    COOKIE_STORE.remove(self.credential.username)
                else:
                    if self._is_session_established():
                        refreshed_cookie = export_session_cookie_header(self.session)
                        if refreshed_cookie:
                            COOKIE_STORE.write(
                                self.credential.username,
                                refreshed_cookie,
                            )
                        details = self._extract_user_details()
                        return LoginResult(
                            account_number=account_number,
                            account_label=details.get("username") or account_label,
                            success=True,
                            status="登录成功",
                            message="本地 Cookie 登录状态有效",
                            details=details,
                        )
                    print("[登录] 本地 Cookie 已失效，回退账号密码")
                    COOKIE_STORE.remove(self.credential.username)
                self.session.cookies.clear()

            login_hash, form_hash = self._fetch_login_tokens()
            login_response_text = self._submit_login(login_hash, form_hash)

            message = extract_cdata_content(login_response_text)
            if contains_any(
                message,
                ("登录失败", "密码错误", "密码不正确", "验证"),
            ):
                raise ValueError(f"登录失败：{strip_html(message)}")

            if not self._is_session_established():
                raise ValueError("登录后未建立会话，请检查账号密码")

            current_cookie = export_session_cookie_header(self.session)
            if current_cookie:
                COOKIE_STORE.write(self.credential.username, current_cookie)
                print("[Cookie存储] 已保存最新 Cookie")
            details = self._extract_user_details()

            return LoginResult(
                account_number=account_number,
                account_label=details.get("username") or account_label,
                success=True,
                status="登录成功",
                message="每日登录完成",
                details=details,
            )

        except requests.Timeout:
            return self._failure_result(account_number, account_label, "网络请求超时")
        except requests.ConnectionError:
            return self._failure_result(account_number, account_label, "无法连接搜书吧")
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

    def _fetch_login_tokens(self) -> tuple[str, str]:
        response = self.session.get(
            f"https://{self.hostname}/member.php?mod=logging&action=login",
            timeout=self.timeout_seconds,
            verify=False,
        )
        response.raise_for_status()

        login_hash_match = LOGINHASH_PATTERN.search(response.text)
        form_hash_match = FORMHASH_PATTERN.search(response.text)

        if not login_hash_match or not form_hash_match:
            raise ValueError("登录页结构已变化，未找到 loginhash 或 formhash")

        return login_hash_match.group(1), form_hash_match.group(1)

    def _submit_login(self, login_hash: str, form_hash: str) -> str:
        login_url = (
            f"https://{self.hostname}/member.php?mod=logging&action=login"
            f"&loginsubmit=yes&handlekey=register&loginhash={login_hash}&inajax=1"
        )
        payload = {
            "formhash": form_hash,
            "referer": f"https://{self.hostname}/",
            "username": self.credential.username,
            "password": self.credential.password,
            "questionid": "0",
            "answer": "",
        }
        response = self.session.post(
            login_url,
            data=payload,
            headers={
                "Origin": f"https://{self.hostname}",
                "Referer": f"https://{self.hostname}/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout_seconds,
            verify=False,
        )
        response.raise_for_status()
        return response.text

    def _is_session_established(self) -> bool:
        """登录后访问首页，检查是否出现退出链接，确认会话已建立。

        Discuz! 模板里 HTML 实体编码后的 &amp; 会出现，所以匹配 action=logout
        而不是 &action=logout。
        """
        response = self.session.get(
            f"https://{self.hostname}/",
            timeout=self.timeout_seconds,
            verify=False,
        )
        response.raise_for_status()
        page_text = response.text
        return "action=logout" in page_text or "退出" in page_text

    def _extract_user_details(self) -> dict[str, str]:
        """尝试获取用户名和银币余额，失败不影响登录结果。"""
        details: dict[str, str] = {}
        try:
            # 先从首页抓用户名（home.php?mod=space&uid=xxx 链接的文本）
            home_response = self.session.get(
                f"https://{self.hostname}/",
                timeout=self.timeout_seconds,
                verify=False,
            )
            if home_response.status_code == 200:
                username_match = re.search(
                    r'<a\s+href="home\.php\?mod=space&(?:amp;)?uid=\d+"[^>]*>([^<]+)</a>',
                    home_response.text,
                )
                if username_match:
                    username = username_match.group(1).strip()
                    if username and "访问我的空间" not in username:
                        details["username"] = username

            # 再查 credit 接口拿银币
            credit_response = self.session.get(
                f"https://{self.hostname}/home.php?mod=spacecp&ac=credit"
                "&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu",
                timeout=self.timeout_seconds,
                verify=False,
            )
            if credit_response.status_code == 200:
                credit_match = re.search(
                    r"hcredit_2[^>]*>([^<]+)<",
                    credit_response.text,
                )
                if credit_match:
                    details["credit"] = credit_match.group(1).strip()
        except Exception:
            pass
        return details

    @staticmethod
    def _failure_result(
        account_number: int,
        account_label: str,
        message: str,
    ) -> LoginResult:
        return LoginResult(
            account_number=account_number,
            account_label=account_label,
            success=False,
            status="失败",
            message=message,
        )


def extract_cdata_content(response_text: str) -> str:
    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", response_text, re.DOTALL)
    return cdata_match.group(1) if cdata_match else response_text


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


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
            configuration_errors.append(f"第 {line_number} 行账号或密码为空")
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
    results: list[LoginResult],
    configuration_errors: list[str],
    hostname_error: str | None,
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"搜书吧每日登录 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail_labels = {
        "username": "用户名",
        "credit": "银币",
    }

    html_sections = [
        "<b>搜书吧每日登录</b>",
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
        "搜书吧每日登录",
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

    if hostname_error:
        html_sections.append(
            "\n".join(
                [
                    "<b>域名发现提示</b>",
                    f"• {escape_html_text(hostname_error)}",
                ]
            )
        )
        plain_sections.append(f"域名发现提示\n• {hostname_error}")

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


def print_result(result: LoginResult) -> None:
    marker = "成功" if result.success else "失败"
    print(f"[{marker}] 账号 {result.account_number}（{result.account_label}）")
    print(f"  状态：{result.status}")
    print(f"  说明：{result.message}")
    for key, value in result.details.items():
        print(f"  {key}：{value}")


def main() -> int:
    print(
        f"==== 搜书吧每日登录开始 "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====",
    )

    raw_accounts = os.getenv("SOUSHUBA_ACCOUNTS", "")
    credentials, configuration_errors = parse_accounts(raw_accounts)
    if not credentials and not configuration_errors:
        configuration_errors.append("未配置 SOUSHUBA_ACCOUNTS 环境变量")

    for error in configuration_errors:
        print(f"[配置错误] {error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    timeout_seconds = runtime_settings.request_timeout_seconds

    apply_startup_random_delay(
        "搜书吧登录",
        runtime_settings,
        has_work=bool(credentials),
    )

    # 发现真实论坛域名（所有账号共用）
    entry_hostname = os.getenv("SOUSHUBA_HOSTNAME", DEFAULT_ENTRY_HOSTNAME)
    hostname_error: str | None = None
    forum_hostname: str | None = None
    try:
        print(f"[域名发现] 从入口 {entry_hostname} 开始...")
        forum_hostname = discover_forum_hostname(entry_hostname, timeout_seconds)
        print(f"[域名发现] 真实域名：{forum_hostname}")
    except ValueError as error:
        hostname_error = str(error)
        print(f"[域名发现] {hostname_error}")

    results: list[LoginResult] = []
    if forum_hostname:
        for account_index, credential in enumerate(credentials, start=1):
            masked_label = mask_identifier(credential.username)
            print(f"\n---- 账号 {account_index}（{masked_label}）开始 ----")
            client = SouShuBaClient(forum_hostname, credential, timeout_seconds)
            result = client.login(account_index)
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
        hostname_error,
    )
    print("\n==== 搜书吧每日登录汇总 ====")
    print(plain_content)

    send_notifications(
        notification_title,
        html_content,
        plain_content,
        timeout_seconds,
    )

    all_succeeded = bool(results) and all(r.success for r in results)
    config_valid = not configuration_errors and hostname_error is None
    return 0 if all_succeeded and config_valid else 1


if __name__ == "__main__":
    sys.exit(main())
