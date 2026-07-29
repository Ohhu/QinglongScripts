# 通用模块

本目录存放多个任务共同使用、不能独立运行的 Python 模块：

```text
comm/
├── cookie_store.py          通用 Cookie 持久化存储
├── discuz_login_common.py   Discuz 论坛登录、验证及通知实现
├── task_runtime.py          通用任务配置、随机延迟和账号间隔
└── token_store.py           通用访问令牌和刷新令牌持久化存储
```

这些文件不应由青龙自动创建为定时任务。仓库订阅需要把它们同时配置为
“黑名单”和“依赖文件”，以便入口脚本可以导入，但不生成无效任务。

```text
黑名单：^comm/(cookie_store|discuz_login_common|task_runtime|token_store)\.py$
依赖文件：^comm/(cookie_store|discuz_login_common|task_runtime|token_store)\.py$
```

Cookie 存储模块会根据调用方传入的存储名称创建独立文件。当前使用 Cookie
持久化的登录及签到任务分别使用：

```text
/ql/data/scripts_data/livecodes_login_cookies.json
/ql/data/scripts_data/mc168_login_cookies.json
/ql/data/scripts_data/sxsy_cookies.json
/ql/data/scripts_data/hifini_cookies.json
/ql/data/scripts_data/soushuba_login_cookies.json
/ql/data/scripts_data/mt_cookies.json
/ql/data/scripts_data/qmj_cookies.json
/ql/data/scripts_data/mikoto_tv_cookies.json
```

MiraiEmby 使用访问令牌和刷新令牌而不是登录 Cookie，保存在：

```text
/ql/data/scripts_data/miraiemby_tokens.json
```

每个文件可以保存该任务下的多个账号。写入采用锁文件和原子替换，避免同一
任务被重复运行时互相覆盖。

`sxsy_cookies.json` 的旧版平铺结构会在读取时自动识别，并在下一次成功写入
时转换为统一的 `version/accounts` 结构。
