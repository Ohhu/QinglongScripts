#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 10 9 * * *
new Env('尚香书苑签到')

环境变量：
  SXSY_ACCOUNTS        必填。每行一个账号，格式如下：
                         用户名|密码         账密登录（需要 OCR）
                         用户名|密码|Cookie  Cookie 优先，失效回退账密
                         第一列支持用户名或邮箱，自动识别（含 @ 视为邮箱）。
                         Cookie 优先级最高：整行不含 | 时视为纯 Cookie。
  OCR_KEY              账密登录或自动域名发现时必填。讯飞图像理解
                       APIKey（服务管控页面获取），所有需要 OCR 的任务共用。
  SXSY_HOST            可选。手动指定站点域名（如 sxsy13.com），
                       设置后跳过发布页域名发现。
  SXSY_PRIVACY_MODE    日志和通知中是否对账号名脱敏，默认为 true。
  TG_NOTIFY_CONFIG     可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                       配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN   是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT         单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY   多账号之间的等待秒数，默认为 3（所有任务共用）。

站点发布页 https://sxsy.org/ 把最新域名放在 site.jpg 图片里。
脚本优先验证上次成功的域名缓存；缓存不可用时下载图片并使用
讯飞 OCR 识别最新域名，再失败则使用内置默认域名。

Cookie 持久化：账密登录成功后把最新 Cookie 存入青龙数据目录
（默认 /ql/data/scripts_data/sxsy_cookies.json），之后运行优先用本地
存储的 Cookie，失效后自动清理并依次回退环境变量 Cookie、账密登录。
旧版 Cookie 文件会自动迁移到统一的多账号存储结构。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import hashlib
import html
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests

from comm.cookie_store import CookieStore
from comm.ocr_client import (
    has_ocr_key as _ocr_has_key,
    recognize_captcha as _ocr_recognize_captcha,
    recognize_text as _ocr_recognize_text,
)
from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)


RELEASE_PAGE_URL = "https://sxsy.org/"
DOMAIN_IMAGE_PATH = "site.jpg"
DEFAULT_HOST = "sxsy13.com"

# 本地数据固定放在青龙持久卷中；目录不存在时直接创建，不把运行数据
# 混入可能被订阅删除或改名的脚本目录。
QINGLONG_DATA_ROOT = os.getenv("QL_DATA_DIR", "").strip() or "/ql/data"
QINGLONG_DATA_DIR = os.path.join(QINGLONG_DATA_ROOT, "scripts_data")
try:
    os.makedirs(QINGLONG_DATA_DIR, exist_ok=True)
except OSError as error:
    raise RuntimeError(
        f"无法创建脚本数据目录 {QINGLONG_DATA_DIR}：{error}",
    ) from error

DOMAIN_CACHE_PATH = os.path.join(QINGLONG_DATA_DIR, "sxsy_last_host")
COOKIE_STORE = CookieStore("sxsy_cookies")

HOST_PATTERN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.IGNORECASE)
FORM_HASH_PATTERN = re.compile(r'name="formhash"\s+value="([a-zA-Z0-9]{8})"')
SECCODE_HASH_PATTERN = re.compile(r"seccode_([a-zA-Z0-9]{6})")
LOGIN_HASH_PATTERN = re.compile(r"loginhash=([a-zA-Z0-9]{5})")
SIGN_HASH_PATTERN = re.compile(r"formhash=([a-zA-Z0-9]{8})")
WELCOME_PATTERN = re.compile(r"欢迎您回来，(.*?)，")
MONEY_PATTERN = re.compile(r"金钱:\s*</em>(\d+)")
MATH_VERIFICATION_PATTERN = re.compile(
    r"签到验证\s*[：:]\s*(-?\d+)\s*([+\-*/×÷xX])\s*(-?\d+)\s*=\s*\?",
)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
CAPTCHA_MAX_ATTEMPTS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class AccountConfiguration:
    identifier: str = ""
    password: str = ""
    cookie: str = ""

    @property
    def login_field(self) -> str:
        """Discuz 登录字段：含 @ 视为邮箱，否则按用户名登录。"""
        return "email" if "@" in self.identifier else "username"

    @property
    def label(self) -> str:
        if self.identifier:
            return self.identifier
        return "Cookie 账号"


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


class MathVerificationError(ValueError):
    """签到算术验证题无法安全解析或计算。"""


def parse_account_configurations(
    raw_accounts: str,
) -> tuple[list[AccountConfiguration], list[str]]:
    configurations: list[AccountConfiguration] = []
    configuration_errors: list[str] = []

    for line_number, raw_line in enumerate(raw_accounts.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 1:
            configurations.append(AccountConfiguration(cookie=parts[0]))
            continue
        if len(parts) == 2 and all(parts):
            configurations.append(
                AccountConfiguration(identifier=parts[0], password=parts[1]),
            )
            continue
        if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
            configurations.append(
                AccountConfiguration(
                    identifier=parts[0],
                    password=parts[1],
                    cookie=parts[2],
                ),
            )
            continue
        configuration_errors.append(
            f"第 {line_number} 行账号格式无效，应为 用户名|密码 或 用户名|密码|Cookie",
        )

    return configurations, configuration_errors


def discover_host_via_ocr(
    timeout_seconds: float,
) -> str | None:
    image_url = urljoin(RELEASE_PAGE_URL, DOMAIN_IMAGE_PATH)
    try:
        image_response = requests.get(
            image_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout_seconds,
        )
        image_response.raise_for_status()
    except requests.RequestException as error:
        print(f"[域名发现] 下载域名图片失败：{describe_request_error(error)}")
        return None

    recognized_text = _ocr_recognize_text(
        image_response.content,
        prompt=(
            "图片中有一个网站域名，请识别并只输出该域名，"
            "不要输出其他任何内容。注意区分数字1和字母l、数字0和字母o。"
        ),
        timeout_seconds=timeout_seconds,
    )
    if not recognized_text:
        return None

    host_match = HOST_PATTERN.search(re.sub(r"\s+", "", recognized_text))
    if not host_match:
        print(f"[域名发现] OCR 结果中未找到域名：{recognized_text!r}")
        return None

    return host_match.group(0).lower()


def read_cached_host() -> str | None:
    try:
        with open(DOMAIN_CACHE_PATH, encoding="utf-8") as cache_file:
            cached_host = cache_file.read().strip()
    except OSError:
        return None
    return cached_host or None


def write_cached_host(host: str) -> None:
    try:
        with open(DOMAIN_CACHE_PATH, "w", encoding="utf-8") as cache_file:
            cache_file.write(host)
    except OSError as error:
        print(f"[域名发现] 写入域名缓存失败：{error}")


def validate_host(host: str, timeout_seconds: float) -> str | None:
    """验证站点登录页可用，并返回重定向后的实际域名。"""
    login_page_url = (
        f"https://{host}/member.php?mod=logging&action=login"
        "&infloat=yes&frommessage&inajax=1&ajaxtarget=messagelogin"
    )
    try:
        response = requests.get(
            login_page_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(
            f"[域名发现] 候选域名 {host} 访问失败："
            f"{describe_request_error(error)}",
        )
        return None

    login_page_is_valid = bool(
        FORM_HASH_PATTERN.search(response.text)
        and LOGIN_HASH_PATTERN.search(response.text)
    )
    if not login_page_is_valid:
        print(f"[域名发现] 候选域名 {host} 未返回有效登录页面")
        return None

    redirected_host = urlparse(response.url).hostname
    return redirected_host.lower() if redirected_host else host


def resolve_host(
    timeout_seconds: float,
) -> tuple[str, str | None]:
    configured_host = os.getenv("SXSY_HOST", "").strip()
    if configured_host:
        return configured_host, None

    cached_host = read_cached_host()
    if cached_host:
        validated_host = validate_host(cached_host, timeout_seconds)
        if validated_host:
            if validated_host != cached_host:
                write_cached_host(validated_host)
                print(
                    "[域名发现] 缓存域名已重定向，更新为："
                    f"{validated_host}",
                )
            else:
                print(f"[域名发现] 缓存域名可用：{cached_host}")
            return validated_host, None
        print("[域名发现] 缓存域名不可用，重新从发布页识别")

    if not os.getenv("OCR_KEY", "").strip():
        error_message = "缓存域名不可用且未配置 OCR_KEY，无法自动发现最新域名"
        print(f"[域名发现] {error_message}，使用默认域名：{DEFAULT_HOST}")
        return DEFAULT_HOST, error_message

    discovered_host = discover_host_via_ocr(timeout_seconds)
    if discovered_host:
        validated_host = validate_host(discovered_host, timeout_seconds)
        if validated_host:
            write_cached_host(validated_host)
            print(f"[域名发现] OCR 识别到最新域名：{validated_host}")
            return validated_host, None
        print(f"[域名发现] OCR 识别域名不可用：{discovered_host}")

    print(f"[域名发现] OCR 识别失败且无缓存，使用默认域名：{DEFAULT_HOST}")
    return DEFAULT_HOST, None


class SxsyCheckinClient:
    """Cookie 优先、账密 OCR 登录回退的尚香书苑签到客户端。"""

    def __init__(
        self,
        configuration: AccountConfiguration,
        host: str,
        timeout_seconds: float,
    ) -> None:
        self.configuration = configuration
        self.host = host
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

    def _storage_key(self) -> str:
        """本地 Cookie 存储用的稳定 key。

        有登录标识的账号用标识本身（邮箱或用户名）；纯 Cookie 账号用
        Cookie 内容的 SHA-256 前 12 位，避免多个纯 Cookie 账号共用
        "Cookie 账号" 互相覆盖。
        """
        if self.configuration.identifier:
            return self.configuration.identifier
        cookie_digest = hashlib.sha256(
            self.configuration.cookie.encode("utf-8"),
        ).hexdigest()[:12]
        return f"cookie:{cookie_digest}"

    def checkin(self, account_number: int) -> CheckinResult:
        account_label = self.configuration.label
        try:
            authenticated, _, auth_message = self._authenticate()
            if not authenticated:
                return self._failure_result(
                    account_number,
                    account_label,
                    f"登录失败：{auth_message}",
                )

            sign_page_text = self._fetch_sign_page()
            if is_confirmed_signed_page(sign_page_text):
                details: dict[str, str] = {}
                money = self._fetch_money()
                if money:
                    details["金钱"] = money
                return CheckinResult(
                    account_number=account_number,
                    account_label=account_label,
                    success=True,
                    status="今日已签到",
                    message="签到页面确认今日已签到",
                    details=details,
                )

            sign_hash_match = SIGN_HASH_PATTERN.search(sign_page_text)
            if not sign_hash_match:
                return self._failure_result(
                    account_number,
                    account_label,
                    "未找到签到 formhash，页面结构可能已变化",
                )

            sign_response_text = self._submit_checkin(sign_hash_match.group(1))
            sign_message = extract_cdata_message(sign_response_text)
            success, status = classify_sign_message(sign_message)
            if not success:
                confirmation_page_text = self._fetch_sign_page()
                if is_confirmed_signed_page(confirmation_page_text):
                    success = True
                    status = "签到成功"
                    sign_message = sign_message or "签到成功，页面状态已确认"

            details: dict[str, str] = {}
            money = self._fetch_money()
            if money:
                details["金钱"] = money

            return CheckinResult(
                account_number=account_number,
                account_label=account_label,
                success=success,
                status=status,
                message=sign_message or "签到接口无返回内容",
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
                "无法连接站点",
            )
        except requests.RequestException as error:
            return self._failure_result(
                account_number,
                account_label,
                f"网络请求失败：{describe_request_error(error)}",
            )
        except MathVerificationError as error:
            return self._failure_result(
                account_number,
                account_label,
                f"签到计算验证失败：{error}",
            )
        except Exception as error:
            return self._failure_result(
                account_number,
                account_label,
                f"未预期异常：{type(error).__name__}: {error}",
            )
        finally:
            self.session.close()

    def _authenticate(self) -> tuple[bool, str, str]:
        account_key = self._storage_key()
        stored_cookie = COOKIE_STORE.read(account_key)
        cookie_candidates = [
            ("本地存储", stored_cookie),
            ("环境变量", self.configuration.cookie),
        ]

        for cookie_source, cookie_value in cookie_candidates:
            if not cookie_value:
                continue
            load_cookie_header(self.session, cookie_value)
            if self._is_session_valid():
                if cookie_source == "本地存储":
                    print("[登录] 本地存储 Cookie 有效")
                refreshed_cookie = session_cookie_header(self.session)
                if refreshed_cookie:
                    COOKIE_STORE.write(account_key, refreshed_cookie)
                return True, "Cookie", ""
            print(f"[登录] {cookie_source} Cookie 已失效")
            if cookie_source == "本地存储":
                COOKIE_STORE.remove(account_key)
            self.session.cookies.clear()

        if not self.configuration.identifier or not self.configuration.password:
            return False, "Cookie", "Cookie 已失效且未配置账号密码"

        self.session.cookies.clear()
        login_success, login_method, login_message = self._login_with_password()
        if login_success:
            session_cookie = session_cookie_header(self.session)
            if session_cookie:
                COOKIE_STORE.write(account_key, session_cookie)
                print("[Cookie存储] 已保存最新 Cookie")
        return login_success, login_method, login_message

    def _is_session_valid(self) -> bool:
        response = self.session.get(
            f"https://{self.host}/home.php?mod=space",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return "请先登录" not in response.text

    def _login_with_password(self) -> tuple[bool, str, str]:
        login_form_response = self.session.get(
            f"https://{self.host}/member.php?mod=logging&action=login"
            "&infloat=yes&frommessage&inajax=1&ajaxtarget=messagelogin",
            timeout=self.timeout_seconds,
        )
        login_form_response.raise_for_status()
        login_page_text = login_form_response.text

        form_hash_match = FORM_HASH_PATTERN.search(login_page_text)
        seccode_hash_match = SECCODE_HASH_PATTERN.search(login_page_text)
        login_hash_match = LOGIN_HASH_PATTERN.search(login_page_text)
        if not form_hash_match or not login_hash_match:
            return False, "账号密码", "登录页面结构已变化，缺少 formhash/loginhash"

        form_hash = form_hash_match.group(1)
        login_hash = login_hash_match.group(1)
        seccode_hash = (
            seccode_hash_match.group(1) if seccode_hash_match else ""
        )

        captcha_text = ""
        if seccode_hash:
            captcha_text = self._solve_captcha(seccode_hash)
            if not captcha_text:
                return False, "账号密码", "验证码识别失败，已达最大重试次数"

        login_url = (
            f"https://{self.host}/member.php?mod=logging&action=login"
            f"&loginsubmit=yes&loginhash={login_hash}&inajax=1"
        )
        login_payload = {
            "formhash": form_hash,
            "referer": f"https://{self.host}/",
            "loginfield": self.configuration.login_field,
            "username": self.configuration.identifier,
            "password": self.configuration.password,
            "questionid": "0",
            "answer": "",
            "cookietime": "2592000",
        }
        if seccode_hash:
            login_payload.update(
                {
                    "seccodehash": seccode_hash,
                    "seccodemodid": "member::logging",
                    "seccodeverify": captcha_text,
                }
            )

        login_response = self.session.post(
            login_url,
            data=quote(
                "&".join(
                    f"{key}={value}" for key, value in login_payload.items()
                ),
                safe="=&",
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"https://{self.host}/member.php?mod=logging&action=login",
            },
            timeout=self.timeout_seconds,
        )
        login_response.raise_for_status()

        login_message = extract_cdata_message(login_response.text)
        welcome_match = WELCOME_PATTERN.search(login_message)
        if welcome_match:
            print(f"[登录] 登录成功：{welcome_match.group(1)}")
            return True, "账号密码", ""

        if "验证码" in login_message:
            return False, "账号密码", f"登录失败：{login_message}"
        return False, "账号密码", f"登录失败：{login_message or '未知原因'}"

    def _solve_captcha(self, seccode_hash: str) -> str:
        if not _ocr_has_key():
            return ""
        for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            captcha_response = self.session.get(
                f"https://{self.host}/misc.php?mod=seccode"
                f"&update={random.randint(10000, 99999)}&idhash={seccode_hash}",
                headers={
                    "Referer": (
                        f"https://{self.host}/member.php"
                        "?mod=logging&action=login"
                    ),
                },
                timeout=self.timeout_seconds,
            )
            captcha_response.raise_for_status()
            captcha_text = _ocr_recognize_captcha(
                captcha_response.content,
                self.timeout_seconds,
            )
            if not captcha_text:
                continue
            captcha_text = re.sub(r"[^a-zA-Z0-9]", "", captcha_text)
            if not captcha_text:
                continue

            check_response = self.session.get(
                f"https://{self.host}/misc.php?mod=seccode&action=check"
                f"&inajax=1&modid=member::logging&idhash={seccode_hash}"
                f"&secverify={captcha_text}",
                timeout=self.timeout_seconds,
            )
            check_response.raise_for_status()
            if "succeed" in extract_cdata_message(check_response.text):
                return captcha_text

            print(f"[登录] 验证码校验失败（第 {attempt} 次）")
            time.sleep(2)
        return ""

    def _fetch_sign_page(self) -> str:
        response = self.session.get(
            f"https://{self.host}/plugin.php?id=k_misign:sign",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def _submit_checkin(self, sign_hash: str) -> str:
        sign_url = f"https://{self.host}/plugin.php"
        sign_parameters = {
            "id": "k_misign:sign",
            "operation": "qiandao",
            "format": "global_usernav_extra",
            "formhash": sign_hash,
            "inajax": "1",
            "ajaxtarget": "k_misign_topb",
        }
        response = self.session.get(
            sign_url,
            params=sign_parameters,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        verification_expression = parse_math_verification(response.text)
        if verification_expression is None:
            if "mathverify_answer" in response.text or "签到验证" in response.text:
                raise MathVerificationError("签到计算验证题格式无法识别")
            return response.text

        left_operand, math_operator, right_operand = verification_expression
        verification_answer = calculate_math_verification_answer(
            left_operand,
            math_operator,
            right_operand,
        )
        print(
            "[签到] 检测到计算验证："
            f"{left_operand} {math_operator} {right_operand} = "
            f"{verification_answer}",
        )

        # 站点首次签到只下发算术题，答案需通过同一签到接口再次提交。
        verification_parameters = {
            "id": "k_misign:sign",
            "operation": "qiandao",
            "formhash": sign_hash,
            "format": "global_usernav_extra",
            "mathverify_answer": verification_answer,
        }
        response = self.session.get(
            sign_url,
            params=verification_parameters,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def _fetch_money(self) -> str:
        try:
            response = self.session.get(
                f"https://{self.host}/home.php?mod=spacecp&ac=credit&showcredit=1",
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException:
            return ""
        money_match = MONEY_PATTERN.search(response.text)
        return money_match.group(1) if money_match else ""

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


def session_cookie_header(session: requests.Session) -> str:
    return "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in session.cookies
    )


def load_cookie_header(session: requests.Session, cookie_header: str) -> None:
    for cookie_item in cookie_header.split(";"):
        if "=" not in cookie_item:
            continue
        cookie_name, cookie_value = cookie_item.split("=", 1)
        session.cookies.set(cookie_name.strip(), cookie_value.strip())


def extract_cdata_message(response_text: str) -> str:
    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", response_text, re.DOTALL)
    message_source = cdata_match.group(1) if cdata_match else response_text
    message_source = re.sub(r"<[^>]+>", " ", message_source)
    return " ".join(html.unescape(message_source).split())


def parse_math_verification(response_text: str) -> tuple[int, str, int] | None:
    decoded_response_text = html.unescape(response_text)
    verification_match = MATH_VERIFICATION_PATTERN.search(decoded_response_text)
    if verification_match is None:
        return None

    return (
        int(verification_match.group(1)),
        verification_match.group(2),
        int(verification_match.group(3)),
    )


def calculate_math_verification_answer(
    left_operand: int,
    math_operator: str,
    right_operand: int,
) -> str:
    if math_operator == "+":
        return str(left_operand + right_operand)
    if math_operator == "-":
        return str(left_operand - right_operand)
    if math_operator in {"*", "×", "x", "X"}:
        return str(left_operand * right_operand)
    if math_operator in {"/", "÷"}:
        if right_operand == 0:
            raise MathVerificationError("签到计算验证题存在除零运算")
        quotient, remainder = divmod(left_operand, right_operand)
        if remainder != 0:
            raise MathVerificationError("签到计算验证题的除法结果不是整数")
        return str(quotient)

    raise MathVerificationError(f"签到计算验证题包含不支持的运算符：{math_operator}")


def classify_sign_message(sign_message: str) -> tuple[bool, str]:
    if not sign_message:
        return False, "失败"
    if contains_any(sign_message, ("已签到", "已经签到", "今日已签")):
        return True, "今日已签到"
    if contains_any(sign_message, ("签到成功", "恭喜")):
        return True, "签到成功"
    return False, "失败"


def is_confirmed_signed_page(sign_page_text: str) -> bool:
    """通过签到页面结构确认今日是否已完成签到。

    k_misign 的已签到页面仍包含 formhash，但会移除 qiandao 操作入口；
    lxdays 是签到页自身的连续天数元素，用于避免把异常页面误判为成功。
    """
    has_sign_page_marker = bool(
        re.search(r'id=["\']lxdays["\']', sign_page_text, re.IGNORECASE),
    )
    has_checkin_action = "operation=qiandao" in html.unescape(sign_page_text)
    return has_sign_page_marker and not has_checkin_action


def contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def mask_identifier(identifier: str) -> str:
    if not identifier:
        return "未知账号"
    local_part, _, domain_part = identifier.partition("@")
    if len(local_part) <= 1:
        masked_local = "*"
    elif len(local_part) == 2:
        masked_local = f"{local_part[0]}*"
    else:
        masked_local = f"{local_part[0]}***{local_part[-1]}"
    return f"{masked_local}@{domain_part}" if domain_part else masked_local


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
    host: str,
) -> tuple[str, str, str]:
    successful_count = sum(result.success for result in results)
    failed_count = len(results) - successful_count
    status_icon = "✅" if failed_count == 0 and successful_count > 0 else "⚠️"
    title = f"尚香书苑签到 {status_icon} {successful_count}/{len(results)}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_sections = [
        "<b>尚香书苑每日签到</b>",
        "\n".join(
            [
                "<b>执行概览</b>",
                f"• 成功：{html_code(successful_count)}",
                f"• 失败：{html_code(failed_count)}",
                f"• 站点：{html_code(host)}",
                f"• 时间：{html_code(execution_time)}",
            ]
        ),
    ]
    plain_sections = [
        "尚香书苑每日签到",
        "\n".join(
            [
                "执行概览",
                f"• 成功：{successful_count}",
                f"• 失败：{failed_count}",
                f"• 站点：{host}",
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
            html_account_lines.append(
                f"• {escape_html_text(detail_key)}：{html_code(detail_value)}",
            )
            plain_account_lines.append(f"• {detail_key}：{detail_value}")
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
    print(f"==== 尚香书苑签到开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    raw_accounts = os.getenv("SXSY_ACCOUNTS", "")
    configurations, configuration_errors = parse_account_configurations(raw_accounts)
    if not configurations and not configuration_errors:
        configuration_errors.append("未配置 SXSY_ACCOUNTS 环境变量")

    password_login_needs_ocr = any(
        configuration.password for configuration in configurations
    )
    if password_login_needs_ocr and not os.getenv("OCR_KEY", "").strip():
        configuration_errors.append(
            "账密登录需要配置 OCR_KEY",
        )

    for configuration_error in configuration_errors:
        print(f"[配置错误] {configuration_error}")

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        default_account_delay_seconds=DEFAULT_ACCOUNT_DELAY_SECONDS,
    )
    timeout_seconds = runtime_settings.request_timeout_seconds
    privacy_mode = read_boolean_environment("SXSY_PRIVACY_MODE", True)

    apply_startup_random_delay(
        "尚香书苑签到",
        runtime_settings,
        has_work=bool(configurations),
    )

    host, host_resolution_error = resolve_host(timeout_seconds)
    if host_resolution_error:
        configuration_errors.append(host_resolution_error)
        print(f"[配置错误] {host_resolution_error}")

    results: list[CheckinResult] = []
    for account_index, configuration in enumerate(configurations, start=1):
        display_label = (
            mask_identifier(configuration.label)
            if privacy_mode
            else configuration.label
        )
        print(f"\n---- 账号 {account_index}（{display_label}）开始 ----")
        client = SxsyCheckinClient(configuration, host, timeout_seconds)
        result = client.checkin(account_index)
        if privacy_mode:
            result.account_label = display_label
        results.append(result)
        print_result(result)

        wait_between_accounts(
            account_index,
            len(configurations),
            runtime_settings.account_delay_seconds,
        )

    notification_title, html_content, plain_content = build_notification_content(
        results,
        configuration_errors,
        host,
    )
    print("\n==== 尚香书苑签到汇总 ====")
    print(plain_content)

    send_notifications(
        notification_title,
        html_content,
        plain_content,
        timeout_seconds,
    )

    all_succeeded = bool(results) and all(result.success for result in results)
    configuration_valid = not configuration_errors
    return 0 if all_succeeded and configuration_valid else 1


if __name__ == "__main__":
    sys.exit(main())
