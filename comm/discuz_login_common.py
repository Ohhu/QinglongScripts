#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discuz 论坛每日登录任务的共用实现。"""

from __future__ import annotations

import builtins
import hashlib
import html
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
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


EASYOCR_API_URL = "https://console.easyocr.org/api/ocr"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
DEFAULT_RANDOM_DELAY_MAX_SECONDS = 3600.0
CAPTCHA_MAX_ATTEMPTS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

LOGIN_PAGE_PATH = "member.php?mod=logging&action=login"
CREDIT_PAGE_PATH = (
    "home.php?mod=spacecp&ac=credit&showcredit=1"
    "&inajax=1&ajaxtarget=extcreditmenu_menu"
)
FALLBACK_HTTP_ENCODINGS = {"iso-8859-1", "latin-1"}


@dataclass(frozen=True)
class DiscuzSiteConfiguration:
    site_key: str
    site_name: str
    task_name: str
    base_url: str
    verification_path: str
    accounts_environment_name: str
    notify_environment_name: str
    privacy_environment_name: str
    connection_error_message: str

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/") + "/"


@dataclass(frozen=True)
class AccountConfiguration:
    identifier: str = ""
    password: str = ""
    cookie: str = ""

    @property
    def login_field(self) -> str:
        return "email" if "@" in self.identifier else "username"

    @property
    def configured_label(self) -> str:
        return self.identifier or "Cookie 账号"


@dataclass
class LoginResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    login_method: str = "未登录"
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoginForm:
    action: str
    hidden_fields: dict[str, str]
    field_names: frozenset[str]
    captcha_image_url: str = ""
    captcha_hash: str = ""
    captcha_module_id: str = ""

    @property
    def requires_captcha(self) -> bool:
        return "seccodeverify" in self.field_names or bool(self.captcha_hash)


@dataclass(frozen=True)
class CaptchaChallenge:
    image_url: str
    hidden_fields: dict[str, str]


class LoginFormParser(HTMLParser):
    """提取 Discuz 登录表单，避免依赖标签属性的固定顺序。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.hidden_fields: dict[str, str] = {}
        self.field_names: set[str] = set()
        self.captcha_image_url = ""
        self._inside_login_form = False

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        attribute_map = {
            name.lower(): value or ""
            for name, value in attributes
        }

        if normalized_tag == "form":
            form_name = attribute_map.get("name", "").lower()
            form_identifier = attribute_map.get("id", "").lower()
            is_login_form = form_name == "login" or form_identifier.startswith(
                "loginform_",
            )
            if is_login_form:
                self._inside_login_form = True
                self.action = attribute_map.get("action", "")
            return

        if not self._inside_login_form:
            return

        if normalized_tag in {"input", "select", "textarea"}:
            field_name = attribute_map.get("name", "")
            if field_name:
                self.field_names.add(field_name)
                if (
                    normalized_tag == "input"
                    and attribute_map.get("type", "").lower() == "hidden"
                ):
                    self.hidden_fields[field_name] = attribute_map.get("value", "")
            return

        if normalized_tag == "img":
            image_source = attribute_map.get("src", "")
            if "mod=seccode" in image_source:
                self.captcha_image_url = image_source

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._inside_login_form:
            self._inside_login_form = False


class CaptchaUpdateParser(HTMLParser):
    """解析 Discuz 验证码更新接口返回的 JavaScript 内嵌标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_fields: dict[str, str] = {}
        self.image_source = ""

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        attribute_map = {
            name.lower(): value or ""
            for name, value in attributes
        }

        if normalized_tag == "input":
            field_name = attribute_map.get("name", "")
            if (
                attribute_map.get("type", "").lower() == "hidden"
                and field_name in {"seccodehash", "seccodemodid"}
            ):
                self.hidden_fields[field_name] = attribute_map.get("value", "")
            return

        if normalized_tag == "img":
            image_source = attribute_map.get("src", "")
            if "mod=seccode" in image_source:
                self.image_source = image_source


class AuthenticatedPageParser(HTMLParser):
    """从登录后的导航链接中提取当前用户名。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.username = ""
        self._current_profile_link = False
        self._current_text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a" or self.username:
            return

        attribute_map = dict(attributes)
        link_target = html.unescape(attribute_map.get("href") or "")
        is_profile_link = (
            "home.php?mod=space" in link_target
            and re.search(r"(?:[?&])uid=\d+", link_target) is not None
        )
        if is_profile_link:
            self._current_profile_link = True
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_profile_link:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_profile_link:
            return

        candidate_username = normalize_text(" ".join(self._current_text_parts))
        ignored_link_texts = {"访问我的空间", "我的空间", "个人空间"}
        if candidate_username and candidate_username not in ignored_link_texts:
            self.username = candidate_username
        self._current_profile_link = False
        self._current_text_parts = []


def parse_account_configurations(
    raw_accounts: str,
) -> tuple[list[AccountConfiguration], list[str]]:
    configurations: list[AccountConfiguration] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        account_line = raw_line.strip()
        if not account_line:
            continue

        account_parts = [part.strip() for part in account_line.split("|", maxsplit=2)]
        if len(account_parts) == 1 and account_parts[0]:
            configurations.append(AccountConfiguration(cookie=account_parts[0]))
            continue
        if len(account_parts) == 2 and all(account_parts):
            configurations.append(
                AccountConfiguration(
                    identifier=account_parts[0],
                    password=account_parts[1],
                ),
            )
            continue
        if len(account_parts) == 3 and all(account_parts):
            configurations.append(
                AccountConfiguration(
                    identifier=account_parts[0],
                    password=account_parts[1],
                    cookie=account_parts[2],
                ),
            )
            continue

        configuration_errors.append(
            f"第 {line_number} 行格式错误，应为 用户名|密码、"
            "用户名|密码|Cookie 或纯 Cookie",
        )

    return configurations, configuration_errors


def decode_html_response(response: requests.Response) -> str:
    candidate_encodings: list[str] = []
    if response.encoding and response.encoding.lower() not in FALLBACK_HTTP_ENCODINGS:
        candidate_encodings.append(response.encoding)

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
    candidate_encodings.append("utf-8")

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
    return response.content.decode("utf-8", errors="replace")


def parse_login_form(page_text: str, page_url: str) -> LoginForm:
    parser = LoginFormParser()
    parser.feed(page_text)
    if not parser.action:
        raise ValueError("登录页结构已变化，未找到登录表单")

    dynamic_captcha_match = re.search(
        r"updateseccode\(\s*['\"](?P<captcha_hash>[^'\"]+)['\"]\s*,"
        r".*?,\s*['\"](?P<module_id>[^'\"]+)['\"]\s*\)",
        page_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    captcha_hash = parser.hidden_fields.get("seccodehash", "")
    captcha_module_id = parser.hidden_fields.get("seccodemodid", "")
    if dynamic_captcha_match:
        captcha_hash = dynamic_captcha_match.group("captcha_hash")
        captcha_module_id = dynamic_captcha_match.group("module_id")

    captcha_image_url = ""
    if parser.captcha_image_url:
        captcha_image_url = urljoin(page_url, parser.captcha_image_url)

    return LoginForm(
        action=urljoin(page_url, html.unescape(parser.action)),
        hidden_fields=parser.hidden_fields,
        field_names=frozenset(parser.field_names),
        captcha_image_url=captcha_image_url,
        captcha_hash=captcha_hash,
        captcha_module_id=captcha_module_id,
    )


def parse_captcha_update(
    update_text: str,
    update_url: str,
) -> CaptchaChallenge:
    parser = CaptchaUpdateParser()
    parser.feed(update_text)
    if not parser.image_source:
        raise ValueError("登录页要求验证码，但验证码更新响应中未找到图片")

    required_hidden_fields = {"seccodehash", "seccodemodid"}
    if not required_hidden_fields.issubset(parser.hidden_fields):
        raise ValueError("登录页要求验证码，但验证码更新响应缺少必要字段")

    return CaptchaChallenge(
        image_url=urljoin(update_url, html.unescape(parser.image_source)),
        hidden_fields=parser.hidden_fields,
    )


def recognize_image_with_ocr(
    ocr_key: str,
    image_bytes: bytes,
    image_filename: str,
    timeout_seconds: float,
) -> str | None:
    try:
        response = requests.post(
            EASYOCR_API_URL,
            headers={"X-Access-Key": ocr_key},
            files={"file": (image_filename, image_bytes)},
            timeout=timeout_seconds,
        )
        response_data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"[OCR] 识别请求失败：{type(error).__name__}")
        return None

    if response.status_code != 200:
        print(f"[OCR] 识别失败：HTTP {response.status_code}")
        return None

    words = response_data.get("words") or []
    recognized_text = "".join(
        str(word.get("text", ""))
        for word in words
        if isinstance(word, dict)
    )
    captcha_text = "".join(re.findall(r"[a-zA-Z0-9]", recognized_text))
    if not captcha_text:
        print(f"[OCR] 未识别到有效验证码：{response_data.get('result_summary')}")
        return None

    remaining_quota = response_data.get("remaining_quota")
    quota_message = (
        f"（剩余额度 {remaining_quota}）"
        if remaining_quota is not None
        else ""
    )
    print(f"[OCR] 已识别到 {len(captcha_text)} 位验证码{quota_message}")
    return captcha_text


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_html(value: str) -> str:
    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", value, re.DOTALL)
    message_source = cdata_match.group(1) if cdata_match else value
    message_source = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        message_source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return normalize_text(re.sub(r"<[^>]+>", " ", message_source))


def extract_credit_points(page_text: str) -> str:
    plain_text = strip_html(page_text)
    points_match = re.search(
        r"(?:^|\s)积分\s*[:：]\s*([+-]?\d[\d,]*(?:\.\d+)?)",
        plain_text,
    )
    return points_match.group(1) if points_match else ""


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


def is_authenticated_page(page_text: str) -> bool:
    decoded_page_text = html.unescape(page_text)
    return (
        "mod=logging&action=logout" in decoded_page_text
        or "member.php?mod=logging&action=logout" in decoded_page_text
    )


def extract_authenticated_username(page_text: str) -> str:
    parser = AuthenticatedPageParser()
    parser.feed(page_text)
    return parser.username


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    normalized_cookie_header = cookie_header.strip()
    if normalized_cookie_header.lower().startswith("cookie:"):
        normalized_cookie_header = normalized_cookie_header.split(":", maxsplit=1)[1]

    parsed_cookie = SimpleCookie()
    try:
        parsed_cookie.load(normalized_cookie_header)
    except Exception as error:
        raise ValueError("Cookie 格式无效，无法解析") from error

    if not parsed_cookie:
        raise ValueError("Cookie 格式无效，未解析到任何字段")
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


def build_account_storage_key(configuration: AccountConfiguration) -> str:
    if configuration.identifier:
        return configuration.identifier
    cookie_digest = hashlib.sha256(configuration.cookie.encode("utf-8")).hexdigest()
    return f"cookie:{cookie_digest[:16]}"


class DiscuzLoginClient:
    """Cookie 优先、账密登录回退的通用 Discuz 登录客户端。"""

    def __init__(
        self,
        site: DiscuzSiteConfiguration,
        configuration: AccountConfiguration,
        timeout_seconds: float,
        ocr_key: str,
        privacy_mode: bool,
    ) -> None:
        self.site = site
        self.configuration = configuration
        self.timeout_seconds = timeout_seconds
        self.ocr_key = ocr_key
        self.privacy_mode = privacy_mode
        self.session = self._create_session()
        self.cookie_store = CookieStore(f"{site.site_key}_login_cookies")

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": USER_AGENT,
            },
        )
        return session

    def login(self, account_number: int) -> LoginResult:
        account_label = self._display_identifier(
            self.configuration.configured_label,
        )
        try:
            authenticated, login_method, username, message = self._authenticate()
            if not authenticated:
                return self._failure_result(
                    account_number,
                    account_label,
                    message,
                    login_method,
                )

            if username:
                account_label = self._display_identifier(username)
            details: dict[str, str] = {}
            if username:
                details["username"] = self._display_identifier(username)
            credit_points = self._fetch_credit_points()
            if credit_points:
                details["points"] = credit_points
            return LoginResult(
                account_number=account_number,
                account_label=account_label,
                success=True,
                status="登录成功",
                message="每日登录状态已确认",
                login_method=login_method,
                details=details,
            )
        except requests.Timeout:
            return self._failure_result(
                account_number,
                account_label,
                "网络请求超时",
            )
        except requests.ConnectionError:
            return self._failure_result(
                account_number,
                account_label,
                self.site.connection_error_message,
            )
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

    def _authenticate(self) -> tuple[bool, str, str, str]:
        account_key = build_account_storage_key(self.configuration)
        stored_cookie = self.cookie_store.read(account_key)

        cookie_candidates = [
            ("本地 Cookie", stored_cookie),
            ("环境变量 Cookie", self.configuration.cookie),
        ]
        cookie_failure_messages: list[str] = []
        for login_method, cookie_value in cookie_candidates:
            if not cookie_value:
                continue

            self.session.close()
            self.session = self._create_session()
            try:
                load_cookie_header(self.session, cookie_value)
                authenticated, username = self._verify_session()
            except ValueError as error:
                authenticated, username = False, ""
                cookie_failure_messages.append(f"{login_method}：{error}")

            if authenticated:
                refreshed_cookie = export_session_cookie_header(self.session)
                if refreshed_cookie:
                    self.cookie_store.write(account_key, refreshed_cookie)
                return True, login_method, username, "Cookie 登录状态有效"

            cookie_failure_messages.append(f"{login_method} 已失效")
            if login_method == "本地 Cookie":
                self.cookie_store.remove(account_key)
            print(f"[登录] {login_method} 不可用")

        if not self.configuration.identifier or not self.configuration.password:
            if cookie_failure_messages:
                return False, "Cookie", "", "；".join(cookie_failure_messages)
            return False, "未配置", "", "未配置 Cookie 或完整账号密码"

        self.session.close()
        self.session = self._create_session()
        authenticated, username, login_message = self._login_with_password()
        if not authenticated:
            combined_message = login_message
            if cookie_failure_messages:
                combined_message = f"{'；'.join(cookie_failure_messages)}；{login_message}"
            return False, "账号密码", "", combined_message

        current_cookie = export_session_cookie_header(self.session)
        if current_cookie:
            self.cookie_store.write(account_key, current_cookie)
            print("[Cookie存储] 已保存本次登录会话")
        return True, "账号密码", username, login_message

    def _login_with_password(self) -> tuple[bool, str, str]:
        last_message = "登录失败"
        for attempt_number in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            login_page_response = self.session.get(
                urljoin(self.site.normalized_base_url, LOGIN_PAGE_PATH),
                timeout=self.timeout_seconds,
            )
            login_page_response.raise_for_status()
            login_page_text = decode_html_response(login_page_response)
            login_form = parse_login_form(login_page_text, login_page_response.url)

            login_payload = {
                **login_form.hidden_fields,
                "loginfield": self.configuration.login_field,
                "username": self.configuration.identifier,
                "password": self.configuration.password,
                "questionid": "0",
                "answer": "",
                "cookietime": "2592000",
                "loginsubmit": "true",
            }

            if login_form.requires_captcha:
                if not self.ocr_key:
                    return False, "", "登录页要求验证码，但未配置 OCR_KEY"

                captcha_challenge = self._load_login_captcha_challenge(
                    login_form,
                    login_page_response.url,
                )

                captcha_text = self._recognize_login_captcha(
                    captcha_challenge.image_url,
                    login_page_response.url,
                )
                if not captcha_text:
                    last_message = f"第 {attempt_number} 次验证码识别失败"
                    continue
                login_payload.update(captcha_challenge.hidden_fields)
                login_payload["seccodeverify"] = captcha_text

            login_response = self.session.post(
                login_form.action,
                data=login_payload,
                headers={
                    "Origin": self.site.normalized_base_url.rstrip("/"),
                    "Referer": login_page_response.url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
            login_response.raise_for_status()
            login_response_message = strip_html(decode_html_response(login_response))

            authenticated, username = self._verify_session()
            if authenticated:
                return True, username, login_response_message or "登录成功"

            last_message = login_response_message or "登录后未建立会话"
            captcha_failed = contains_any(
                last_message,
                ("验证码填写错误", "验证码错误", "验证码不正确", "验证码"),
            )
            if captcha_failed and attempt_number < CAPTCHA_MAX_ATTEMPTS:
                print(
                    f"[登录] 第 {attempt_number} 次验证码未通过，"
                    "刷新后重试",
                )
                continue
            break

        return False, "", f"登录失败：{last_message}"

    def _load_login_captcha_challenge(
        self,
        login_form: LoginForm,
        login_page_url: str,
    ) -> CaptchaChallenge:
        captcha_hidden_fields = {
            field_name: field_value
            for field_name, field_value in login_form.hidden_fields.items()
            if field_name in {"seccodehash", "seccodemodid"}
        }
        if login_form.captcha_hash:
            captcha_hidden_fields.setdefault(
                "seccodehash",
                login_form.captcha_hash,
            )
        if login_form.captcha_module_id:
            captcha_hidden_fields.setdefault(
                "seccodemodid",
                login_form.captcha_module_id,
            )

        if login_form.captcha_image_url:
            required_hidden_fields = {"seccodehash", "seccodemodid"}
            if not required_hidden_fields.issubset(captcha_hidden_fields):
                raise ValueError("登录页要求验证码，但验证码表单缺少必要字段")
            return CaptchaChallenge(
                image_url=login_form.captcha_image_url,
                hidden_fields=captcha_hidden_fields,
            )

        if not login_form.captcha_hash or not login_form.captcha_module_id:
            raise ValueError("登录页要求验证码，但未找到验证码动态加载参数")

        print("[验证码] 检测到动态验证码，正在加载验证码图片")
        update_response = self.session.get(
            urljoin(login_page_url, "misc.php"),
            params={
                "mod": "seccode",
                "action": "update",
                "idhash": login_form.captcha_hash,
                "modid": login_form.captcha_module_id,
                "_": str(time.time_ns()),
            },
            headers={
                "Referer": login_page_url,
                "Accept": "application/javascript,*/*;q=0.8",
            },
            timeout=self.timeout_seconds,
        )
        update_response.raise_for_status()
        return parse_captcha_update(
            decode_html_response(update_response),
            update_response.url,
        )

    def _recognize_login_captcha(
        self,
        captcha_image_url: str,
        referer_url: str,
    ) -> str | None:
        image_response = self.session.get(
            captcha_image_url,
            headers={"Referer": referer_url, "Accept": "image/avif,image/webp,*/*"},
            timeout=self.timeout_seconds,
        )
        image_response.raise_for_status()
        return recognize_image_with_ocr(
            self.ocr_key,
            image_response.content,
            "discuz-login-captcha.png",
            self.timeout_seconds,
        )

    def _verify_session(self) -> tuple[bool, str]:
        verification_url = urljoin(
            self.site.normalized_base_url,
            self.site.verification_path,
        )
        response = self.session.get(
            verification_url,
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        page_text = decode_html_response(response)
        if not is_authenticated_page(page_text):
            return False, ""
        return True, extract_authenticated_username(page_text)

    def _fetch_credit_points(self) -> str:
        try:
            response = self.session.get(
                urljoin(self.site.normalized_base_url, CREDIT_PAGE_PATH),
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            print(f"[积分] 查询失败：{describe_request_error(error)}")
            return ""

        credit_points = extract_credit_points(decode_html_response(response))
        if credit_points:
            print(f"[积分] 当前积分：{credit_points}")
        else:
            print("[积分] 积分页面中未找到余额")
        return credit_points

    def _display_identifier(self, identifier: str) -> str:
        if not self.privacy_mode:
            return identifier
        return mask_identifier(identifier)

    @staticmethod
    def _failure_result(
        account_number: int,
        account_label: str,
        message: str,
        login_method: str = "未登录",
    ) -> LoginResult:
        return LoginResult(
            account_number=account_number,
            account_label=account_label,
            success=False,
            status="失败",
            message=message,
            login_method=login_method,
        )


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def mask_identifier(identifier: str) -> str:
    if not identifier or identifier == "Cookie 账号":
        return "Cookie 账号"
    if len(identifier) <= 1:
        return "*"
    if len(identifier) == 2:
        return f"{identifier[0]}*"
    return f"{identifier[0]}***{identifier[-1]}"


def escape_html_text(value: Any) -> str:
    return html.escape(str(value).replace("\n", " "), quote=False)


def html_code(value: Any) -> str:
    return f"<code>{escape_html_text(value)}</code>"


def build_notification_content(
    site: DiscuzSiteConfiguration,
    results: list[LoginResult],
    configuration_errors: list[str],
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"{site.task_name} {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_sections = [
        f"<b>{escape_html_text(site.task_name)}</b>",
        "\n".join(
            [
                "<b>执行概览</b>",
                f"• 成功：{html_code(successful_count)}",
                f"• 失败：{html_code(failed_count)}",
                f"• 时间：{html_code(execution_time)}",
            ],
        ),
    ]
    plain_sections = [
        site.task_name,
        "\n".join(
            [
                "执行概览",
                f"• 成功：{successful_count}",
                f"• 失败：{failed_count}",
                f"• 时间：{execution_time}",
            ],
        ),
    ]

    for result in results:
        result_icon = "✅" if result.success else "❌"
        html_account_lines = [
            f"{result_icon} <b>账号 {result.account_number} · "
            f"{escape_html_text(result.account_label)}</b>",
            f"• 登录方式：{escape_html_text(result.login_method)}",
            f"• 状态：<b>{escape_html_text(result.status)}</b>",
            f"• 说明：{escape_html_text(result.message)}",
        ]
        plain_account_lines = [
            f"{result_icon} 账号 {result.account_number} · {result.account_label}",
            f"• 登录方式：{result.login_method}",
            f"• 状态：{result.status}",
            f"• 说明：{result.message}",
        ]
        if result.details.get("username"):
            username = result.details["username"]
            html_account_lines.append(f"• 用户名：{html_code(username)}")
            plain_account_lines.append(f"• 用户名：{username}")
        if result.details.get("points"):
            credit_points = result.details["points"]
            html_account_lines.append(f"• 积分：{html_code(credit_points)}")
            plain_account_lines.append(f"• 积分：{credit_points}")
        html_sections.append("\n".join(html_account_lines))
        plain_sections.append("\n".join(plain_account_lines))

    if configuration_errors:
        html_error_lines = ["<b>配置提示</b>"]
        html_error_lines.extend(
            f"• {escape_html_text(error)}" for error in configuration_errors
        )
        html_sections.append("\n".join(html_error_lines))
        plain_sections.append(
            "\n".join(["配置提示", *(f"• {error}" for error in configuration_errors)]),
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
        response_data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"[通知] Telegram HTML 直发失败：{type(error).__name__}")
        return False
    if response.status_code != 200 or not response_data.get("ok"):
        print(f"[通知] Telegram HTML 直发失败：HTTP {response.status_code}")
        return False
    print("[通知] Telegram HTML 直发成功")
    return True


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
    print("[通知] 青龙面板系统通知调用完成")
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
    print(f"  登录方式：{result.login_method}")
    print(f"  状态：{result.status}")
    print(f"  说明：{result.message}")
    detail_labels = {
        "username": "用户名",
        "points": "积分",
    }
    for detail_name, detail_value in result.details.items():
        detail_label = detail_labels.get(detail_name, detail_name)
        print(f"  {detail_label}：{detail_value}")


def run_discuz_login(site: DiscuzSiteConfiguration) -> int:
    print(
        f"==== {site.task_name}开始 "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====",
    )

    raw_accounts = os.getenv(site.accounts_environment_name, "")
    configurations, configuration_errors = parse_account_configurations(raw_accounts)
    if not configurations and not configuration_errors:
        configuration_errors.append(
            f"未配置 {site.accounts_environment_name} 环境变量",
        )
    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
        default_random_delay_max_seconds=DEFAULT_RANDOM_DELAY_MAX_SECONDS,
    )
    timeout_seconds = runtime_settings.request_timeout_seconds
    privacy_mode = read_boolean_environment(site.privacy_environment_name, True)

    apply_startup_random_delay(
        site.task_name,
        runtime_settings,
        has_work=bool(configurations),
    )

    ocr_key = os.getenv("OCR_KEY", "").strip()
    results: list[LoginResult] = []
    for account_index, configuration in enumerate(configurations, start=1):
        configured_label = (
            mask_identifier(configuration.configured_label)
            if privacy_mode
            else configuration.configured_label
        )
        print(f"\n---- 账号 {account_index}（{configured_label}）开始 ----")
        client = DiscuzLoginClient(
            site,
            configuration,
            timeout_seconds,
            ocr_key,
            privacy_mode,
        )
        result = client.login(account_index)
        results.append(result)
        print_result(result)

        wait_between_accounts(
            account_index,
            len(configurations),
            runtime_settings.account_delay_seconds,
        )

    notification_title, html_content, plain_content = build_notification_content(
        site,
        results,
        configuration_errors,
    )
    print(f"\n==== {site.task_name}汇总 ====")
    print(plain_content)

    if read_boolean_environment(site.notify_environment_name, True):
        send_notifications(
            notification_title,
            html_content,
            plain_content,
            timeout_seconds,
        )
    else:
        print(f"[通知] {site.notify_environment_name} 已关闭通知")

    all_succeeded = bool(results) and all(result.success for result in results)
    configuration_valid = not configuration_errors
    return 0 if all_succeeded and configuration_valid else 1


def execute_site_login(site: DiscuzSiteConfiguration) -> None:
    try:
        exit_code = run_discuz_login(site)
    except KeyboardInterrupt:
        print("\n任务已被用户中断")
        exit_code = 130
    sys.exit(exit_code)
