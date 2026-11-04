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
  2. 读取 /coins/taskcenter/signinlist 的连续签到奖励，领取
     received=1 的 7/15/30/60/90 天奖励（/coins/taskcenter/signinaward）；
  3. 对当前 90 天签到周期内的漏签日调用
     /coins/taskcenter/supptasklist 查询补签方式；只使用 SVIP 免费额度
     （supp_type=1）补最近一天，不消耗金币、不自动做补签任务，每日限 1 次；
  4. 调用 /act/v2/membergrowv2/getdailyquestion 获取判断题并提交答案；
     答对后调用 /api/taskscore/tasksave 上报「每日答题」任务（task_type=169）；
  5. 任务进入 task_status=6（待领取）后调用 /api/taskscore/antisave
     （action=receive_award）领取答题奖励，领取后再复核 task_status 是否变为 1。
     网页端领取按钮走的正是 antisave；该接口不能带 hjs=1，否则返回 8001。
     答题奖励同时给积分与成长值：成长值只取 antisave 返回的 addGrowScore，
     积分在 addScore；answerquestion 的 score 只是题目分值，不能计入成长值。

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
from datetime import datetime, timedelta
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
API_COIN_SIGN_AWARD = "/coins/taskcenter/signinaward"
API_COIN_SUPP_TASK = "/coins/taskcenter/supptasklist"
API_COIN_SUPP_SIGN = "/coins/taskcenter/suppsignin"

COIN_TASK_FROM = [task_from for task_from in TASK_FROM if task_from != "task_sys_badge"]
GROWTH_TASK_FROM = ["task_sys_task_growth"]

ERRNO_MEANING = {
    "-6": "未登录",
    "0": "OK",
    "2": "参数错误",
    "40003": "已领取",
    "40008": "任务当前不可领取",
    "8001": "HTJ 反作弊",
    "8002": "jsToken 校验失败",
    "8011": "作弊频控",
    "8024": "作弊频控",
    "9312": "设备重复",
    "35025": "该日已签到",
    "35026": "今日补签次数已用完",
    "36353": "未达到领取条件",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass
class SignInResult:
    status: str
    message: str
    signed: bool = False
    already: bool = False
    endpoint_ok: bool = True
    days: int = 0
    daily_growth: int = 0
    supplement_growth: int = 0
    reward_growth: int = 0
    supplement_text: str = ""
    reward_text: str = ""
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class SignRewardResult:
    claimed: int = 0
    growth: int = 0
    days: list[int] = field(default_factory=list)
    message: str = ""
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class SupplementResult:
    supplemented: bool = False
    message: str = ""
    date_text: str = ""
    growth: int = 0
    month_left: int = 0
    month_total: int = 0
    day_limit: int = 0
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class QuizResult:
    status: str
    message: str
    correct: bool = False
    # 仅统计真正入账的成长值（由 antisave 领奖返回的 addGrowScore 决定）；
    # answerquestion 的 score 只是题目分值，不能计入成长值。
    growth: int = 0
    endpoint_ok: bool = True
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class AccountResult:
    account_number: int
    account_label: str
    success: bool
    status: str
    quiz_message: str
    signin_message: str = ""
    supplement_message: str = ""
    reward_message: str = ""
    signin_growth: int = 0
    supplement_growth: int = 0
    reward_growth: int = 0
    quiz_growth: int = 0
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


def format_prize_list(prizes: Any) -> str:
    if not isinstance(prizes, list):
        return ""
    prize_names = {10: "积分", 3: "成长值"}
    parts: list[str] = []
    for prize in prizes:
        if not isinstance(prize, dict):
            continue
        unit = to_int(prize.get("unit"))
        if unit <= 0:
            continue
        prize_name = prize_names.get(to_int(prize.get("type")), "奖励")
        parts.append(f"+{unit} {prize_name}")
    return "、".join(parts)


def sum_growth_prizes(prizes: Any) -> int:
    if not isinstance(prizes, list):
        return 0
    total = 0
    for prize in prizes:
        if not isinstance(prize, dict):
            continue
        if to_int(prize.get("type")) == 3:
            total += max(to_int(prize.get("unit")), 0)
    return total


def format_task_reward(payload: dict[str, Any]) -> str:
    """从 antisave/tasksave 的 result 中拼出奖励文案。"""
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
        space_unit = "GB" if to_int(result.get("space_unit"), 1) == 1 else "MB"
        parts.append(f"+{add_space}{space_unit} 空间")
    return "、".join(parts)


def extract_task_growth(payload: dict[str, Any]) -> int:
    result = payload.get("result")
    if not isinstance(result, dict):
        result = payload.get("task_result")
    if not isinstance(result, dict):
        return 0
    return max(to_int(result.get("addGrowScore")), 0)


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
            quiz_result = self._answer_daily_question()

            endpoints_ok = signin_result.endpoint_ok and quiz_result.endpoint_ok
            success = endpoints_ok and signin_result.signed
            warning = success and not quiz_result.correct
            if not success:
                status = "失败"
            elif warning:
                status = "完成（有提示）"
            else:
                status = "完成"
            quiz_message = {
                "回答正确": "答对",
                "今日已答对": "已答对",
                "回答错误": "答错",
                "今日已答错": "已答错",
                "答题未生效": "未生效",
                "提交失败": "提交失败",
                "获取失败": "获取失败",
            }.get(quiz_result.status, "未完成")
            if signin_result.signed:
                signin_message = (
                    f"{'已签' if signin_result.already else '成功'}"
                    + (f" · 连续 {signin_result.days} 天" if signin_result.days else "")
                )
            else:
                signin_message = signin_result.message

            details: dict[str, str] = {}
            if quiz_result.details.get("答题任务"):
                details["答题任务"] = quiz_result.details["答题任务"]
            return AccountResult(
                account_number=self.account_number,
                account_label=account_label,
                success=success,
                status=status,
                quiz_message=quiz_message,
                signin_message=signin_message,
                supplement_message=signin_result.supplement_text,
                reward_message=signin_result.reward_text,
                signin_growth=signin_result.daily_growth,
                supplement_growth=signin_result.supplement_growth,
                reward_growth=signin_result.reward_growth,
                quiz_growth=quiz_result.growth,
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
            quiz_message="未执行",
            signin_message=message,
        )

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

        # 网页端 urlMap 把 antisave 的事件名映射为 task，领取时必须带 ev=task。
        event = "task" if path == API_TASK_ANTI_SAVE else ""
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

    @staticmethod
    def _build_sign_params(sign_task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(sign_task.get("task_id_str") or sign_task.get("task_id") or "")
        return {
            "is_growth": "0",
            "task_id": task_id,
            "task_id_str": task_id,
            "task_from": str(sign_task.get("task_from") or ""),
            "signToday": "1",
        }

    @staticmethod
    def _sign_day_growth(sign_data: dict[str, Any] | None, day_number: int) -> int:
        if not sign_data:
            return 0
        for item in sign_data.get("signin_list") or []:
            if not isinstance(item, dict):
                continue
            if to_int(item.get("day")) == day_number:
                return sum_growth_prizes(item.get("prize"))
        return 0

    def _fetch_sign_list(self, sign_task: dict[str, Any]) -> dict[str, Any] | None:
        try:
            payload = self._api(
                API_COIN_SIGN_LIST,
                self._build_sign_params(sign_task),
                "签到日历",
            )
        except (requests.RequestException, ValueError) as error:
            print(f"[签到] 日历获取失败：{error}")
            return None
        if to_int(payload.get("errno")) != 0:
            print(f"[签到] 日历获取失败（{errno_text(payload)}）")
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _supplement_sign(self, sign_data: dict[str, Any] | None) -> SupplementResult:
        result = SupplementResult()
        if not sign_data:
            return result

        signin_list = sign_data.get("signin_list")
        current_day = to_int(sign_data.get("day"))
        current_timestamp = to_int(sign_data.get("date"))
        if (
            not isinstance(signin_list, list)
            or current_day <= 0
            or current_timestamp <= 0
        ):
            return result

        current_date = datetime.fromtimestamp(current_timestamp)
        unsigned_days: list[tuple[int, datetime]] = []
        for item in signin_list:
            if not isinstance(item, dict):
                continue
            number = to_int(item.get("day"))
            if number <= 0 or number >= current_day:
                continue
            if to_int(item.get("signed")) != 0:
                continue
            date = current_date - timedelta(days=current_day - number)
            unsigned_days.append((number, date))
        if not unsigned_days:
            return result

        unsigned_days.sort(key=lambda pair: pair[0], reverse=True)
        latest_day, latest_date = unsigned_days[0]
        print(
            f"[补签] 当前周期漏签 {len(unsigned_days)} 天，"
            f"最近 {latest_date.strftime('%m.%d')}",
        )

        quota_logged = False
        for number, date in unsigned_days:
            date_text = date.strftime("%m.%d")
            try:
                payload = self._api(
                    API_COIN_SUPP_TASK,
                    {"day": number},
                    f"补签信息 {date_text}",
                )
            except (requests.RequestException, ValueError) as error:
                print(f"[补签] {date_text} 信息获取失败：{error}")
                continue

            if to_int(payload.get("errno")) != 0:
                print(f"[补签] {date_text} 信息获取失败（{errno_text(payload)}）")
                continue

            data = payload.get("data")
            if not isinstance(data, dict):
                continue

            supp_type = to_int(data.get("supp_type"))
            coins_consumed = to_int(data.get("coins_consumed"))
            month_count = to_int(data.get("month_svip_supp_sign_count"))
            month_left = to_int(data.get("month_svip_supp_sign_count_left"))
            day_count = to_int(data.get("day_svip_supp_sign_count"))
            if not quota_logged and month_count > 0:
                quota_logged = True
                result.month_left = month_left
                result.month_total = month_count
                result.day_limit = day_count
                quota_text = (
                    f"补签前剩余 {month_left}/{month_count} 次，"
                    f"每日限 {day_count} 次"
                )
                result.details["本月补签"] = quota_text
                print(f"  · 本月补签{quota_text}")

            if supp_type != 1:
                if supp_type == 3:
                    reason = f"需 {coins_consumed} 金币"
                elif supp_type == 2:
                    reason = "需完成任务"
                else:
                    reason = "当前不可用"
                result.message = f"{date_text} {reason}，未自动补签"
                print(f"  · {date_text} {reason}，跳过")
                return result

            if month_left <= 0:
                print("  · 接口显示本月额度已用完，仍交由服务端判断")

            try:
                sign_payload = self._api(
                    API_COIN_SUPP_SIGN,
                    {"day": number, "supp_type": 1},
                    f"补签 {date_text}",
                )
            except (requests.RequestException, ValueError) as error:
                result.message = f"{date_text} 补签请求失败：{error}"
                print(f"  ❌ {result.message}")
                return result

            errno = to_int(sign_payload.get("errno"))
            if errno == 0:
                data = sign_payload.get("data")
                new_days = to_int(data.get("signin_days")) if isinstance(data, dict) else 0
                new_days = new_days or to_int(sign_data.get("signin_days"))
                result.supplemented = True
                result.date_text = date_text
                result.growth = self._sign_day_growth(sign_data, number)
                result.message = f"已补签 {date_text}（连续 {new_days} 天）"
                result.details["补签"] = f"{date_text}（连续 {new_days} 天）"
                print(f"  ✅ 补签 {date_text} 成功")
                return result

            if errno == 35026:
                result.message = "今日补签次数已用完"
                print(f"  · {result.message}")
                return result

            result.message = f"{date_text} 补签失败（{errno_text(sign_payload)}）"
            print(f"  ❌ {result.message}")
            return result

        return result

    def _claim_sign_rewards(self, sign_data: dict[str, Any] | None) -> SignRewardResult:
        result = SignRewardResult()
        if not sign_data:
            return result

        rewards = sign_data.get("reward_list")
        if not isinstance(rewards, list):
            return result

        claimable = [
            item
            for item in rewards
            if isinstance(item, dict) and to_int(item.get("received")) == 1
        ]
        if not claimable:
            return result

        print(f"[签到] 发现 {len(claimable)} 个连续签到奖励")
        messages: list[str] = []
        for item in claimable:
            day = to_int(item.get("day"))
            if day <= 0:
                continue
            try:
                payload = self._api(
                    API_COIN_SIGN_AWARD,
                    {"day": day},
                    f"连续签到第 {day} 天奖励",
                )
            except (requests.RequestException, ValueError) as error:
                print(f"[签到] 连续签到第 {day} 天奖励领取异常：{error}")
                continue

            errno = to_int(payload.get("errno"))
            if errno == 0:
                reward_text = format_prize_list(item.get("prize"))
                result.claimed += 1
                result.growth += sum_growth_prizes(item.get("prize"))
                result.days.append(day)
                messages.append(f"第 {day} 天{(' ' + reward_text) if reward_text else ''}")
                print(
                    f"  ✅ 连续签到第 {day} 天奖励领取成功"
                    f"{(' ' + reward_text) if reward_text else ''}",
                )
            elif errno == 36353:
                print(f"  · 连续签到第 {day} 天奖励尚未达到领取条件")
            else:
                print(f"  ❌ 连续签到第 {day} 天奖励领取失败（{errno_text(payload)}）")
            time.sleep(random.uniform(0.6, 1.2))

        if messages:
            result.message = "，".join(messages)
            result.details["连续签到奖励"] = result.message
        return result

    def _sign_in(self) -> SignInResult:
        print("[签到] 查询今日签到状态")
        days = 0
        signed = False
        already_signed = False
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
                    already_signed = to_int(home_data.get("signed")) == 1
                    signed = already_signed
                    if already_signed:
                        print(f"  · 今日已签到（连续 {days} 天）")
            else:
                print(
                    f"[签到] 状态查询失败（{errno_text(home_payload)}），"
                    "继续尝试签到",
                )
        except (requests.RequestException, ValueError) as error:
            print(f"[签到] 状态查询异常：{error}")

        sign_task = self._find_sign_task()
        if not signed:
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

            sign_params = self._build_sign_params(sign_task)
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

        sign_data = self._fetch_sign_list(sign_task) if sign_task else None
        if sign_data:
            days = to_int(sign_data.get("signin_days")) or days

        supplement_result = self._supplement_sign(sign_data)
        if supplement_result.supplemented:
            sign_data = self._fetch_sign_list(sign_task) if sign_task else sign_data
            if sign_data:
                days = to_int(sign_data.get("signin_days")) or days
            print(f"  · 补签后连续 {days} 天")
            if supplement_result.date_text:
                supplement_result.message = (
                    f"已补签 {supplement_result.date_text}（连续 {days} 天）"
                )
                supplement_result.details["补签"] = (
                    f"{supplement_result.date_text}（连续 {days} 天）"
                )
            if supplement_result.month_total > 0:
                supplement_result.details["本月补签"] = (
                    f"补签前剩余 {supplement_result.month_left}/"
                    f"{supplement_result.month_total} 次，"
                    f"每日限 {supplement_result.day_limit} 次（本次已用 1 次）"
                )

        reward_result = self._claim_sign_rewards(sign_data)

        daily_growth = 0
        if sign_data:
            daily_growth = self._sign_day_growth(
                sign_data,
                to_int(sign_data.get("day")),
            )

        details: dict[str, str] = {"连续签到": f"{days} 天"}
        details.update(supplement_result.details)
        if supplement_result.message and not supplement_result.supplemented:
            details["补签"] = supplement_result.message
        details.update(reward_result.details)

        message = (
            f"今日已签到（连续 {days} 天）"
            if already_signed
            else f"签到成功（连续 {days} 天）"
        )
        if supplement_result.supplemented:
            message += f"，{supplement_result.message}"
        if reward_result.claimed:
            message += f"，领取连续签到奖励 {reward_result.message}"

        reward_text = "、".join(f"{day} 天" for day in reward_result.days)
        return SignInResult(
            status="今日已签到" if already_signed else "签到成功",
            message=message,
            signed=True,
            already=already_signed,
            days=days,
            daily_growth=daily_growth,
            supplement_growth=supplement_result.growth,
            reward_growth=reward_result.growth,
            supplement_text=(
                supplement_result.date_text if supplement_result.supplemented else ""
            ),
            reward_text=reward_text,
            details=details,
        )

    def _find_growth_task(self, task_type: int) -> dict[str, Any] | None:
        tasks, ok = self._fetch_tasks_from(
            API_COIN_TASK_LIST,
            GROWTH_TASK_FROM,
            "成长任务列表",
        )
        if not ok:
            return None
        for task in tasks:
            if to_int(task.get("task_type")) == task_type:
                return task
        return None

    def _claim_task_reward(
        self,
        task: dict[str, Any],
        purpose: str,
    ) -> tuple[bool, str, int]:
        """调用 antisave receive_award 领取任务奖励。

        网页端领取按钮会把任务对象整体作为请求参数提交，这里同样带上任务字段，
        避免服务端判定参数缺失。返回（是否领取成功，说明，领取到的成长值）。
        """
        task_id = str(task.get("task_id_str") or task.get("task_id") or "")
        task_name = str(task.get("task_name") or task_id or "任务")
        if not task_id:
            return False, f"{task_name}缺少 task_id", 0

        params: dict[str, Any] = {
            key: value
            for key, value in task.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }
        params.update(
            {
                "task_id": task_id,
                "action": "receive_award",
                "uk": self.uk,
            },
        )

        print(f"[答题] 领取{task_name}奖励")
        try:
            payload = self._api(API_TASK_ANTI_SAVE, params, purpose)
        except (requests.RequestException, ValueError) as error:
            print(f"[答题] {task_name}奖励领取异常：{error}")
            return False, f"{task_name}奖励领取异常", 0

        errno = to_int(payload.get("errno"))
        if errno == 0:
            reward_text = format_task_reward(payload)
            growth = extract_task_growth(payload)
            message = f"已领取{task_name}奖励" + (
                f" {reward_text}" if reward_text else ""
            )
            print(f"  ✅ {message}")
            return True, message, growth
        if errno in (40003, 40008):
            print(f"  · {task_name}奖励当前不可领取（{errno_text(payload)}）")
            return False, f"{task_name}奖励当前不可领取", 0
        print(f"  ❌ {task_name}奖励领取失败（{errno_text(payload)}）")
        return False, f"{task_name}奖励领取失败（{errno_text(payload)}）", 0

    def _claim_quiz_task(self, task: dict[str, Any]) -> tuple[bool, str, int]:
        """领取答题任务奖励，并复核 task_status 是否变为 1（已领取）。"""
        claimed, message, growth = self._claim_task_reward(
            task,
            "领取答题奖励",
        )
        time.sleep(0.6)
        refreshed = self._find_growth_task(169)
        refreshed_status = to_int((refreshed or {}).get("task_status"), -1)
        if claimed:
            if refreshed_status == 1:
                return True, message, growth
            return True, f"{message}（任务状态 {refreshed_status}，可能延迟更新）", growth
        if refreshed_status == 1:
            # 重复领取时接口可能返回 40003/40008，以任务状态为准。
            return True, "答题任务已领取", 0
        return True, message, 0

    def _report_quiz_task(self) -> tuple[bool, str, int]:
        """答题后上报并领取任务奖励；返回（是否已处理，说明，领取到的成长值）。"""
        task = self._find_growth_task(169)
        if not task:
            return False, "", 0

        task_id = str(task.get("task_id_str") or task.get("task_id") or "")
        task_from = str(task.get("task_from") or "")
        status = to_int(task.get("task_status"))
        if status == 6:
            return self._claim_quiz_task(task)
        if status == 1:
            return True, "答题任务已完成", 0
        if status not in (0, 3):
            return True, f"答题任务状态 {status}", 0
        if not task_id or not task_from:
            return False, "答题任务缺少 task_id/task_from", 0

        print("[答题] 上报答题任务")
        try:
            payload = self._api(
                API_TASK_SAVE,
                {"task_id": task_id, "task_from": task_from},
                "答题任务上报",
            )
        except (requests.RequestException, ValueError) as error:
            print(f"[答题] 答题任务上报异常：{error}")
            return False, f"答题任务上报异常：{error}", 0

        errno = to_int(payload.get("errno"))
        if errno == 40003:
            return True, "答题任务已领取", 0
        if errno != 0:
            print(f"[答题] 答题任务上报失败（{errno_text(payload)}）")
            return False, f"答题任务上报失败（{errno_text(payload)}）", 0

        time.sleep(0.6)
        refreshed = self._find_growth_task(169)
        refreshed_status = to_int((refreshed or {}).get("task_status"), -1)
        if refreshed_status == 6:
            return self._claim_quiz_task(refreshed or task)
        if refreshed_status == 1:
            return True, "答题任务已完成", 0
        return True, "答题任务已上报，奖励待领取", 0

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
            details: dict[str, str] = {}
            _task_handled, task_message, growth = self._report_quiz_task()
            if task_message:
                details["答题任务"] = task_message
            message = "今日已经答对过，无需重复答题"
            if task_message:
                message += f"，{task_message}"
            return QuizResult(
                status="今日已答对",
                message=message,
                correct=True,
                growth=growth,
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
        details: dict[str, str] = {}
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
                else:
                    print(f"[答题] 复核失败（{errno_text(verify_payload)}）")
            except (requests.RequestException, ValueError) as error:
                print(f"[答题] 复核异常：{error}")

            _task_handled, task_message, growth = self._report_quiz_task()
            if task_message:
                details["答题任务"] = task_message
            print("  ✅ 回答正确" + (f"，{task_message}" if task_message else ""))
            return QuizResult(
                status="回答正确",
                message="答对",
                correct=True,
                growth=growth,
                details=details,
            )

        print("  ❌ 回答错误")
        return QuizResult(
            status="回答错误",
            message=f"回答错误（选择「{answer_label}」，{answer_source}），明天再来",
            correct=False,
            details=details,
        )


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

    entries: list[tuple[str, str, int]] = []
    if result.signin_message:
        entries.append(("签到", result.signin_message, result.signin_growth))
    if result.supplement_message:
        entries.append(("补签", result.supplement_message, result.supplement_growth))
    if result.reward_message:
        entries.append(("签到奖励", result.reward_message, result.reward_growth))
    if result.quiz_message:
        entries.append(("答题", result.quiz_message, result.quiz_growth))

    html_lines = [
        "<b>百度网盘任务系统</b>",
        "",
        f"{status_icon} <b>账号 {result.account_number} · "
        f"{escape_html_text(result.account_label)}</b>",
    ]
    plain_lines = [
        "百度网盘任务系统",
        "",
        f"{status_icon} 账号 {result.account_number} · {result.account_label}",
    ]
    for label, text, growth in entries:
        suffix = f" · +{growth} 成长值" if growth > 0 else ""
        html_lines.append(f"• {label}：{escape_html_text(text)}{suffix}")
        plain_lines.append(f"• {label}：{text}{suffix}")

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
