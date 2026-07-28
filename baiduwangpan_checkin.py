#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
cron: 0 9 * * *
new Env('百度网盘签到')

环境变量：
  BAIDU_COOKIE         必填。完整 Cookie，多账号使用换行分隔。
  TG_NOTIFY_CONFIG     可选。统一 Telegram 配置：BotToken|ChatID|APIHost；
                       配置后使用 HTML 直发，失败回退青龙纯文本通知。
"""

import os
import time
import re
import requests
import random
import builtins
import html
from datetime import datetime, timedelta

from comm.task_runtime import (
    apply_startup_random_delay,
    load_task_runtime_settings,
    read_boolean_environment,
    wait_between_accounts,
)

# 配置项
BAIDU_COOKIE = os.environ.get('BAIDU_COOKIE', '')
privacy_mode = read_boolean_environment("PRIVACY_MODE", True)
task_timeout = 20.0

HEADERS = {
    'Connection': 'keep-alive',
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 '
        'Safari/537.36'
    ),
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Dest': 'empty',
    'Referer': 'https://pan.baidu.com/wap/svip/growth/task',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
}

def escape_html_text(value):
    return html.escape(str(value).replace("\n", " "), quote=False)


def html_code(value):
    return f"<code>{escape_html_text(value)}</code>"


def read_telegram_notify_configuration():
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


def send_telegram_html_notification(content):
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
            timeout=task_timeout,
        )
    except requests.exceptions.RequestException as error:
        print(f"[通知] Telegram HTML 直发网络错误：{type(error).__name__}")
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


def send_system_notification(title, content):
    qinglong_api = getattr(builtins, "QLAPI", None)
    if qinglong_api is None:
        print(f"[通知] 非青龙环境，跳过：{title}")
        return False
    try:
        response = qinglong_api.systemNotify({"title": title, "content": content})
        if isinstance(response, dict) and response.get("code") == 200:
            print(f"[通知] 面板系统通知发送完成：{title}")
            return True
        else:
            print(f"[通知] 面板系统通知返回异常：{response}")
    except Exception as error:
        print(f"[通知] 面板系统通知调用失败：{type(error).__name__}: {error}")
    return False


def notify_user(title, html_content, plain_content):
    """优先发送 Telegram HTML，失败时回退青龙纯文本通知。"""
    if send_telegram_html_notification(html_content):
        return True

    print("[通知] 使用青龙纯文本通知回退")
    return send_system_notification(title, plain_content)


def build_account_notification(
    account_number,
    user,
    level,
    growth_value,
    vip_status,
    signin_message,
    answer_message,
    is_success,
):
    result_icon = "✅" if is_success else "❌"
    result_text = "成功" if is_success else "失败"
    title = f"百度网盘账号 {account_number} 签到{result_text}"
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_lines = [
        "<b>百度网盘每日签到</b>",
        "",
        f"{result_icon} <b>账号 {account_number} · {escape_html_text(user)}</b>",
        f"• 签到：{escape_html_text(signin_message)}",
    ]
    plain_lines = [
        "百度网盘每日签到",
        "",
        f"{result_icon} 账号 {account_number} · {user}",
        f"• 签到：{signin_message}",
    ]

    if answer_message:
        html_lines.append(f"• 答题：{escape_html_text(answer_message)}")
        plain_lines.append(f"• 答题：{answer_message}")

    html_lines.extend(
        [
            f"• 等级：{html_code(f'Lv.{level}')}（成长值 {html_code(growth_value)}）",
            f"• 会员：{escape_html_text(vip_status)}",
            f"• 时间：{html_code(execution_time)}",
        ]
    )
    plain_lines.extend(
        [
            f"• 等级：Lv.{level}（成长值 {growth_value}）",
            f"• 会员：{vip_status}",
            f"• 时间：{execution_time}",
        ]
    )
    return title, "\n".join(html_lines), "\n".join(plain_lines)


def build_configuration_error_notification(error_message):
    title = "百度网盘签到配置错误"
    html_content = "\n\n".join(
        [
            "<b>百度网盘每日签到</b>",
            f"<b>配置提示</b>\n• {escape_html_text(error_message)}",
        ]
    )
    plain_content = "\n\n".join(
        [
            "百度网盘每日签到",
            f"配置提示\n• {error_message}",
        ]
    )
    return title, html_content, plain_content


def build_summary_notification(results):
    total_count = len(results)
    successful_count = sum(result["success"] for result in results)
    failed_count = total_count - successful_count
    execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"百度网盘签到汇总 {successful_count}/{total_count}"

    html_lines = [
        "<b>百度网盘签到汇总</b>",
        "",
        "<b>执行概览</b>",
        f"• 成功：{html_code(successful_count)}",
        f"• 失败：{html_code(failed_count)}",
        f"• 时间：{html_code(execution_time)}",
    ]
    plain_lines = [
        "百度网盘签到汇总",
        "",
        "执行概览",
        f"• 成功：{successful_count}",
        f"• 失败：{failed_count}",
        f"• 时间：{execution_time}",
    ]

    for result in results:
        result_icon = "✅" if result["success"] else "❌"
        html_lines.append(
            f"{result_icon} 账号 {html_code(result['index'])} · "
            f"{escape_html_text(result['user'])}",
        )
        plain_lines.append(
            f"{result_icon} 账号 {result['index']} · {result['user']}",
        )

    return title, "\n".join(html_lines), "\n".join(plain_lines)

class BaiduPan:
    name = "百度网盘"

    def __init__(self, cookie: str, index: int = 1):
        self.cookie = cookie
        self.index = index
        self.final_messages = []

    def add_message(self, msg: str):
        """统一收集消息并打印"""
        print(msg)
        self.final_messages.append(msg)

    def signin(self):
        """执行每日签到"""
        if not self.cookie.strip():
            self.add_message("❌ 未检测到 BAIDU_COOKIE，请检查配置。")
            return False, "Cookie配置错误"

        print("📝 正在执行签到...")
        url = "https://pan.baidu.com/rest/2.0/membership/level?app_id=250528&web=5&method=signin"
        signed_headers = HEADERS.copy()
        signed_headers['Cookie'] = self.cookie
        
        try:
            resp = requests.get(url, headers=signed_headers, timeout=task_timeout)
            print(f"🔍 签到响应状态码: {resp.status_code}")
            
            if resp.status_code == 200:
                sign_point = re.search(r'points":(\d+)', resp.text)
                signin_error_msg = re.search(r'"error_msg":"(.*?)"', resp.text)

                if sign_point:
                    points = sign_point.group(1)
                    self.add_message(f"✅ 签到成功，获得积分: {points}")
                    print(f"🎁 今日奖励: {points}积分")
                    return True, f"签到成功，获得{points}积分"
                else:
                    # 检查是否有错误信息
                    if signin_error_msg and signin_error_msg.group(1):
                        error_msg = signin_error_msg.group(1)
                        if any(keyword in error_msg for keyword in ["已签到", "重复签到", "not allow"]):
                            self.add_message("📅 今日已签到")
                            return True, "今日已签到"
                        else:
                            self.add_message(f"❌ 签到失败: {error_msg}")
                            return False, f"签到失败: {error_msg}"
                    else:
                        self.add_message("✅ 签到成功，但未检索到积分信息")
                        return True, "签到成功"
            else:
                error_msg = f"签到失败，状态码: {resp.status_code}"
                self.add_message(f"❌ {error_msg}")
                return False, error_msg
                
        except requests.exceptions.Timeout:
            error_msg = "签到请求超时"
            self.add_message(f"❌ {error_msg}")
            return False, error_msg
        except requests.exceptions.ConnectionError:
            error_msg = "网络连接错误"
            self.add_message(f"❌ {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = f"签到请求异常: {e}"
            self.add_message(f"❌ {error_msg}")
            return False, error_msg

    def get_daily_question(self):
        """获取日常问题"""
        if not self.cookie.strip():
            return None, None

        print("🤔 正在获取每日问题...")
        url = "https://pan.baidu.com/act/v2/membergrowv2/getdailyquestion?app_id=250528&web=5"
        signed_headers = HEADERS.copy()
        signed_headers['Cookie'] = self.cookie
        
        try:
            resp = requests.get(url, headers=signed_headers, timeout=task_timeout)
            if resp.status_code == 200:
                answer = re.search(r'"answer":(\d+)', resp.text)
                ask_id = re.search(r'"ask_id":(\d+)', resp.text)
                question = re.search(r'"question":"(.*?)"', resp.text)
                
                if answer and ask_id:
                    if question:
                        print(f"❓ 今日问题: {question.group(1)}")
                        print(f"💡 答案: {answer.group(1)}")
                    return answer.group(1), ask_id.group(1)
                else:
                    self.add_message("⚠️ 未找到日常问题或答案")
            else:
                self.add_message(f"⚠️ 获取日常问题失败，状态码: {resp.status_code}")
        except Exception as e:
            self.add_message(f"⚠️ 获取问题请求异常: {e}")
        return None, None

    def answer_question(self, answer, ask_id):
        """回答每日问题"""
        if not self.cookie.strip():
            return False, "Cookie配置错误"

        print("📝 正在回答每日问题...")
        url = (
            "https://pan.baidu.com/act/v2/membergrowv2/answerquestion"
            f"?app_id=250528&web=5&ask_id={ask_id}&answer={answer}"
        )
        signed_headers = HEADERS.copy()
        signed_headers['Cookie'] = self.cookie
        
        try:
            resp = requests.get(url, headers=signed_headers, timeout=task_timeout)
            if resp.status_code == 200:
                answer_msg = re.search(r'"show_msg":"(.*?)"', resp.text)
                answer_score = re.search(r'"score":(\d+)', resp.text)

                if answer_score:
                    score = answer_score.group(1)
                    self.add_message(f"✅ 答题成功，获得积分: {score}")
                    print(f"🎁 答题奖励: {score}积分")
                    return True, f"答题成功，获得{score}积分"
                else:
                    # 检查答题信息
                    if answer_msg and answer_msg.group(1):
                        msg = answer_msg.group(1)
                        if any(keyword in msg for keyword in ["已回答", "exceeded", "超出", "超限"]):
                            self.add_message("📅 今日已答题或次数已用完")
                            return True, "今日已答题"
                        else:
                            self.add_message(f"❌ 答题失败: {msg}")
                            return False, f"答题失败: {msg}"
                    else:
                        self.add_message("✅ 答题成功，但未检索到积分信息")
                        return True, "答题成功"
            else:
                error_msg = f"答题失败，状态码: {resp.status_code}"
                self.add_message(f"❌ {error_msg}")
                return False, error_msg
        except Exception as e:
            error_msg = f"答题请求异常: {e}"
            self.add_message(f"❌ {error_msg}")
            return False, error_msg

    def get_user_info(self):
        """获取用户信息"""
        if not self.cookie.strip():
            return "未知用户", "未知", "未知", "未知"

        print("👤 正在获取用户信息...")
        url = "https://pan.baidu.com/rest/2.0/membership/user?app_id=250528&web=5&method=query"
        signed_headers = HEADERS.copy()
        signed_headers['Cookie'] = self.cookie
        
        try:
            resp = requests.get(url, headers=signed_headers, timeout=task_timeout)
            if resp.status_code == 200:
                current_value = re.search(r'current_value":(\d+)', resp.text)
                current_level = re.search(r'current_level":(\d+)', resp.text)
                username = re.search(r'"username":"(.*?)"', resp.text)
                vip_type = re.search(r'"vip_type":(\d+)', resp.text)

                level = current_level.group(1) if current_level else "未知"
                value = current_value.group(1) if current_value else "未知"
                user = username.group(1) if username else "未知用户"
                
                # VIP类型解析
                vip_status = "普通用户"
                if vip_type:
                    vip_code = int(vip_type.group(1))
                    if vip_code == 1:
                        vip_status = "普通会员"
                    elif vip_code == 2:
                        vip_status = "超级会员"
                    elif vip_code == 3:
                        vip_status = "至尊会员"

                # 隐私保护处理
                if privacy_mode and user != "未知用户":
                    if len(user) > 2:
                        user = f"{user[0]}***{user[-1]}"
                    else:
                        user = "***"

                level_msg = f"当前会员等级: Lv.{level}，成长值: {value}，会员类型: {vip_status}"
                self.add_message(level_msg)
                
                print(f"👤 用户: {user}")
                print(f"🏆 等级: Lv.{level}")
                print(f"📊 成长值: {value}")
                print(f"💎 会员: {vip_status}")

                return user, level, value, vip_status
            else:
                self.add_message(f"⚠️ 获取用户信息失败，状态码: {resp.status_code}")
                return "未知用户", "未知", "未知", "未知"
        except Exception as e:
            self.add_message(f"⚠️ 用户信息请求异常: {e}")
            return "未知用户", "未知", "未知", "未知"

    def main(self):
        """主执行函数"""
        print(f"\n==== 百度网盘账号{self.index} 开始签到 ====")
        
        if not self.cookie.strip():
            error_msg = """Cookie配置错误

❌ 错误原因: 未找到BAIDU_COOKIE环境变量

🔧 解决方法:
1. 打开百度网盘网页版: https://pan.baidu.com/
2. 登录您的账号
3. 按F12打开开发者工具
4. 切换到Network标签页，刷新页面
5. 找到任意请求的Request Headers
6. 复制完整的Cookie值
7. 在青龙面板中添加环境变量BAIDU_COOKIE
"""
            
            print(f"❌ {error_msg}")
            notification_title, html_content, plain_content = (
                build_configuration_error_notification("未配置 BAIDU_COOKIE 环境变量")
            )
            return (
                notification_title,
                html_content,
                plain_content,
                False,
                "未知用户",
            )

        # 1. 执行签到
        signin_success, signin_msg = self.signin()
        
        # 2. 随机等待
        time.sleep(random.uniform(2, 5))
        
        # 3. 获取并回答每日问题
        answer_success = False
        answer_msg = ""
        answer, ask_id = self.get_daily_question()
        if answer and ask_id:
            answer_success, answer_msg = self.answer_question(answer, ask_id)
        
        # 4. 获取用户信息
        user, level, value, vip_status = self.get_user_info()
        
        # 签到或答题任一成功都算成功
        is_success = signin_success or answer_success
        notification_title, html_content, plain_content = build_account_notification(
            self.index,
            user,
            level,
            value,
            vip_status,
            signin_msg,
            answer_msg,
            is_success,
        )
        print(f"{'✅ 任务完成' if is_success else '❌ 任务失败'}")
        return (
            notification_title,
            html_content,
            plain_content,
            is_success,
            user,
        )

def main():
    """主程序入口"""
    global task_timeout

    print(f"==== 百度网盘签到开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    runtime_settings = load_task_runtime_settings()
    task_timeout = runtime_settings.request_timeout_seconds

    # 显示配置状态
    print(f"🔒 隐私保护模式: {'已启用' if privacy_mode else '已禁用'}")

    # 获取Cookie配置
    baidu_cookies = BAIDU_COOKIE
    
    if not baidu_cookies:
        error_msg = "未配置 BAIDU_COOKIE 环境变量"
        print(error_msg)
        notification_title, html_content, plain_content = (
            build_configuration_error_notification(error_msg)
        )
        notify_user(notification_title, html_content, plain_content)
        return

    # 支持多账号（用换行分隔）
    if '\n' in baidu_cookies:
        cookies = [cookie.strip() for cookie in baidu_cookies.split('\n') if cookie.strip()]
    else:
        cookies = [baidu_cookies.strip()]

    apply_startup_random_delay(
        "百度网盘签到",
        runtime_settings,
        has_work=bool(cookies),
    )
    
    print(f"📝 共发现 {len(cookies)} 个账号")
    
    success_count = 0
    total_count = len(cookies)
    results = []
    
    for index, cookie in enumerate(cookies):
        try:
            # 账号间等待
            if index > 0:
                wait_between_accounts(
                    index,
                    total_count,
                    runtime_settings.account_delay_seconds,
                )
            
            # 执行签到
            baidu_pan = BaiduPan(cookie, index + 1)
            (
                notification_title,
                html_content,
                plain_content,
                is_success,
                account_label,
            ) = baidu_pan.main()
            
            if is_success:
                success_count += 1
            
            results.append({
                'index': index + 1,
                'success': is_success,
                'user': account_label,
            })

            notify_user(notification_title, html_content, plain_content)
            
        except Exception as e:
            error_msg = f"账号{index + 1}: 执行异常 - {str(e)}"
            print(f"❌ {error_msg}")
            notification_title, html_content, plain_content = (
                build_configuration_error_notification(error_msg)
            )
            notify_user(notification_title, html_content, plain_content)
            results.append({
                'index': index + 1,
                'success': False,
                'user': "未知用户",
            })
    
    # 发送汇总通知
    if total_count > 1:
        summary_title, summary_html, summary_plain = build_summary_notification(results)
        notify_user(summary_title, summary_html, summary_plain)
    
    print(f"\n==== 百度网盘签到完成 - 成功{success_count}/{total_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

def handler(event, context):
    """云函数入口"""
    main()

if __name__ == "__main__":
    main()
