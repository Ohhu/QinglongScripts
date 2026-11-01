#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 30 8 * * *
new Env('百度网盘任务系统签到')

环境变量：
  BAIDU_COOKIE           必填。pan.baidu.com 完整 Cookie，多账号使用换行分隔；
                         与现有百度网盘脚本共用同一个变量即可。
  BAIDU_TASK_QUIZ_ANSWER 可选。每日答题强制答案：0=否，1=是；不配置时优先使用
                         接口返回的 answer，取不到则随机选择。
  BAIDU_TASK_ACTIVITY_URL 可选。任务系统页面地址，默认
                         https://pan.baidu.com/operation/activitys/taskSystem/main
  PRIVACY_MODE           日志和通知中是否对账号脱敏，默认为 true。
  TG_NOTIFY_CONFIG       可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                         配置后使用 HTML 直发，失败回退青龙纯文本通知。
  TASK_RANDOM_SIGNIN     是否启用启动前随机延迟，默认为 true（所有任务共用）。
  TASK_RANDOM_DELAY_MAX  随机延迟的最大秒数，默认为 3600（所有任务共用）。
  TASK_TIMEOUT           单次请求超时秒数，默认为 20（所有任务共用）。
  TASK_ACCOUNT_DELAY     多账号之间的等待秒数，默认为 3（所有任务共用）。

脚本对应 https://pan.baidu.com/operation/activitys/taskSystem/main 页面：
  1. 调用 /coins/taskcenter/home 检查今日签到状态，未签到时使用
     /coins/taskcenter/signin 提交签到；签到请求需要「今日签到领奖」任务的
     task_id/task_from，并携带移动端设备参数 cuid/devuid/idfv；
  2. 调用 /coins/taskcenter/tasklist 拉取日常与成长任务，领取
     task_status=6 的任务奖励；金币接口不可用时回退旧版
     /api/taskscore/tasklist（task_status=1）；
  3. 调用 /act/v2/membergrowv2/getdailyquestion 获取判断题并提交答案，
     答对后再次拉取任务列表，把刚解锁的答题任务奖励领掉。

请求需要复刻网页端的设备参数与 jt/token 签名。device 参数由 Cookie 中的
BAIDUID 派生，保证同一账号每次运行使用同一设备标识。jt 签名会带上活动页
地址；若日志出现 errno=8001（HTJ 反作弊），可把 BAIDU_TASK_ACTIVITY_URL
设置为浏览器中实际打开的完整页面地址后重试。

通知优先使用 Telegram HTML 直发；失败或未配置时回退青龙纯文本通知。
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import html
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)


BASE_URL = "https://pan.baidu.com"
DEFAULT_ACTIVITY_URL = "https://pan.baidu.com/operation/activitys/taskSystem/main"
AID = "13655"
TOKEN_SALT = "ae82c240578eb391de93c2f4c3dfc3ba"

TASK_FROM = [
    "task_sys_beginner",
    "task_sys_daily",
    "task_sys_space",
    "task_sys_badge",
    "task_sys_ai",
    "task_sys_function",
    "task_sys_growth",
]

API_TASK_LIST = "/api/taskscore/tasklist"
API_COIN_TASK_LIST = "/coins/taskcenter/tasklist"
API_TASK_SAVE = "/api/taskscore/tasksave"
API_TASK_ANTI_SAVE = "/api/taskscore/antisave"
API_LOGIN_STATUS = "/api/loginstatus"
API_DAILY_QUESTION = "/act/v2/membergrowv2/getdailyquestion"
API_ANSWER_QUESTION = "/act/v2/membergrowv2/answerquestion"
API_COIN_HOME = "/coins/taskcenter/home"
API_COIN_SIGN = "/coins/taskcenter/signin"
API_COIN_SIGN_LIST = "/coins/taskcenter/signinlist"

COIN_TASK_FROM = [task_from for task_from in TASK_FROM if task_from != "task_sys_badge"]
GROWTH_TASK_FROM = ["task_sys_task_growth"]

EVENT_MAP = {
    API_TASK_ANTI_SAVE: "task",
}

ERRNO_MEANING = {
    "-6": "未登录",
    "0": "OK",
    "2": "参数错误",
    "40003": "已领取",
    "8001": "HTJ 反作弊",
    "8002": "jsToken 校验失败",
    "8011": "作弊频控",
    "8024": "作弊频控",
    "9312": "设备重复",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_SPACE_UNIT = {1: "GB", 2: "MB"}


@dataclass
class ClaimSummary:
    total: int = 0
    claimed: int = 0
    already: int = 0
    failed: int = 0
    messages: list[str] = field(default_factory=list)
    endpoint_ok: bool = True
    fallback: bool = False


@dataclass
class SignInResult:
    status: str
    message: str
    signed: bool = False
    already: bool = False
    endpoint_ok: bool = True
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class QuizResult:
    status: str
    message: str
    correct: bool = False
    should_claim: bool = False
    endpoint_ok: bool = True
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class AccountResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    claim_message: str
    quiz_message: str
    signin_message: str = ""
    warning: bool = False
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class DeviceIdentity:
    bid: str
    cuid: str
    z: str
    devuid: str
    idfv: str
    channel: str = "0"
    version: str = "13.36.4"
    app: str = "android"


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def encode_base64_ascii(value: str) -> str:
    """与网页端 b64s 一致；签名内容均为 ASCII，可直接使用标准 Base64。"""
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def obfuscate_text(value: str) -> str:
    """复刻网页端 obf：ASCII 可见字符按位置偏移。"""
    result: list[str] = []
    for index, character in enumerate(value):
        code = ord(character)
        offset = index % 32
        if 41 <= code <= 122:
            shifted = code + offset
            if shifted > 122:
                shifted = 40 + code - 122 + offset
            result.append(chr(shifted))
        else:
            result.append(character)
    return "".join(result)


def make_jt(
    device: DeviceIdentity,
    activity_url: str,
    payload: str,
    timestamp_ms: int | None = None,
) -> str:
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    raw_value = f"{device.bid}|{activity_url}|{timestamp}|{payload}"
    return "30$CODED--v30" + obfuscate_text(encode_base64_ascii(raw_value))


def random_numeric_string() -> str:
    """复刻网页端 rand/rand2 的 10~11 位数字。"""
    return str(
        int(
            (random.random() + int(random.random() * 9 + 1)) * 1_000_000_000
        )
    )


def parse_cookie(cookie: str) -> dict[str, str]:
    cookie_values: dict[str, str] = {}
    for raw_part in cookie.split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookie_values[name.strip()] = value.strip()
    return cookie_values


def build_device_identity(cookie: str, cookie_values: dict[str, str]) -> DeviceIdentity:
    raw_bid = cookie_values.get("BAIDUID", "").split(":", 1)[0].strip().lower()
    seed_source = raw_bid or cookie
    seed = md5_hex(f"baidu-pan-task|{seed_source}")

    def derive(label: str) -> str:
        return md5_hex(f"{seed}|{label}")

    bid = raw_bid or derive("bid")
    idfv_raw = derive("idfv")
    idfv = (
        f"{idfv_raw[0:8]}-{idfv_raw[8:12]}-{idfv_raw[12:16]}-"
        f"{idfv_raw[16:20]}-{idfv_raw[20:32]}"
    ).upper()
    return DeviceIdentity(
        bid=bid,
        cuid=bid or derive("cuid"),
        z=derive("z"),
        devuid=derive("devuid"),
        idfv=idfv,
    )


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mask_account(account_label: str) -> str:
    if not account_label:
        return "未知账号"
    if len(account_label) <= 4:
        return account_label
    return f"{account_label[:3]}***{account_label[-2:]}"


def describe_request_error(error: requests.RequestException) -> str:
    if error.response is not None:
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def errno_text(payload: dict[str, Any]) -> str:
    errno = str(payload.get("errno", ""))
    if not errno:
        return ""
    meaning = ERRNO_MEANING.get(errno, "")
    return f"errno={errno}" + (f" {meaning}" if meaning else "")


def pick_task_list(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    result = payload.get("result")
    data = payload.get("data")
    candidates: list[Any] = [
        result.get("list") if isinstance(result, dict) else result,
        data.get("list") if isinstance(data, dict) else data,
        payload.get("list"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return None


def format_reward(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        result = payload.get("task_result")
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    add_score = to_int(result.get("addScore"))
    add_growth = to_int(result.get("addGrowScore"))
    add_space = to_int(result.get("addSpace"))
    if add_score > 0:
        parts.append(f"+{add_score} 积分")
    if add_growth > 0:
        parts.append(f"+{add_growth} 成长值")
    if add_space > 0:
        space_unit = DEFAULT_SPACE_UNIT.get(
            to_int(result.get("space_unit"), 1),
            "GB",
        )
        parts.append(f"+{add_space}{space_unit} 空间")
    return "、".join(parts)


def parse_answer_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        mapping = {
            "0": 0,
            "1": 1,
            "否": 0,
            "是": 1,
            "false": 0,
            "true": 1,
            "no": 0,
            "yes": 1,
            "n": 0,
            "y": 1,
            "a": 0,
            "b": 1,
        }
        return mapping.get(normalized)
    return None


class BaiduPanTaskClient:
    """使用 pan.baidu.com Cookie 完成网盘任务系统领取与每日答题。"""

    def __init__(
        self,
        cookie: str,
        account_number: int,
        timeout_seconds: float,
        activity_url: str,
        quiz_answer_override: str = "",
        privacy_mode: bool = True,
    ) -> None:
        self.cookie = cookie.strip()
        self.account_number = account_number
        self.timeout_seconds = timeout_seconds
        self.activity_url = activity_url.strip()
        self.quiz_answer_override = quiz_answer_override
        self.privacy_mode = privacy_mode
        self.cookie_values = parse_cookie(self.cookie)
        self.device = build_device_identity(self.cookie, self.cookie_values)
        self.uk = ""
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": self.cookie,
                "Origin": BASE_URL,
                "Referer": self.activity_url,
                "User-Agent": DEFAULT_USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            }
        )

    def run(self) -> AccountResult:
        try:
            self._ensure_uk()
            account_label = self.uk
            if self.privacy_mode:
                account_label = mask_account(account_label)
            print(f"[账号] uk={account_label}")

            signin_result = self._sign_in()
            claim_summary = self._claim_tasks("签到后领取")
            quiz_result = self._answer_daily_question()

            if quiz_result.correct and quiz_result.should_claim:
                time.sleep(1.2)
                second_summary = self._claim_tasks("答题后领取")
                self._merge_claim_summary(claim_summary, second_summary)

            endpoints_ok = (
                signin_result.endpoint_ok
                and claim_summary.endpoint_ok
                and quiz_result.endpoint_ok
            )
            claims_effective = claim_summary.claimed + claim_summary.already
            claims_ok = claim_summary.failed == 0 or claims_effective > 0
            success = endpoints_ok and claims_ok and signin_result.signed
            warning = success and (
                not quiz_result.correct
                or claim_summary.failed > 0
                or claim_summary.fallback
            )
            if not success:
                status = "失败"
            elif warning:
                status = "完成（有提示）"
            else:
                status = "完成"
            details = {
                "签到状态": signin_result.status,
                "可领取任务": str(claim_summary.total),
                "领取成功": str(claim_summary.claimed),
                "已领取过": str(claim_summary.already),
                "领取失败": str(claim_summary.failed),
            }
            details.update(signin_result.details)
            details.update(quiz_result.details)
            return AccountResult(
                account_number=self.account_number,
                account_label=account_label,
                success=success,
                status=status,
                claim_message=self._format_claim_message(claim_summary),
                quiz_message=quiz_result.message,
                signin_message=signin_result.message,
                warning=warning,
                details=details,
            )
        except requests.Timeout:
            return self._failure_result("请求超时", "网络请求超过设定时间")
        except requests.ConnectionError:
            return self._failure_result("连接失败", "无法连接 pan.baidu.com")
        except requests.RequestException as error:
            return self._failure_result("网络错误", describe_request_error(error))
        except ValueError as error:
            return self._failure_result("执行失败", str(error))
        except Exception as error:
            return self._failure_result(
                "执行异常",
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.session.close()

    def _failure_result(self, status: str, message: str) -> AccountResult:
        print(f"[失败] {status}：{message}")
        return AccountResult(
            account_number=self.account_number,
            account_label="未知账号",
            success=False,
            status=status,
            claim_message=message,
            quiz_message="未执行",
            signin_message="未执行",
        )

    @staticmethod
    def _format_claim_message(summary: ClaimSummary) -> str:
        if not summary.endpoint_ok:
            return "任务列表获取失败"
        if summary.total == 0:
            return "暂无待领取任务"
        return (
            f"待领取 {summary.total} 个，成功 {summary.claimed} 个，"
            f"已领 {summary.already} 个，失败 {summary.failed} 个"
        )

    @staticmethod
    def _merge_claim_summary(target: ClaimSummary, source: ClaimSummary) -> None:
        target.total += source.total
        target.claimed += source.claimed
        target.already += source.already
        target.failed += source.failed
        target.messages.extend(source.messages)
        target.endpoint_ok = target.endpoint_ok and source.endpoint_ok
        target.fallback = target.fallback or source.fallback

    def _ensure_uk(self) -> str:
        if self.uk:
            return self.uk

        payload = self._api(API_LOGIN_STATUS, {"clienttype": "1"}, "登录状态")
        if to_int(payload.get("errno")) != 0:
            raise ValueError(
                f"登录状态获取失败（{errno_text(payload) or '无 errno'}），"
                "Cookie 可能已失效",
            )

        login_info = payload.get("login_info")
        uk = ""
        if isinstance(login_info, dict):
            uk = str(login_info.get("uk", "") or "")
        if not uk:
            uk = str(payload.get("uk", "") or "")
        if not uk:
            raise ValueError("登录状态未返回 uk，Cookie 可能不完整")
        self.uk = uk
        return self.uk

    def _sign(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            key: str(value) for key, value in (params or {}).items()
        }
        query.setdefault("clienttype", "1")
        if self.uk:
            query.setdefault("uk", self.uk)
        query.setdefault("aid", AID)

        event = EVENT_MAP.get(path)
        if event:
            query.setdefault("ev", event)
        query.setdefault("jt", make_jt(self.device, self.activity_url, event or "task"))
        query.setdefault("rand", random_numeric_string())
        query.setdefault("rand2", random_numeric_string())
        query.setdefault("time", str(int(time.time() * 1000)))
        query.setdefault("channel", self.device.channel)
        query.setdefault("version", self.device.version)
        query.setdefault("devuid", self.device.devuid)
        query.setdefault("c", self.device.cuid)
        query.setdefault("z", self.device.z)
        query.setdefault("app", self.device.app)
        # /coins/* 接口会校验移动端设备特征；只补在这类请求上，
        # 避免影响 taskscore 的签名与反作弊校验。
        if path.startswith("/coins/"):
            query.setdefault("cuid", self.device.cuid)
            query.setdefault("idfv", self.device.idfv)
        # 实测 antisave 携带 hjs=1 会直接返回 8001，需省略；
        # tasksave / tasklist 等接口保持 hjs=1。
        if path != API_TASK_ANTI_SAVE:
            query.setdefault("hjs", "1")

        if path in (API_TASK_SAVE, API_TASK_ANTI_SAVE):
            task_ids = str(query.get("task_ids") or query.get("task_id") or "")
            if task_ids and self.uk:
                query["token"] = md5_hex(
                    f"{task_ids}_{self.uk}_{query['rand']}_{query['time']}_{TOKEN_SALT}"
                )
        return query

    def _api(
        self,
        path: str,
        params: dict[str, Any] | None,
        purpose: str,
    ) -> dict[str, Any]:
        query = self._sign(path, params)
        response = self.session.get(
            f"{BASE_URL}{path}",
            params=query,
            timeout=self.timeout_seconds,
        )
        try:
            payload = response.json()
        except ValueError as error:
            if response.status_code == 405:
                raise ValueError(
                    f"{purpose}接口返回 HTTP 405，Cookie/BAIDUID 可能无效",
                ) from error
            raise ValueError(
                f"{purpose}接口返回非 JSON（HTTP {response.status_code}）",
            ) from error
        if not isinstance(payload, dict):
            raise ValueError(f"{purpose}接口返回结构异常")

        summary = errno_text(payload)
        print(
            f"  [接口] {purpose} HTTP {response.status_code}"
            + (f" {summary}" if summary else "")
        )
        return payload

    def _fetch_tasks_from(
        self,
        path: str,
        task_from: list[str],
        purpose: str,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        try:
            payload = self._api(
                path,
                {"task_from": " ".join(task_from)},
                purpose,
            )
        except (requests.RequestException, ValueError) as error:
            print(f"[任务] {purpose}获取失败：{error}")
            return None, False

        if to_int(payload.get("errno")) != 0:
            print(f"[任务] {purpose}获取失败（{errno_text(payload)}）")
            return None, False

        tasks = pick_task_list(payload)
        if tasks is None:
            print(f"[任务] {purpose}结构异常")
            return None, False
        return tasks, True

    def _fetch_coin_tasks(self) -> tuple[list[dict[str, Any]], bool]:
        all_tasks: list[dict[str, Any]] = []
        any_ok = False
        seen: set[tuple[str, str]] = set()
        for task_from, purpose in (
            (COIN_TASK_FROM, "金币任务列表"),
            (GROWTH_TASK_FROM, "成长任务列表"),
        ):
            tasks, ok = self._fetch_tasks_from(
                API_COIN_TASK_LIST,
                task_from,
                purpose,
            )
            if not ok:
                continue
            any_ok = True
            for task in tasks:
                key = (
                    str(task.get("task_id_str") or task.get("task_id") or ""),
                    str(task.get("task_from") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                all_tasks.append(task)
        return all_tasks, any_ok

    def _fetch_tasks(
        self,
    ) -> tuple[list[dict[str, Any]], int, str, bool]:
        coin_tasks, coin_ok = self._fetch_coin_tasks()
        if coin_ok and coin_tasks:
            return coin_tasks, 6, "coins", True
        if not coin_ok:
            print("[任务] 金币任务列表不可用，尝试旧任务列表")

        tasks, tasks_ok = self._fetch_tasks_from(
            API_TASK_LIST,
            TASK_FROM,
            "旧任务列表",
        )
        if tasks_ok:
            return tasks, 1, "taskscore", True
        if coin_ok:
            return coin_tasks, 6, "coins", True
        return [], 1, "none", False

    def _find_sign_task(self) -> dict[str, Any] | None:
        for path, task_from, purpose in (
            (API_COIN_TASK_LIST, COIN_TASK_FROM, "金币任务列表"),
            (API_TASK_LIST, TASK_FROM, "旧任务列表"),
        ):
            tasks, ok = self._fetch_tasks_from(path, task_from, purpose)
            if not ok:
                continue
            for task in tasks:
                if to_int(task.get("task_type")) == 166:
                    return task
                if "今日签到" in str(task.get("task_name") or ""):
                    return task
        return None

    def _sign_in(self) -> SignInResult:
        print("[签到] 查询今日签到状态")
        days = 0
        try:
            home_payload = self._api(
                API_COIN_HOME,
                {"go_home": "1", "ptype": "5"},
                "签到首页",
            )
            if to_int(home_payload.get("errno")) == 0:
                home_data = home_payload.get("data")
                if isinstance(home_data, dict):
                    days = to_int(home_data.get("signin_days"))
                    if to_int(home_data.get("signed")) == 1:
                        print(f"  · 今日已签到（连续 {days} 天）")
                        return SignInResult(
                            status="今日已签到",
                            message=f"今日已签到（连续 {days} 天）",
                            signed=True,
                            already=True,
                            details={"连续签到": f"{days} 天"},
                        )
            else:
                print(
                    f"[签到] 状态查询失败（{errno_text(home_payload)}），"
                    "继续尝试签到",
                )
        except (requests.RequestException, ValueError) as error:
            print(f"[签到] 状态查询异常：{error}")

        sign_task = self._find_sign_task()
        if not sign_task:
            return SignInResult(
                status="签到失败",
                message="未找到「今日签到领奖」任务，无法提交签到",
                endpoint_ok=False,
            )

        task_id = str(sign_task.get("task_id_str") or sign_task.get("task_id") or "")
        task_from = str(sign_task.get("task_from") or "")
        if not task_id or not task_from:
            return SignInResult(
                status="签到失败",
                message="签到任务缺少 task_id/task_from",
                endpoint_ok=False,
            )

        sign_params = {
            "is_growth": "0",
            "task_id": task_id,
            "task_id_str": task_id,
            "task_from": task_from,
        }
        print("[签到] 提交签到")
        try:
            payload = self._api(API_COIN_SIGN, sign_params, "签到")
        except (requests.RequestException, ValueError) as error:
            return SignInResult(
                status="签到失败",
                message=f"签到请求失败：{error}",
                endpoint_ok=False,
            )

        if to_int(payload.get("errno")) != 0:
            return SignInResult(
                status="签到失败",
                message=f"签到失败（{errno_text(payload)}）",
                endpoint_ok=False,
            )

        data = payload.get("data")
        signed = isinstance(data, dict) and to_int(data.get("signed")) == 1
        if isinstance(data, dict):
            days = to_int(data.get("signin_days")) or days

        time.sleep(0.6)
        try:
            verify_payload = self._api(API_COIN_SIGN_LIST, sign_params, "签到复核")
            if to_int(verify_payload.get("errno")) == 0:
                verify_data = verify_payload.get("data")
                if isinstance(verify_data, dict):
                    verify_days = to_int(verify_data.get("signin_days"))
                    if verify_days > 0:
                        days = verify_days
                    if to_int(verify_data.get("signed_today")) == 1:
                        signed = True
            else:
                print(f"[签到] 复核失败（{errno_text(verify_payload)}）")
        except (requests.RequestException, ValueError) as error:
            print(f"[签到] 复核异常：{error}")

        if not signed:
            return SignInResult(
                status="签到未生效",
                message="签到接口返回成功，但今日签到状态仍未更新",
                endpoint_ok=False,
                details={"连续签到": f"{days} 天"} if days > 0 else {},
            )

        print(f"  ✅ 签到成功（连续 {days} 天）")
        return SignInResult(
            status="签到成功",
            message=f"签到成功（连续 {days} 天）",
            signed=True,
            details={"连续签到": f"{days} 天"},
        )

    def _claim_tasks(self, phase: str) -> ClaimSummary:
        summary = ClaimSummary()
        print(f"[任务] 开始{phase}")
        tasks, claimable_status, source, endpoint_ok = self._fetch_tasks()
        summary.endpoint_ok = endpoint_ok
        summary.fallback = source != "coins"
        if not endpoint_ok:
            message = "任务列表获取失败"
            summary.messages.append(message)
            print(f"[任务] {message}")
            return summary

        claimable_tasks = [
            task
            for task in tasks
            if to_int(task.get("task_status"), -1) == claimable_status
        ]
        summary.total = len(claimable_tasks)
        if claimable_tasks:
            print(f"[任务] 可领取 {len(claimable_tasks)} 个")
        if not claimable_tasks:
            return summary

        for task in claimable_tasks:
            task_id = str(task.get("task_id_str") or task.get("task_id") or "")
            task_name = str(task.get("task_name") or task_id or "未知任务")
            task_from = str(task.get("task_from") or "")
            if not task_id:
                summary.failed += 1
                summary.messages.append(f"{task_name}：缺少 task_id")
                continue

            # 旧任务列表用 tasksave 领取；金币任务列表与网页端一致，
            # 走 antisave + action=receive_award。
            claim_path = (
                API_TASK_ANTI_SAVE if source == "coins" else API_TASK_SAVE
            )
            claim_params: dict[str, Any] = {
                "task_id": task_id,
                "task_from": task_from,
                "uk": self.uk,
            }
            if claim_path == API_TASK_ANTI_SAVE:
                claim_params["action"] = "receive_award"

            try:
                claim_payload = self._api(
                    claim_path,
                    claim_params,
                    f"领取 {task_name}",
                )
            except (requests.RequestException, ValueError) as error:
                summary.failed += 1
                summary.messages.append(f"{task_name}：{error}")
                print(f"[任务] {task_name} 领取异常：{error}")
                time.sleep(random.uniform(0.6, 1.2))
                continue

            errno = to_int(claim_payload.get("errno"))
            if errno == 0:
                reward = format_reward(claim_payload)
                summary.claimed += 1
                summary.messages.append(f"{task_name}：领取成功{(' ' + reward) if reward else ''}")
                print(f"  ✅ {task_name} 领取成功{(' ' + reward) if reward else ''}")
            elif errno == 40003:
                summary.already += 1
                summary.messages.append(f"{task_name}：已领取")
                print(f"  · {task_name} 已领取")
            else:
                summary.failed += 1
                message = f"{task_name}：{errno_text(claim_payload) or '领取失败'}"
                summary.messages.append(message)
                print(f"  ❌ {message}")
            time.sleep(random.uniform(0.6, 1.2))

        print(
            f"[任务] {phase}完成：成功 {summary.claimed}，已领 {summary.already}，"
            f"失败 {summary.failed}",
        )
        return summary

    def _choose_quiz_answer(self, question: dict[str, Any]) -> tuple[int, str]:
        if self.quiz_answer_override:
            parsed_override = parse_answer_value(self.quiz_answer_override)
            if parsed_override is not None:
                return parsed_override, "手动指定"
            print("[答题] BAIDU_TASK_QUIZ_ANSWER 无效，应为 0/1/否/是")

        parsed_answer = parse_answer_value(question.get("answer"))
        if parsed_answer is not None:
            return parsed_answer, "接口答案"

        return random.choice((0, 1)), "随机选择"

    def _answer_daily_question(self) -> QuizResult:
        print("[答题] 获取每日问题")
        try:
            payload = self._api(API_DAILY_QUESTION, {}, "每日问题")
        except (requests.RequestException, ValueError) as error:
            return QuizResult(
                status="获取失败",
                message=f"获取每日问题失败：{error}",
                endpoint_ok=False,
            )

        if to_int(payload.get("errno")) != 0:
            return QuizResult(
                status="获取失败",
                message=f"获取每日问题失败（{errno_text(payload)}）",
                endpoint_ok=False,
            )

        question = payload.get("data")
        if not isinstance(question, dict):
            return QuizResult(
                status="获取失败",
                message="每日问题结构异常",
                endpoint_ok=False,
            )

        answer_status = to_int(question.get("answer_status"), -1)
        ask_id = question.get("ask_id")
        question_text = str(question.get("question") or "(空)").replace("\n", " ")
        print(f"[答题] 题目：{question_text}")
        answer_status_text = {
            1: "今日已答对",
            2: "今日已答错",
        }.get(answer_status, "今日待作答")
        print(f"[答题] {answer_status_text}")

        if answer_status == 1:
            details = self._quiz_details(question)
            return QuizResult(
                status="今日已答对",
                message="今日已经答对过，无需重复答题",
                correct=True,
                should_claim=False,
                details=details,
            )
        if answer_status == 2:
            return QuizResult(
                status="今日已答错",
                message="今日已答错且无法重试，明天再来",
                correct=False,
            )
        if ask_id in (None, ""):
            return QuizResult(
                status="获取失败",
                message="每日问题缺少 ask_id",
                endpoint_ok=False,
            )

        answer_index, answer_source = self._choose_quiz_answer(question)
        answer_label = "是" if answer_index == 1 else "否"
        print(f"[答题] 提交答案：{answer_label}（{answer_source}）")

        try:
            submit_payload = self._api(
                API_ANSWER_QUESTION,
                {"ask_id": ask_id, "answer": answer_index},
                "提交答案",
            )
        except (requests.RequestException, ValueError) as error:
            return QuizResult(
                status="提交失败",
                message=f"提交答案失败：{error}",
                endpoint_ok=False,
            )

        if to_int(submit_payload.get("errno")) != 0:
            return QuizResult(
                status="提交失败",
                message=f"提交答案失败（{errno_text(submit_payload)}）",
                endpoint_ok=False,
            )

        result = submit_payload.get("data")
        if not isinstance(result, dict):
            result = {}
        details = self._quiz_details(result)
        if to_int(result.get("answer_status")) == 1:
            time.sleep(0.6)
            try:
                verify_payload = self._api(
                    API_DAILY_QUESTION,
                    {},
                    "答题复核",
                )
                if to_int(verify_payload.get("errno")) == 0:
                    verify_data = verify_payload.get("data")
                    if (
                        not isinstance(verify_data, dict)
                        or to_int(verify_data.get("answer_status")) != 1
                    ):
                        print("  ⚠️ 答题复核未通过")
                        return QuizResult(
                            status="答题未生效",
                            message="提交成功但复核仍显示未答对",
                            correct=False,
                            endpoint_ok=False,
                            details=details,
                        )
                    details["答题复核"] = "已记录"
                else:
                    print(f"[答题] 复核失败（{errno_text(verify_payload)}）")
            except (requests.RequestException, ValueError) as error:
                print(f"[答题] 复核异常：{error}")

            score = result.get("score", question.get("task_score"))
            points = result.get("points")
            reward_parts: list[str] = []
            if score not in (None, ""):
                details["答题得分"] = str(score)
                reward_parts.append(f"得分 {score}")
            if points not in (None, ""):
                details["答题积分"] = str(points)
                reward_parts.append(f"积分 {points}")
            reward_text = "，".join(reward_parts)
            print(f"  ✅ 回答正确{('，' + reward_text) if reward_text else ''}")
            return QuizResult(
                status="回答正确",
                message=(
                    f"回答正确（{answer_source}）"
                    + (f"，{reward_text}" if reward_text else "")
                ),
                correct=True,
                should_claim=True,
                details=details,
            )

        print("  ❌ 回答错误")
        return QuizResult(
            status="回答错误",
            message=f"回答错误（选择「{answer_label}」，{answer_source}），明天再来",
            correct=False,
            details=details,
        )

    @staticmethod
    def _quiz_details(question: dict[str, Any]) -> dict[str, str]:
        details: dict[str, str] = {}
        if question.get("score") not in (None, ""):
            details["当前得分"] = str(question.get("score"))
        if question.get("points") not in (None, ""):
            details["累计积分"] = str(question.get("points"))
        return details


def escape_html_text(value: Any) -> str:
    return html.escape(str(value).replace("\n", " "), quote=False)


def html_code(value: Any) -> str:
    return f"<code>{escape_html_text(value)}</code>"


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

    print(f"[通知] 面板系统通知调用完成：{response}")
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


def account_status_icon(success: bool, warning: bool) -> str:
    if not success:
        return "❌"
    if warning:
        return "⚠️"
    return "✅"


def build_account_notification(result: AccountResult) -> tuple[str, str, str]:
    status_icon = account_status_icon(result.success, result.warning)
    title = f"百度网盘任务系统 账号 {result.account_number} {result.status}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_lines = [
        "<b>百度网盘任务系统</b>",
        "",
        f"{status_icon} <b>账号 {result.account_number} · "
        f"{escape_html_text(result.account_label)}</b>",
        f"• 状态：{escape_html_text(result.status)}",
        f"• 签到：{escape_html_text(result.signin_message)}",
        f"• 任务：{escape_html_text(result.claim_message)}",
        f"• 答题：{escape_html_text(result.quiz_message)}",
    ]
    plain_lines = [
        "百度网盘任务系统",
        "",
        f"{status_icon} 账号 {result.account_number} · {result.account_label}",
        f"• 状态：{result.status}",
        f"• 签到：{result.signin_message}",
        f"• 任务：{result.claim_message}",
        f"• 答题：{result.quiz_message}",
    ]
    for detail_key, detail_value in result.details.items():
        html_lines.append(f"• {escape_html_text(detail_key)}：{html_code(detail_value)}")
        plain_lines.append(f"• {detail_key}：{detail_value}")
    html_lines.append(f"• 时间：{html_code(execution_time)}")
    plain_lines.append(f"• 时间：{execution_time}")

    return title, "\n".join(html_lines), "\n".join(plain_lines)


def build_summary_notification(results: list[AccountResult]) -> tuple[str, str, str]:
    total_count = len(results)
    successful_count = sum(1 for result in results if result.success)
    failed_count = total_count - successful_count
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"百度网盘任务系统汇总 {successful_count}/{total_count}"

    html_lines = [
        "<b>百度网盘任务系统汇总</b>",
        "",
        "<b>执行概览</b>",
        f"• 成功：{html_code(successful_count)}",
        f"• 失败：{html_code(failed_count)}",
        f"• 时间：{html_code(execution_time)}",
    ]
    plain_lines = [
        "百度网盘任务系统汇总",
        "",
        "执行概览",
        f"• 成功：{successful_count}",
        f"• 失败：{failed_count}",
        f"• 时间：{execution_time}",
    ]

    for result in results:
        result_icon = account_status_icon(result.success, result.warning)
        html_lines.append(
            f"{result_icon} 账号 {html_code(result.account_number)} · "
            f"{escape_html_text(result.account_label)}",
        )
        plain_lines.append(
            f"{result_icon} 账号 {result.account_number} · {result.account_label}",
        )

    return title, "\n".join(html_lines), "\n".join(plain_lines)


def build_configuration_error_notification(error_message: str) -> tuple[str, str, str]:
    title = "百度网盘任务系统配置错误"
    html_content = "\n\n".join(
        [
            "<b>百度网盘任务系统</b>",
            f"<b>配置提示</b>\n• {escape_html_text(error_message)}",
        ],
    )
    plain_content = "\n\n".join(
        [
            "百度网盘任务系统",
            f"配置提示\n• {error_message}",
        ],
    )
    return title, html_content, plain_content


def read_account_cookies() -> list[str]:
    raw_cookie = os.getenv("BAIDU_COOKIE", "").strip()
    if not raw_cookie:
        return []

    cookies: list[str] = []
    for line in raw_cookie.replace("\r", "\n").split("\n"):
        cookie = line.strip()
        if not cookie:
            continue
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        cookies.append(cookie)
    return cookies


def main() -> int:
    print(
        f"==== 百度网盘任务系统签到开始 "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====",
    )

    runtime_settings = load_task_runtime_settings(
        default_request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    timeout_seconds = runtime_settings.request_timeout_seconds
    privacy_mode = read_boolean_environment("PRIVACY_MODE", True)
    activity_url = (
        os.getenv("BAIDU_TASK_ACTIVITY_URL", "").strip() or DEFAULT_ACTIVITY_URL
    )
    quiz_answer_override = os.getenv("BAIDU_TASK_QUIZ_ANSWER", "").strip()

    cookies = read_account_cookies()
    if not cookies:
        error_message = "未配置 BAIDU_COOKIE 环境变量"
        print(f"[配置错误] {error_message}")
        title, html_content, plain_content = build_configuration_error_notification(
            error_message,
        )
        send_notifications(title, html_content, plain_content, timeout_seconds)
        return 1

    print(f"[配置] 共发现 {len(cookies)} 个账号")
    print(f"[配置] 活动页面：{activity_url}")
    print(f"[配置] 隐私保护模式：{'已启用' if privacy_mode else '已禁用'}")

    apply_startup_random_delay(
        "百度网盘任务系统签到",
        runtime_settings,
        has_work=bool(cookies),
    )

    results: list[AccountResult] = []
    for index, cookie in enumerate(cookies):
        if index > 0:
            wait_between_accounts(
                index,
                len(cookies),
                runtime_settings.account_delay_seconds,
            )

        client = BaiduPanTaskClient(
            cookie=cookie,
            account_number=index + 1,
            timeout_seconds=timeout_seconds,
            activity_url=activity_url,
            quiz_answer_override=quiz_answer_override,
            privacy_mode=privacy_mode,
        )
        result = client.run()
        results.append(result)

        for detail_key, detail_value in result.details.items():
            print(f"  {detail_key}：{detail_value}")
        print(f"[结果] 账号 {result.account_number}：{result.status}")

        title, html_content, plain_content = build_account_notification(result)
        send_notifications(title, html_content, plain_content, timeout_seconds)

    if len(results) > 1:
        title, html_content, plain_content = build_summary_notification(results)
        send_notifications(title, html_content, plain_content, timeout_seconds)

    successful_count = sum(1 for result in results if result.success)
    print(
        f"\n==== 百度网盘任务系统签到完成 - 成功{successful_count}/{len(results)} - "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====",
    )
    return 0 if successful_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
