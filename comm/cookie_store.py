#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为青龙任务提供按任务隔离的 Cookie 持久化存储。"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
STALE_LOCK_SECONDS = 60.0
STORE_VERSION = 1
VALID_STORE_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class CookieStore:
    """在青龙持久化目录中保存一个任务的多账号 Cookie。"""

    def __init__(self, store_name: str) -> None:
        normalized_store_name = store_name.strip()
        if not VALID_STORE_NAME_PATTERN.fullmatch(normalized_store_name):
            raise ValueError(
                "Cookie 存储名称只能包含字母、数字、点、下划线和连字符",
            )

        filename = (
            normalized_store_name
            if normalized_store_name.endswith(".json")
            else f"{normalized_store_name}.json"
        )
        qinglong_data_root = os.getenv("QL_DATA_DIR", "").strip() or "/ql/data"
        self.store_path = os.path.join(
            qinglong_data_root,
            "scripts_data",
            filename,
        )
        self.lock_path = f"{self.store_path}.lock"

    def read(self, account_key: str) -> str:
        store_data = self._read_store_data()
        account_entries = store_data.get("accounts")
        if not isinstance(account_entries, dict):
            return ""

        account_entry = account_entries.get(account_key)
        if not isinstance(account_entry, dict):
            return ""
        return str(account_entry.get("cookie", "")).strip()

    def write(self, account_key: str, cookie: str) -> None:
        normalized_cookie = cookie.strip()
        if not account_key or not normalized_cookie:
            return

        try:
            with self._acquire_write_lock():
                store_data = self._read_store_data()
                account_entries = store_data.setdefault("accounts", {})
                if not isinstance(account_entries, dict):
                    account_entries = {}
                    store_data["accounts"] = account_entries

                account_entries[account_key] = {
                    "cookie": normalized_cookie,
                    "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._write_store_data_atomically(store_data)
        except (OSError, TimeoutError) as error:
            print(f"[Cookie存储] 获取写入锁失败，跳过本次保存：{error}")

    def remove(self, account_key: str) -> None:
        if not account_key:
            return

        try:
            with self._acquire_write_lock():
                store_data = self._read_store_data()
                account_entries = store_data.get("accounts")
                if (
                    not isinstance(account_entries, dict)
                    or account_key not in account_entries
                ):
                    return

                account_entries.pop(account_key)
                self._write_store_data_atomically(store_data)
        except (OSError, TimeoutError) as error:
            print(f"[Cookie存储] 获取写入锁失败，跳过本次清理：{error}")

    def _read_store_data(self) -> dict[str, Any]:
        try:
            with open(self.store_path, encoding="utf-8") as store_file:
                store_data = json.load(store_file)
        except FileNotFoundError:
            return self._empty_store_data()
        except (OSError, ValueError) as error:
            print(f"[Cookie存储] 读取失败，忽略本地缓存：{error}")
            return self._empty_store_data()

        if not isinstance(store_data, dict):
            print("[Cookie存储] 文件结构无效，忽略本地缓存")
            return self._empty_store_data()

        account_entries = store_data.get("accounts")
        if not isinstance(account_entries, dict):
            legacy_account_entries = {
                str(account_key): account_entry
                for account_key, account_entry in store_data.items()
                if isinstance(account_entry, dict)
                and str(account_entry.get("cookie", "")).strip()
            }
            if legacy_account_entries:
                print("[Cookie存储] 已识别旧格式，将在下次写入时自动迁移")
                store_data = {
                    "version": STORE_VERSION,
                    "accounts": legacy_account_entries,
                }
            else:
                store_data["accounts"] = {}
        store_data["version"] = STORE_VERSION
        return store_data

    def _write_store_data_atomically(self, store_data: dict[str, Any]) -> None:
        store_directory = os.path.dirname(self.store_path)
        temporary_path = f"{self.store_path}.{os.getpid()}.tmp"
        try:
            os.makedirs(store_directory, exist_ok=True)
            with open(temporary_path, "w", encoding="utf-8") as store_file:
                json.dump(store_data, store_file, ensure_ascii=False, indent=2)
                store_file.flush()
                os.fsync(store_file.fileno())
            self._restrict_file_permissions(temporary_path)
            os.replace(temporary_path, self.store_path)
        except OSError as error:
            print(f"[Cookie存储] 写入失败：{error}")
            try:
                os.remove(temporary_path)
            except OSError:
                pass

    @contextmanager
    def _acquire_write_lock(self) -> Iterator[None]:
        lock_directory = os.path.dirname(self.lock_path)
        os.makedirs(lock_directory, exist_ok=True)
        deadline = time.monotonic() + DEFAULT_LOCK_TIMEOUT_SECONDS
        lock_file_descriptor: int | None = None

        while lock_file_descriptor is None:
            try:
                lock_file_descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(lock_file_descriptor, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                self._remove_stale_lock()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"等待 Cookie 存储锁超时：{self.lock_path}",
                    )
                time.sleep(0.1)

        try:
            yield
        finally:
            os.close(lock_file_descriptor)
            try:
                os.remove(self.lock_path)
            except FileNotFoundError:
                pass

    def _remove_stale_lock(self) -> None:
        try:
            lock_age_seconds = time.time() - os.path.getmtime(self.lock_path)
            if lock_age_seconds > STALE_LOCK_SECONDS:
                os.remove(self.lock_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _restrict_file_permissions(file_path: str) -> None:
        try:
            os.chmod(file_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _empty_store_data() -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "accounts": {},
        }
