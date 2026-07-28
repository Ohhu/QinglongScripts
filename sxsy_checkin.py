#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 10 9 * * *
new Env('尚香书苑签到')

环境变量：
  SXSY_ACCOUNTS        必填。每行一个账号，格式如下：
                         邮箱|密码          账密登录（需要 OCR）
                         邮箱|密码|Cookie   Cookie 优先，失效回退账密
                         Cookie 优先级最高：整行不含 | 时视为纯 Cookie。
  OCR_KEY              账密登录或自动域名发现时必填。EasyOCR 云端
                       访问密钥（console.easyocr.org 创建，eocr_ 开头），
                       所有需要 OCR 的任务共用。
  SXSY_HOST            可选。手动指定站点域名（如 sxsy13.com），
                       设置后跳过发布页域名发现。
  SXSY_PRIVACY_MODE    日志和通知中是否对账号名脱敏，默认为 true。
  TG_NOTIFY_CONFIG     可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                       配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN   是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX 随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT         单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY   多账号之间的等待秒数，默认为 3（所有任务共用）。

站点发布页 https://sxsy.org/ 把最新域名放在 site.jpg 图片里，
脚本下载该图片后用 EasyOCR 云端识别域名；识别失败回退上次成功的
域名缓存，再失败使用内置默认域名。

Cookie 持久化：账密登录成功后把最新 Cookie 存入本地文件
（/ql/data/scripts_data/sxsy_cookies.json），之后运行优先用本地存储的
Cookie，失效后自动清理并依次回退环境变量 Cookie、账密重新登录。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import builtins
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

import requests


EASYOCR_API_URL = "https://console.easyocr.org/api/ocr"


RELEASE_PAGE_URL = "https://sxsy.org/"
DOMAIN_IMAGE_PATH = "site.jpg"
DEFAULT_HOST = "sxsy13.com"

# 本地数据统一放在 /ql/data/scripts_data/（青龙持久卷），
# 非青龙环境降级到脚本旁 ./scripts_data/，目录不存在时自动创建。
QINGLONG_DATA_DIR = "/ql/data/scripts_data"
if not os.path.isdir(QINGLONG_DATA_DIR):
    QINGLONG_DATA_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scripts_data",
    )
os.makedirs(QINGLONG_DATA_DIR, exist_ok=True)

DOMAIN_CACHE_PATH = os.path.join(QINGLONG_DATA_DIR, "sxsy_last_host")
COOKIE_STORE_PATH = os.path.join(QINGLONG_DATA_DIR, "sxsy_cookies.json")

HOST_PATTERN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.IGNORECASE)
FORM_HASH_PATTERN = re.compile(r'name="formhash"\s+value="([a-zA-Z0-9]{8})"')
SECCODE_HASH_PATTERN = re.compile(r"seccode_([a-zA-Z0-9]{6})")
LOGIN_HASH_PATTERN = re.compile(r"loginhash=([a-zA-Z0-9]{5})")
SIGN_HASH_PATTERN = re.compile(r"formhash=([a-zA-Z0-9]{8})")
WELCOME_PATTERN = re.compile(r"欢迎您回来，(.*?)，")
MONEY_PATTERN = re.compile(r"金钱:\s*</em>(\d+)")

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
    email: str = ""
    password: str = ""
    cookie: str = ""

    @property
    def label(self) -> str:
        if self.email:
            return self.email
        return "Cookie 账号"


@dataclass
class CheckinResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


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
                AccountConfiguration(email=parts[0], password=parts[1]),
            )
            continue
        if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
            configurations.append(
                AccountConfiguration(
                    email=parts[0],
                    password=parts[1],
                    cookie=parts[2],
                ),
            )
            continue
        configuration_errors.append(
            f"第 {line_number} 行账号格式无效，应为 邮箱|密码 或 邮箱|密码|Cookie",
        )

    return configurations, configuration_errors


def discover_host_via_ocr(
    ocr_key: str,
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

    recognized_text = recognize_image_with_ocr(
        ocr_key,
        image_response.content,
        "site.jpg",
        timeout_seconds,
    )
    if not recognized_text:
        return None

    host_match = HOST_PATTERN.search(recognized_text.replace(" ", ""))
    if not host_match:
        print(f"[域名发现] OCR 结果中未找到域名：{recognized_text!r}")
        return None

    return host_match.group(0).lower()


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
        print(f"[OCR] 识别失败：HTTP {response.status_code} {response_data}")
        return None

    words = response_data.get("words") or []
    recognized_text = " ".join(
        str(word.get("text", "")) for word in words
    ).strip()
    if not recognized_text:
        print(f"[OCR] 未识别到文字：{response_data.get('result_summary')}")
        return None

    remaining_quota = response_data.get("remaining_quota")
    if remaining_quota is not None:
        print(f"[OCR] 识别成功：{recognized_text!r}（剩余额度 {remaining_quota}）")
    return recognized_text


def read_stored_cookie(account_key: str) -> str:
    try:
        with open(COOKIE_STORE_PATH, encoding="utf-8") as store_file:
            store_data = json.load(store_file)
    except (OSError, ValueError):
        return ""

    account_entry = store_data.get(account_key)
    if not isinstance(account_entry, dict):
        return ""
    return str(account_entry.get("cookie", "")).strip()


def write_stored_cookie(account_key: str, cookie: str) -> None:
    store_data: dict[str, Any] = {}
    try:
        with open(COOKIE_STORE_PATH, encoding="utf-8") as store_file:
            existing_data = json.load(store_file)
            if isinstance(existing_data, dict):
                store_data = existing_data
    except (OSError, ValueError):
        pass

    store_data[account_key] = {
        "cookie": cookie,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(COOKIE_STORE_PATH, "w", encoding="utf-8") as store_file:
            json.dump(store_data, store_file, ensure_ascii=False, indent=2)
    except OSError as error:
        print(f"[Cookie存储] 写入失败：{error}")


def remove_stored_cookie(account_key: str) -> None:
    try:
        with open(COOKIE_STORE_PATH, encoding="utf-8") as store_file:
            store_data = json.load(store_file)
    except (OSError, ValueError):
        return

    if account_key not in store_data:
        return
    store_data.pop(account_key)
    try:
        with open(COOKIE_STORE_PATH, "w", encoding="utf-8") as store_file:
            json.dump(store_data, store_file, ensure_ascii=False, indent=2)
    except OSError as error:
        print(f"[Cookie存储] 清理失败：{error}")


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


def resolve_host(
    ocr_key: str,
    timeout_seconds: float,
) -> tuple[str, str | None]:
    configured_host = os.getenv("SXSY_HOST", "").strip()
    if configured_host:
        return configured_host, None

    discovered_host = discover_host_via_ocr(ocr_key, timeout_seconds)
    if discovered_host:
        write_cached_host(discovered_host)
        print(f"[域名发现] OCR 识别到最新域名：{discovered_host}")
        return discovered_host, None

    cached_host = read_cached_host()
    if cached_host:
        print(f"[域名发现] OCR 识别失败，回退缓存域名：{cached_host}")
        return cached_host, None

    print(f"[域名发现] OCR 识别失败且无缓存，使用默认域名：{DEFAULT_HOST}")
    return DEFAULT_HOST, None


class SxsyCheckinClient:
    """Cookie 优先、账密 OCR 登录回退的尚香书苑签到客户端。"""

    def __init__(
        self,
        configuration: AccountConfiguration,
        host: str,
        ocr_key: str,
        timeout_seconds: float,
    ) -> None:
        self.configuration = configuration
        self.host = host
        self.ocr_key = ocr_key
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

        有邮箱的账号用邮箱；纯 Cookie 账号用 Cookie 内容的 SHA-256 前 12 位，
        避免多个纯 Cookie 账号共用 "Cookie 账号" 互相覆盖。
        """
        if self.configuration.email:
            return self.configuration.email
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
        stored_cookie = read_stored_cookie(account_key)
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
                return True, "Cookie", ""
            print(f"[登录] {cookie_source} Cookie 已失效")
            if cookie_source == "本地存储":
                remove_stored_cookie(account_key)
            self.session.cookies.clear()

        if not self.configuration.email or not self.configuration.password:
            return False, "Cookie", "Cookie 已失效且未配置账号密码"
        if not self.ocr_key:
            return False, "账号密码", "账密登录需要配置 OCR_KEY"

        self.session.cookies.clear()
        login_success, login_method, login_message = self._login_with_password()
        if login_success:
            session_cookie = session_cookie_header(self.session)
            if session_cookie:
                write_stored_cookie(account_key, session_cookie)
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
            "loginfield": "email",
            "username": self.configuration.email,
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
            captcha_text = recognize_image_with_ocr(
                self.ocr_key,
                captcha_response.content,
                "captcha.jpg",
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
        response = self.session.get(
            f"https://{self.host}/plugin.php?id=k_misign:sign"
            "&operation=qiandao&format=global_usernav_extra"
            f"&formhash={sign_hash}&inajax=1&ajaxtarget=k_misign_topb",
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


def classify_sign_message(sign_message: str) -> tuple[bool, str]:
    if not sign_message:
        return False, "失败"
    if contains_any(sign_message, ("已签到", "已经签到", "今日已签")):
        return True, "今日已签到"
    if contains_any(sign_message, ("签到成功", "恭喜")):
        return True, "签到成功"
    return False, "失败"


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
    remaining_seconds = delay_seconds
    while remaining_seconds > 0:
        print(
            f"{task_name} 倒计时："
            f"{format_time_remaining(remaining_seconds)}",
        )
        sleep_seconds = 1 if remaining_seconds <= 10 else min(10, remaining_seconds)
        time.sleep(sleep_seconds)
        remaining_seconds -= sleep_seconds


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

    ocr_key = os.getenv("OCR_KEY", "").strip()
    needs_ocr = any(
        configuration.password for configuration in configurations
    ) or not os.getenv("SXSY_HOST", "").strip()
    if needs_ocr and not ocr_key:
        configuration_errors.append(
            "账密登录或自动域名发现需要配置 OCR_KEY",
        )

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
    privacy_mode = read_boolean_environment("SXSY_PRIVACY_MODE", True)

    random_signin_enabled = read_boolean_environment("TASK_RANDOM_SIGNIN", True)
    if random_signin_enabled:
        max_random_delay = read_positive_float_environment(
            "TASK_RANDOM_DELAY_MAX",
            3600.0,
        )
        if max_random_delay > 0:
            delay_seconds = random.uniform(0, max_random_delay)
            print(f"🎲 随机延迟：{format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "尚香书苑签到")

    host, _ = resolve_host(ocr_key, timeout_seconds)

    results: list[CheckinResult] = []
    for account_index, configuration in enumerate(configurations, start=1):
        display_label = (
            mask_identifier(configuration.label)
            if privacy_mode
            else configuration.label
        )
        print(f"\n---- 账号 {account_index}（{display_label}）开始 ----")
        client = SxsyCheckinClient(configuration, host, ocr_key, timeout_seconds)
        result = client.checkin(account_index)
        if privacy_mode:
            result.account_label = display_label
        results.append(result)
        print_result(result)

        has_next_account = account_index < len(configurations)
        if has_next_account and account_delay_seconds > 0:
            print(f"等待 {account_delay_seconds:g} 秒后处理下一个账号")
            time.sleep(account_delay_seconds)

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
