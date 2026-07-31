"""通用 OCR 识别模块，供各签到/登录任务调用。

当前实现：讯飞图像理解 xoppaddleocrv16。
环境变量：OCR_KEY（讯飞 APIKey），所有任务共用。

使用方式：
  from comm.ocr_client import recognize_text, recognize_captcha
"""

from __future__ import annotations

import base64
import json
import os
import re

import requests

XFYUN_BASE_URL = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
XFYUN_MODEL_ID = "xoppaddleocrv16"

# 验证码识别结果只保留英数字符
_CAPTCHA_CHARSET_PATTERN = re.compile(r"[a-zA-Z0-9]")


def _get_ocr_key() -> str:
    return os.environ.get("OCR_KEY", "").strip()


def has_ocr_key() -> bool:
    """是否已配置 OCR_KEY（供调用方做快速失败守卫）。"""
    return bool(_get_ocr_key())


def _detect_image_mime(image_bytes: bytes) -> str:
    """根据 magic bytes 推断图片 MIME 类型，默认 jpeg。"""
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def _call_xfyun(
    api_key: str,
    image_bytes: bytes,
    prompt: str,
    timeout_seconds: float,
) -> str | None:
    """调用讯飞图像理解模型，返回识别到的原始文本。

    temperature 固定为 0，确保同一张图多次调用输出一致，
    避免 1/l、0/O 这类易混字符因采样随机性偶发误识别。
    """
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    image_mime = _detect_image_mime(image_bytes)
    payload = {
        "model": XFYUN_MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_mime};base64,{image_base64}",
                        },
                    },
                ],
            },
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 4096,
    }
    try:
        response = requests.post(
            f"{XFYUN_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
        response_data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"[OCR] 讯飞识别请求失败：{type(error).__name__}")
        return None

    if response.status_code != 200:
        error_detail = json.dumps(response_data, ensure_ascii=False)[:300]
        print(f"[OCR] 讯飞识别失败：HTTP {response.status_code} {error_detail}")
        return None

    choices = response_data.get("choices") or []
    if not choices:
        print("[OCR] 讯飞识别响应缺少 choices")
        return None

    recognized_text = (
        (choices[0].get("message") or {}).get("content", "")
    ).strip()
    return recognized_text or None


# --- 以下为 EasyOCR 存档代码，已被讯飞方案替代，不再启用 ---
#
# EASYOCR_API_URL = "https://console.easyocr.org/api/ocr"
#
# def _call_easyocr(
#     api_key: str,
#     image_bytes: bytes,
#     image_filename: str,
#     timeout_seconds: float,
# ) -> str | None:
#     try:
#         response = requests.post(
#             EASYOCR_API_URL,
#             headers={"X-Access-Key": api_key},
#             files={"file": (image_filename, image_bytes)},
#             timeout=timeout_seconds,
#         )
#         response_data = response.json()
#     except (requests.RequestException, ValueError) as error:
#         print(f"[OCR] EasyOCR 识别请求失败：{type(error).__name__}")
#         return None
#
#     if response.status_code != 200:
#         print(f"[OCR] EasyOCR 识别失败：HTTP {response.status_code} {response_data}")
#         return None
#
#     words = response_data.get("words") or []
#     recognized_text = "".join(
#         str(word.get("text", ""))
#         for word in words
#         if isinstance(word, dict)
#     )
#     return recognized_text or None


def recognize_text(
    image_bytes: bytes,
    prompt: str,
    timeout_seconds: float,
) -> str | None:
    """通用文字识别：按自定义 prompt 识别图片中的文字，返回原始文本。"""
    ocr_key = _get_ocr_key()
    if not ocr_key:
        print("[OCR] 未配置 OCR_KEY，无法识别")
        return None
    return _call_xfyun(ocr_key, image_bytes, prompt, timeout_seconds)


def recognize_captcha(
    image_bytes: bytes,
    timeout_seconds: float,
) -> str | None:
    """验证码专用识别：返回提取后的英数字符串。"""
    ocr_key = _get_ocr_key()
    if not ocr_key:
        print("[OCR] 未配置 OCR_KEY，无法识别验证码")
        return None

    prompt = (
        "这是一张登录验证码图片，请识别图中的验证码字符，"
        "只输出图中的字母和数字序列，不要输出其他任何内容。"
    )
    recognized_text = _call_xfyun(ocr_key, image_bytes, prompt, timeout_seconds)
    if recognized_text is None:
        return None

    captcha_text = "".join(
        _CAPTCHA_CHARSET_PATTERN.findall(recognized_text),
    )
    if not captcha_text:
        print(f"[OCR] 讯飞识别结果中无有效验证码：{recognized_text!r}")
        return None

    print(f"[OCR] 讯飞已识别到 {len(captcha_text)} 位验证码")
    return captcha_text
