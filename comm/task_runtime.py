#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""青龙任务共用的环境配置读取和等待逻辑。"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass


DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_ACCOUNT_DELAY_SECONDS = 3.0
DEFAULT_RANDOM_DELAY_MAX_SECONDS = 3600.0


@dataclass(frozen=True)
class TaskRuntimeSettings:
    request_timeout_seconds: float
    account_delay_seconds: float
    random_signin_enabled: bool
    random_delay_max_seconds: float


def read_boolean_environment(variable_name: str, default_value: bool) -> bool:
    raw_value = os.getenv(variable_name)
    if raw_value is None:
        return default_value
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def read_positive_float_environment(
    variable_name: str,
    default_value: float,
) -> float:
    """读取有限正数，适用于网络请求超时。"""
    return _read_float_environment(
        variable_name,
        default_value,
        minimum_value=0.0,
        minimum_inclusive=False,
    )


def read_nonnegative_float_environment(
    variable_name: str,
    default_value: float,
) -> float:
    """读取有限非负数，适用于任务等待时间。"""
    return _read_float_environment(
        variable_name,
        default_value,
        minimum_value=0.0,
        minimum_inclusive=True,
    )


def _read_float_environment(
    variable_name: str,
    default_value: float,
    *,
    minimum_value: float,
    minimum_inclusive: bool,
) -> float:
    raw_value = os.getenv(variable_name, str(default_value)).strip()
    try:
        parsed_value = float(raw_value)
    except ValueError:
        print(f"[配置] {variable_name}={raw_value!r} 无效，使用默认值 {default_value}")
        return default_value

    value_is_finite = math.isfinite(parsed_value)
    value_meets_minimum = (
        parsed_value >= minimum_value
        if minimum_inclusive
        else parsed_value > minimum_value
    )
    if value_is_finite and value_meets_minimum:
        return parsed_value

    requirement = "不能为负数" if minimum_inclusive else "必须大于 0"
    print(f"[配置] {variable_name} {requirement}且必须为有限数，使用默认值 {default_value}")
    return default_value


def load_task_runtime_settings(
    *,
    default_request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    default_account_delay_seconds: float = DEFAULT_ACCOUNT_DELAY_SECONDS,
    default_random_delay_max_seconds: float = DEFAULT_RANDOM_DELAY_MAX_SECONDS,
) -> TaskRuntimeSettings:
    return TaskRuntimeSettings(
        request_timeout_seconds=read_positive_float_environment(
            "TASK_TIMEOUT",
            default_request_timeout_seconds,
        ),
        account_delay_seconds=read_nonnegative_float_environment(
            "TASK_ACCOUNT_DELAY",
            default_account_delay_seconds,
        ),
        random_signin_enabled=read_boolean_environment(
            "TASK_RANDOM_SIGNIN",
            True,
        ),
        random_delay_max_seconds=read_nonnegative_float_environment(
            "TASK_RANDOM_DELAY_MAX",
            default_random_delay_max_seconds,
        ),
    )


def format_time_remaining(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
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
    remaining_seconds = max(0.0, delay_seconds)
    while remaining_seconds > 0:
        print(f"{task_name} 倒计时：{format_time_remaining(remaining_seconds)}")
        sleep_seconds = (
            min(1.0, remaining_seconds)
            if remaining_seconds <= 10
            else min(10.0, remaining_seconds)
        )
        time.sleep(sleep_seconds)
        remaining_seconds -= sleep_seconds


def apply_startup_random_delay(
    task_name: str,
    settings: TaskRuntimeSettings,
    *,
    has_work: bool,
) -> float:
    if (
        not has_work
        or not settings.random_signin_enabled
        or settings.random_delay_max_seconds <= 0
    ):
        return 0.0

    delay_seconds = random.uniform(0, settings.random_delay_max_seconds)
    print(f"[随机延迟] {format_time_remaining(delay_seconds)}")
    wait_with_countdown(delay_seconds, task_name)
    return delay_seconds


def wait_between_accounts(
    current_account_number: int,
    total_account_count: int,
    account_delay_seconds: float,
) -> None:
    has_next_account = current_account_number < total_account_count
    if not has_next_account or account_delay_seconds <= 0:
        return
    print(f"等待 {account_delay_seconds:g} 秒后处理下一个账号")
    time.sleep(account_delay_seconds)
