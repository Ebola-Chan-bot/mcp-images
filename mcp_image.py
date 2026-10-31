#!/usr/bin/env python3

import os
import sys
import re
import json
import asyncio
import time
import shutil
import socket
import argparse
import tempfile
import subprocess
import importlib.util
import threading
import urllib.request
import httpx
import logging
from io import BytesIO
from datetime import datetime
from contextlib import contextmanager
from PIL import Image as PILImage
from pathlib import Path
from urllib.parse import urlparse
# 修复 mcp 2.0 移除 mcp.server.fastmcp 导致的 ModuleNotFoundError；迁移到新的 mcpserver 高层 API。
from mcp.server.mcpserver import MCPServer, Image, Context
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree

_当前文件目录 = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(_当前文件目录, "Temp")
DATA_DIR = os.path.join(_当前文件目录, "data")
CDP渲染脚本路径 = os.path.join(os.path.dirname(__file__), "SVG转PNG渲染器.py")

# 持久化 headless Edge 进程，避免每次 SVG 渲染都启动/销毁浏览器
_持久Edge进程: Optional[subprocess.Popen] = None
_持久Edge端口: int = 0
_持久Edge配置: str = ""
_渲染器模块: Any = None
_Edge启动锁 = threading.Lock()


def _加载渲染器模块() -> Any:
    """懒加载 SVG转PNG渲染器 模块，供 CDP 渲染直接调用。"""
    global _渲染器模块
    if _渲染器模块 is None:
        spec = importlib.util.spec_from_file_location("svg_renderer", CDP渲染脚本路径)
        _渲染器模块 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_渲染器模块)
    return _渲染器模块


def _获取或启动持久Edge() -> tuple[int, str]:
    """返回 (调试端口, 配置目录)。跨进程复用：通过锁文件记录端口并探测存活性。
    若锁文件不可用则重启 Edge 到同一持久 profile。"""
    global _持久Edge进程, _持久Edge端口, _持久Edge配置

    if not _持久Edge配置:
        _持久Edge配置 = os.path.abspath(os.path.join(DATA_DIR, "edge-profile"))

    os.makedirs(_持久Edge配置, exist_ok=True)
    锁文件 = os.path.join(_持久Edge配置, ".mcp.lock")

    # 内存中已有有效端口 → 直接复用（同进程内最快路径）
    if _持久Edge端口:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{_持久Edge端口}/json/version", timeout=2
            ) as resp:
                if resp.status == 200:
                    return _持久Edge端口, _持久Edge配置
        except Exception:
            _持久Edge端口 = 0

    # 从锁文件恢复（跨进程复用）
    if os.path.isfile(锁文件):
        try:
            with open(锁文件, "r") as fh:
                锁数据 = json.loads(fh.read())
            恢复端口 = int(锁数据["端口"])
            with urllib.request.urlopen(
                f"http://127.0.0.1:{恢复端口}/json/version", timeout=2
            ) as resp:
                if resp.status == 200:
                    _持久Edge端口 = 恢复端口
                    return 恢复端口, _持久Edge配置
        except Exception:
            pass

    # 不可复用 → 加锁后重新探测（避免并发时多个线程同时重建）
    with _Edge启动锁:
        # 再次探测：可能已被其他线程启动
        if _持久Edge端口:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{_持久Edge端口}/json/version", timeout=2
                ) as resp:
                    if resp.status == 200:
                        return _持久Edge端口, _持久Edge配置
            except Exception:
                _持久Edge端口 = 0
        if os.path.isfile(锁文件):
            try:
                with open(锁文件, "r") as fh:
                    锁数据 = json.loads(fh.read())
                恢复端口 = int(锁数据["端口"])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{恢复端口}/json/version", timeout=2
                ) as resp:
                    if resp.status == 200:
                        _持久Edge端口 = 恢复端口
                        return 恢复端口, _持久Edge配置
            except Exception:
                pass

        # 删锁文件（旧进程已不可用），启动新 Edge
        try:
            os.remove(锁文件)
        except Exception:
            pass

        svg = _加载渲染器模块()
        浏览器 = 解析SVG浏览器路径()
        _持久Edge端口 = _获取空闲TCP端口()
        启动参数 = svg.构建浏览器启动参数()
        命令 = svg.构建Edge命令(浏览器, _持久Edge端口, _持久Edge配置)

        _持久Edge进程 = subprocess.Popen(命令, **启动参数)
        svg.等待JSON(f"http://127.0.0.1:{_持久Edge端口}/json/version", timeout_ms=20000)

        try:
            with open(锁文件, "w") as fh:
                json.dump({"端口": _持久Edge端口}, fh)
        except Exception:
            pass

    return _持久Edge端口, _持久Edge配置


# 修复启动参数与环境变量名称不一致的问题；优先使用与参数同名的环境变量，并兼容旧名称。
SVG浏览器环境变量 = ("浏览器路径", "MCP图片浏览器路径")
SVG表情字体环境变量 = ("表情字体路径", "MCP图片表情字体路径")
MCP表情字体族名 = "MCPImageEmojiOverride"
_启动浏览器路径: Optional[str] = None
_启动表情字体路径: Optional[str] = None

浏览器候选项 = {
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "msedge",
        "chrome",
        "chromium",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "microsoft-edge",
        "chromium",
    ],
    "linux": [
        "google-chrome",
        "microsoft-edge",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/microsoft-edge",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ],
}

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configure logging: first disable other loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
# 修复工具名含中文时 FastMCP 反复告警刷屏的问题；SEP-986 名称校验仅限 ASCII，中文工具名由 VS Code 侧清洗即可。
logging.getLogger("mcp.shared.tool_name_validation").setLevel(logging.ERROR)

# Configure our logger
log_filename = os.path.join(DATA_DIR, datetime.now().strftime("%d-%m-%y.log"))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create handlers
file_handler = logging.FileHandler(log_filename)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(formatter)

# Set up our logger
logger = logging.getLogger("image-mcp")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# Prevent double logging
logger.propagate = False

表情文本正则 = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u200D\uFE0F]+")

彩色表情字体候选项 = {
    "win32": [
        r"C:\Windows\Fonts\seguiemj.ttf",
    ],
    "darwin": [
        "/System/Library/Fonts/Apple Color Emoji.ttc",
    ],
    "linux": [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/local/share/fonts/NotoColorEmoji.ttf",
    ],
}


def _image_has_transparency(img: PILImage.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode == "P":
        return "transparency" in img.info
    return False


def _save_processed_image(img: PILImage.Image, has_transparency: bool, quality: int) -> tuple[bytes, str]:
    img_byte_arr = BytesIO()
    # 修复大图压缩时透明背景被强制转成 JPEG 导致 alpha 丢失的问题。
    if has_transparency:
        png_img = img if img.mode in ("RGBA", "LA") else img.convert("RGBA")
        png_img.save(img_byte_arr, format="PNG", optimize=True, compress_level=9)
        return img_byte_arr.getvalue(), "png"

    rgb_img = img if img.mode == "RGB" else img.convert("RGB")
    rgb_img.save(img_byte_arr, format="JPEG", quality=quality, optimize=True)
    return img_byte_arr.getvalue(), "jpeg"


def _规范裁剪框(裁剪框: Optional[List[int]], 图像宽度: int, 图像高度: int) -> Optional[tuple]:
    """校验并规范化裁剪框 [x, y, width, height] 到 (x, y, w, h)。

    超出图像边缘的部分自动裁剪到边界；裁剪框 为 None 时返回 None；框非法（长度不为 4、宽高非正、起点越界）时抛 ValueError。
    """
    if 裁剪框 is None:
        return None
    if len(裁剪框) != 4:
        raise ValueError(f"裁剪框 必须是 [x, y, width, height] 四个整数，收到 {裁剪框}")
    try:
        x, y, w, h = (int(v) for v in 裁剪框)
    except (TypeError, ValueError) as 转换异常:
        raise ValueError(f"裁剪框 必须是四个整数，收到 {裁剪框}") from 转换异常
    if w <= 0 or h <= 0:
        raise ValueError(f"裁剪框 的宽高必须为正数，收到 width={w}, height={h}")
    if x >= 图像宽度 or y >= 图像高度:
        raise ValueError(f"裁剪框 起点 ({x}, {y}) 超出图像尺寸 {图像宽度}x{图像高度}")
    x = max(0, x)
    y = max(0, y)
    w = min(w, 图像宽度 - x)
    h = min(h, 图像高度 - y)
    return (x, y, w, h)


def _裁剪图像字节(图像数据: bytes, 裁剪框: List[int], 来源格式: Optional[str] = None) -> tuple:
    """按归一化后的裁剪框裁剪位图字节流并重新编码。返回 (字节数据, 格式名)。

    超出边缘的部分自动 clamp；非法 裁剪框 抛 ValueError（由调用方转成每源错误）。
    """
    with PILImage.open(BytesIO(图像数据)) as 图像:
        图像.load()
        宽度, 高度 = 图像.size
        归一化框 = _规范裁剪框(裁剪框, 宽度, 高度)
        if 归一化框 is None:
            return 图像数据, 来源格式
        # _规范裁剪框返回 (x, y, w, h)，PIL.crop 需要 (left, upper, right, lower)。
        x, y, w, h = 归一化框
        PIL裁剪框 = (x, y, x + w, y + h)
        原始格式 = (图像.format or "png").lower()
        输出格式 = (来源格式 or 原始格式).lower()
        if 输出格式.startswith("image/"):
            输出格式 = 输出格式.split("/")[-1]
        裁剪后图 = 图像.crop(PIL裁剪框)
        字节流 = BytesIO()
        if 输出格式 in ("jpeg", "jpg"):
            # JPEG 不支持 alpha，裁剪后统一转 RGB。
            (裁剪后图 if 裁剪后图.mode == "RGB" else 裁剪后图.convert("RGB")).save(
                字节流, format="JPEG", quality=90, optimize=True)
            输出格式 = "jpeg"
        elif 输出格式 == "gif":
            裁剪后图.save(字节流, format="GIF")
        elif 输出格式 == "webp":
            裁剪后图.save(字节流, format="WEBP", quality=90)
        elif 输出格式 == "bmp":
            裁剪后图.save(字节流, format="BMP")
        elif 输出格式 in ("tiff", "tif"):
            裁剪后图.save(字节流, format="TIFF")
            输出格式 = "tiff"
        else:
            裁剪后图.save(字节流, format="PNG")
            输出格式 = "png"
        return 字节流.getvalue(), 输出格式


def _浮点转属性字符串(数值: float) -> str:
    文本 = f"{数值:.4f}".rstrip("0").rstrip(".")
    return 文本 if 文本 else "0"


def _按裁剪框重写SVG(SVG文本: str, 裁剪框: tuple, 完整渲染宽度: int, 完整渲染高度: int) -> Optional[str]:
    """矢量级裁剪：把根 <svg> 标签的 viewBox 按完整渲染尺寸重映射，并将 width/height 改为裁剪框像素。

    裁剪框 是已规范化的 (x, y, w, h)，坐标属于“按 svg_dpi 渲染完整图像后的像素空间”。重写后渲染器只会以全 DPI 渲染裁剪区域，细节无损。
    仅在根标签能解析出 viewBox 时生效，否则返回 None（调用方回退到渲染后位图裁剪）。
    只用正则重写根标签而不用 ElementTree 整树序列化，避免破坏 xlink: 等命名空间前缀。
    """
    根标签匹配 = re.search(r"<svg\b[^>]*>", SVG文本)
    if not 根标签匹配:
        return None
    根标签 = 根标签匹配.group(0)
    if 根标签.endswith("/>"):  # 自闭合根标签不符合 SVG 结构，防御性放弃
        return None
    视图框匹配 = re.search(
        r"""viewBox\s*=\s*["']\s*([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)""",
        根标签,
    )
    if not 视图框匹配:
        return None
    try:
        vb_x, vb_y, vb_w, vb_h = (float(g) for g in 视图框匹配.groups())
    except ValueError:
        return None
    if vb_w <= 0 or vb_h <= 0 or 完整渲染宽度 <= 0 or 完整渲染高度 <= 0:
        return None

    x, y, w, h = 裁剪框
    横向比例 = vb_w / 完整渲染宽度
    纵向比例 = vb_h / 完整渲染高度
    新视图框 = " ".join(
        _浮点转属性字符串(v)
        for v in (vb_x + x * 横向比例, vb_y + y * 纵向比例, w * 横向比例, h * 纵向比例)
    )

    def _替换属性(标签: str, 属性名: str, 属性值: str) -> str:
        模式 = rf'(\s{属性名}\s*=\s*)(["\'])[^"\']*\2'
        if re.search(模式, 标签):
            return re.sub(模式, lambda m: m.group(1) + m.group(2) + 属性值 + m.group(2), 标签, count=1)
        return 标签[:-1] + f' {属性名}="{属性值}">'

    新标签 = _替换属性(根标签, "viewBox", 新视图框)
    新标签 = _替换属性(新标签, "width", str(w))
    新标签 = _替换属性(新标签, "height", str(h))
    return SVG文本[: 根标签匹配.start()] + 新标签 + SVG文本[根标签匹配.end():]


def _解析SVG长度(长度文本: Optional[str], dpi: float) -> Optional[int]:
    if not 长度文本:
        return None

    匹配结果 = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z%]*)\s*", 长度文本)
    if not 匹配结果:
        return None

    数值 = float(匹配结果.group(1))
    单位 = 匹配结果.group(2).lower() or "px"
    if 单位 == "px":
        return max(1, int(round(数值)))
    if 单位 == "in":
        return max(1, int(round(数值 * dpi)))
    if 单位 == "cm":
        return max(1, int(round(数值 * dpi / 2.54)))
    if 单位 == "mm":
        return max(1, int(round(数值 * dpi / 25.4)))
    if 单位 == "pt":
        return max(1, int(round(数值 * dpi / 72.0)))
    if 单位 == "pc":
        return max(1, int(round(数值 * dpi / 6.0)))
    return None


def _查找现存字体路径(候选路径列表: List[str]) -> Optional[str]:
    for 候选路径 in 候选路径列表:
        if 候选路径 and os.path.isfile(候选路径):
            return 候选路径
    return None


def _规范化可选路径(原始值: Optional[str]) -> Optional[str]:
    if 原始值 is None:
        return None
    规范化结果 = 原始值.strip().strip('"')
    return 规范化结果 or None


def _解析现存可执行文件(路径或名称: Optional[str]) -> Optional[str]:
    候选项 = _规范化可选路径(路径或名称)
    if not 候选项:
        return None

    展开路径 = os.path.expanduser(候选项)
    if os.path.isabs(展开路径) or os.sep in 展开路径 or (os.altsep and os.altsep in 展开路径):
        return 展开路径 if os.path.isfile(展开路径) else None

    已解析路径 = shutil.which(展开路径)
    return 已解析路径 or None


def _遍历默认浏览器候选项() -> List[str]:
    return 浏览器候选项.get(sys.platform, 浏览器候选项.get("linux", []))


def 解析SVG浏览器路径(浏览器路径: Optional[str] = None) -> str:
    请求路径 = _规范化可选路径(浏览器路径)
    if 请求路径:
        已解析路径 = _解析现存可执行文件(请求路径)
        if 已解析路径:
            return 已解析路径
        raise FileNotFoundError(f"配置的 SVG 浏览器路径不存在: {请求路径}")

    for 候选项 in (_启动浏览器路径, *[os.getenv(name) for name in SVG浏览器环境变量]):
        规范化结果 = _规范化可选路径(候选项)
        if not 规范化结果:
            continue
        已解析路径 = _解析现存可执行文件(规范化结果)
        if 已解析路径:
            return 已解析路径
        raise FileNotFoundError(f"配置的 SVG 浏览器路径不存在: {规范化结果}")

    已检查项: List[str] = []
    for 候选项 in _遍历默认浏览器候选项():
        已检查项.append(候选项)
        已解析路径 = _解析现存可执行文件(候选项)
        if 已解析路径:
            return 已解析路径

    raise FileNotFoundError(
        "未找到可用的 Chromium 浏览器。已检查: " + ", ".join(已检查项)
    )


def 解析表情字体路径(表情字体路径: Optional[str] = None) -> str:
    请求路径 = _规范化可选路径(表情字体路径)
    if 请求路径:
        展开路径 = os.path.expanduser(请求路径)
        if os.path.isfile(展开路径):
            return 展开路径
        raise FileNotFoundError(f"配置的 emoji 字体路径不存在: {请求路径}")

    for 候选项 in (_启动表情字体路径, *[os.getenv(name) for name in SVG表情字体环境变量]):
        规范化结果 = _规范化可选路径(候选项)
        if not 规范化结果:
            continue
        展开路径 = os.path.expanduser(规范化结果)
        if os.path.isfile(展开路径):
            return 展开路径
        raise FileNotFoundError(f"配置的 emoji 字体路径不存在: {规范化结果}")

    已检查项 = 彩色表情字体候选项.get(sys.platform, [])
    已解析路径 = _查找现存字体路径(已检查项)
    if 已解析路径:
        return 已解析路径

    raise FileNotFoundError(
        "未找到默认彩色 emoji 字体。已检查: " + ", ".join(已检查项)
    )


def _获取SVG目标尺寸(svg_data: bytes, dpi: int) -> tuple[int, int]:
    try:
        根元素 = ElementTree.fromstring(svg_data)
        宽度 = _解析SVG长度(根元素.attrib.get("width"), dpi)
        高度 = _解析SVG长度(根元素.attrib.get("height"), dpi)
        if 宽度 and 高度:
            return 宽度, 高度

        视图框 = 根元素.attrib.get("viewBox")
        if 视图框:
            片段 = re.split(r"[\s,]+", 视图框.strip())
            if len(片段) == 4:
                视图框宽度 = max(1, int(round(float(片段[2]) * dpi / 96.0)))
                视图框高度 = max(1, int(round(float(片段[3]) * dpi / 96.0)))
                return 视图框宽度, 视图框高度
    except Exception:
        pass

    return 300, 150


def _获取空闲TCP端口() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as 套接字:
        套接字.bind(("127.0.0.1", 0))
        return int(套接字.getsockname()[1])


def _去掉SVG文档前导(svg_text: str) -> str:
    去掉XML声明后的文本 = re.sub(r"^\s*<\?xml[^>]*>\s*", "", svg_text, count=1)
    return re.sub(r"^\s*<!DOCTYPE[^>]*>\s*", "", 去掉XML声明后的文本, count=1, flags=re.IGNORECASE)


def _向SVG注入表情字体(svg_text: str, emoji_font_family: str) -> str:
    优先字体 = f"'{emoji_font_family}'"

    def 重写文本节点(匹配结果: re.Match[str]) -> str:
        属性文本 = 匹配结果.group(1)
        节点内容 = 匹配结果.group(2)
        if not 表情文本正则.search(节点内容):
            return 匹配结果.group(0)

        def 前置属性字体(属性匹配: re.Match[str]) -> str:
            引号 = 属性匹配.group(1)
            原始值 = 属性匹配.group(2)
            return f"font-family={引号}{优先字体}, {原始值}{引号}"

        def 前置样式字体(样式匹配: re.Match[str]) -> str:
            return f"font-family: {优先字体}, {样式匹配.group(1)}"

        更新后的属性文本 = re.sub(
            r"font-family\s*=\s*([\"'])(.*?)\1",
            前置属性字体,
            属性文本,
            count=1,
        )
        更新后的属性文本 = re.sub(
            r"font-family:\s*([^;\"'\>\<\}]+)",
            前置样式字体,
            更新后的属性文本,
            count=1,
        )
        if 更新后的属性文本 == 属性文本:
            更新后的属性文本 = f'{属性文本} font-family="{优先字体}"'

        return f"<text{更新后的属性文本}>{节点内容}</text>"

    return re.sub(r"<text([^>]*)>(.*?)</text>", 重写文本节点, svg_text, flags=re.DOTALL)


def _构建SVG包装HTML(svg_text: str, width: int, height: int, 表情字体路径: str) -> str:
    字体路径URI = Path(表情字体路径).resolve().as_uri()
    清理后的SVG = _向SVG注入表情字体(_去掉SVG文档前导(svg_text), MCP表情字体族名)
    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <style>
    @font-face {{
      font-family: '{MCP表情字体族名}';
      src: url('{字体路径URI}');
    }}

    html, body {{
      margin: 0;
      padding: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
            /* 修复 CDP 截图时包装页白底覆盖 SVG 透明区域的问题。 */
            background: transparent;
    }}

    svg {{
      display: block;
      width: {width}px;
      height: {height}px;
      overflow: visible;
    }}
  </style>
</head>
<body>{清理后的SVG}</body>
</html>
"""


def 通过CDP渲染SVG到PNG(
    svg_data: bytes,
    svg_dpi: int,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
) -> bytes:
    浏览器可执行文件 = 解析SVG浏览器路径(浏览器路径)
    表情字体文件 = 解析表情字体路径(表情字体路径)
    if not os.path.isfile(CDP渲染脚本路径):
        raise FileNotFoundError(f"未找到 CDP 渲染脚本: {CDP渲染脚本路径}")

    目标宽度, 目标高度 = _获取SVG目标尺寸(svg_data, svg_dpi)
    端口, 配置目录 = _获取或启动持久Edge()

    with tempfile.TemporaryDirectory(prefix="svg-cdp-", dir=TEMP_DIR) as 工作目录:
        包装页路径 = os.path.join(工作目录, "wrapper.html")
        SVG文本 = svg_data.decode("utf-8", errors="replace")
        HTML文本 = _构建SVG包装HTML(SVG文本, 目标宽度, 目标高度, 表情字体文件)
        with open(包装页路径, "w", encoding="utf-8") as handle:
            handle.write(HTML文本)

        url = Path(包装页路径).resolve().as_uri()
        svg = _加载渲染器模块()
        png_data = svg.内联CDP渲染(浏览器可执行文件, url, 目标宽度, 目标高度, 端口, 配置目录)
        return png_data


def _解析启动参数(argv: Optional[List[str]] = None) -> List[str]:
    参数解析器 = argparse.ArgumentParser(add_help=False)
    参数解析器.add_argument("--浏览器路径", dest="浏览器路径")
    参数解析器.add_argument("--表情字体路径", dest="表情字体路径")
    启动参数, 剩余参数 = 参数解析器.parse_known_args(argv)

    global _启动浏览器路径, _启动表情字体路径
    # 修复服务进程无法从启动参数接收浏览器路径的问题。
    _启动浏览器路径 = _规范化可选路径(启动参数.浏览器路径)
    # 修复服务进程无法从启动参数接收 emoji 字体路径的问题。
    _启动表情字体路径 = _规范化可选路径(启动参数.表情字体路径)
    return 剩余参数


async def _处理SVG字节(
    svg_data: bytes,
    image_source: str,
    ctx: Context,
    svg_dpi: int,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
    裁剪框: Optional[List[int]] = None,
) -> Dict[str, Any]:
    try:
        svg_dpi = max(50, min(svg_dpi, 1200))
        logger.debug("检测到 SVG 图片: %s，按 %s DPI 通过 CDP 渲染", image_source, svg_dpi)
        渲染数据 = svg_data
        渲染后裁剪框 = 裁剪框
        if 裁剪框 is not None:
            # 裁剪框坐标属于“按 svg_dpi 渲染完整 SVG 后的像素空间”，与 _获取SVG目标尺寸一致。
            完整宽度, 完整高度 = _获取SVG目标尺寸(svg_data, svg_dpi)
            归一化框 = _规范裁剪框(裁剪框, 完整宽度, 完整高度)
            重写后SVG = _按裁剪框重写SVG(
                svg_data.decode("utf-8", errors="replace"), 归一化框, 完整宽度, 完整高度
            )
            if 重写后SVG is not None:
                # 矢量级裁剪：渲染器以全 DPI 只渲染裁剪区域，细节无损。
                渲染数据 = 重写后SVG.encode("utf-8")
                渲染后裁剪框 = None
                logger.debug("SVG 矢量级裁剪: box=%s，完整渲染尺寸 %dx%d", 归一化框, 完整宽度, 完整高度)
            else:
                # 无 viewBox 等情形回退：先渲染完整图，再做位图裁剪。
                logger.debug("SVG 缺少 viewBox，回退到渲染后位图裁剪: box=%s", 归一化框)
        png_data = await asyncio.to_thread(
            通过CDP渲染SVG到PNG,
            渲染数据,
            svg_dpi,
            浏览器路径,
            表情字体路径,
        )
        处理后图片 = await process_image_data(png_data, "png", image_source, ctx, 裁剪框=渲染后裁剪框)
        if 处理后图片 is None:
            return {"error": "处理 SVG 图片失败"}
        return {"image": 处理后图片}
    except Exception as exc:
        错误信息 = f"转换 SVG {image_source} 时出错: {str(exc)}"
        await ctx.error(错误信息)
        logger.exception(错误信息)
        return {"error": 错误信息}


# Create an MCP server instance（mcp 2.0 中 FastMCP 已更名为 MCPServer）
mcp = MCPServer("image-service")

async def process_image_data(
    data: bytes,
    content_type: str,
    image_source: str,
    ctx: Context,
    裁剪框: Optional[List[int]] = None,
) -> Image | None:
    """Process image data and return an MCP Image object."""
    try:
        if 裁剪框 is not None:
            try:
                # 先裁剪再进入压缩管线，避免裁剪区域被大图降质/缩放二次损伤。
                data, content_type = _裁剪图像字节(data, 裁剪框, content_type)
            except ValueError as 裁剪异常:
                # 非法裁剪框需把具体原因作为错误信息返回给对应源，不能被下面的通用 except 吞成 "Failed to process image"。
                raise ValueError(f"图像源 {image_source} 的裁剪框无效: {裁剪异常}")
            logger.debug(f"Cropped {image_source} with 裁剪框={裁剪框} -> {len(data)} bytes")

        # If image is not large, try to log dimensions without processing
        if len(data) <= 1048576:
            try:
                with PILImage.open(BytesIO(data)) as img:
                    width, height = img.size
                    logger.debug(f"Original image dimensions from {image_source}: {width}x{height}")
                    logger.debug(f"Image format from PIL: {img.format}, mode: {img.mode}")
            except Exception as e:
                logger.debug(f"Could not determine dimensions for {image_source}: {e}")
            
            # Ensure content_type is valid and doesn't include 'image/'
            if content_type.startswith('image/'):
                content_type = content_type.split('/')[-1]
            
            logger.debug(f"Creating Image object with format: {content_type}")
            return Image(data=data, format=content_type)

        # For large images, save to temp file and process
        temp_path = os.path.join(TEMP_DIR, f"temp_image_{hash(image_source)}." + content_type.split('/')[-1])
        with open(temp_path, "wb") as f:
            f.write(data)
        
        try:
            # First pass: get dimensions and basic info
            with PILImage.open(temp_path) as img:
                orig_width, orig_height = img.size
                orig_format = img.format
                orig_mode = img.mode
                has_transparency = _image_has_transparency(img)
                logger.debug(f"Original image dimensions from {image_source}: {orig_width}x{orig_height}")
                logger.debug(f"Large image format from PIL: {orig_format}, mode: {orig_mode}")
            
            # Calculate optimal resize factor if image is very large
            max_dimension = max(orig_width, orig_height)
            initial_scale = 1.0
            if max_dimension > 3000:
                initial_scale = 3000 / max_dimension
                logger.debug(f"Very large image detected ({max_dimension}px), will start with scale factor: {initial_scale}")
            
            # Second pass: process the image
            with PILImage.open(temp_path) as img:
                if not has_transparency and img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                # Apply initial scale if needed
                if initial_scale < 1.0:
                    width = int(orig_width * initial_scale)
                    height = int(orig_height * initial_scale)
                    img = img.resize((width, height), PILImage.LANCZOS)
                else:
                    width, height = img.size
                
                quality = 85
                scale_factor = 1.0
                
                while True:
                    # Create a copy for this iteration to avoid accumulating transforms
                    if scale_factor < 1.0:
                        current_width = int(width * scale_factor)
                        current_height = int(height * scale_factor)
                        current_img = img.resize((current_width, current_height), PILImage.LANCZOS)
                    else:
                        current_img = img
                        current_width, current_height = width, height
                    
                    processed_data, output_format = _save_processed_image(current_img, has_transparency, quality)
                    
                    # Clean up the temporary image if we created one
                    if scale_factor < 1.0 and hasattr(current_img, 'close'):
                        current_img.close()
                    
                    # Target 800KB to leave buffer for any MCP overhead
                    if len(processed_data) <= 819200:  # 800KB
                        logger.debug(f"Processed image dimensions from {image_source}: {current_width}x{current_height} (quality={quality})")
                        logger.debug(f"Returning processed image with format: {output_format}, size: {len(processed_data)} bytes")
                        return Image(data=processed_data, format=output_format)
                    
                    # 修复透明 PNG 被误走 JPEG 降质分支的问题；透明图只能继续缩放，不能靠丢 alpha 压缩。
                    if not has_transparency and quality > 20:
                        quality -= 10
                        logger.debug(f"Reducing quality to {quality} for {image_source}, current size: {len(processed_data)} bytes")
                    else:
                        # Then try scaling down
                        scale_factor *= 0.8
                        if current_width * scale_factor < 200 or current_height * scale_factor < 200:
                            await ctx.error("Unable to compress image to acceptable size while maintaining quality")
                            logger.error(f"Failed processing image from {image_source}: dimensions too small")
                            return None
                        logger.debug(f"Applying scale factor {scale_factor} to image from {image_source}")
                        quality = 85  # Reset quality when changing size
        except MemoryError as e:
            await ctx.error(f"Out of memory processing large image: {str(e)}")
            logger.error(f"MemoryError processing image from {image_source}: {str(e)}")
            return None
        except Exception as e:
            await ctx.error(f"Image processing error: {str(e)}")
            logger.exception(f"Exception processing image from {image_source}")
            return None
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except ValueError:
        # 非法裁剪框：重新抛出让 process_local_image/fetch_single_image 的每源错误分支携带具体原因。
        raise
    except Exception as e:
        await ctx.error(f"Error processing image: {str(e)}")
        logger.exception(f"Unexpected error processing {image_source}")
        return None

async def process_local_image(
    file_path: str,
    ctx: Context,
    svg_dpi: int = 150,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
    裁剪框: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Processes a local image file and returns a dictionary with the result."""
    try:
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            await ctx.error(error_msg)
            logger.error(error_msg)
            return {"path": file_path, "error": error_msg}
        
        # Determine content type based on file extension
        _, ext = os.path.splitext(file_path)
        ext = ext[1:].lower() if ext else "jpeg"  # Default to jpeg if no extension
        
        # Handle SVG files by converting to PNG first
        if ext == "svg":
            try:
                with open(file_path, "rb") as f:
                    svg_data = f.read()
                SVG处理结果 = await _处理SVG字节(
                    svg_data,
                    file_path,
                    ctx,
                    svg_dpi,
                    浏览器路径=浏览器路径,
                    表情字体路径=表情字体路径,
                    裁剪框=裁剪框,
                )
                SVG处理结果["path"] = file_path
                return SVG处理结果
            except Exception as e:
                error_msg = f"转换 SVG {file_path} 时出错: {str(e)}"
                await ctx.error(error_msg)
                logger.exception(error_msg)
                return {"path": file_path, "error": error_msg}

        # Map extension to proper MIME type
        mime_type_map = {
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "png": "png",
            "gif": "gif",
            "bmp": "bmp",
            "webp": "webp",
            "tiff": "tiff",
            "tif": "tiff"
        }
        
        content_type = mime_type_map.get(ext, "jpeg")  # Default to jpeg if unknown extension
        logger.debug(f"Local image {file_path} has extension '{ext}', mapped to content type '{content_type}'")
        
        # For large files, read and process directly without loading entire file into memory
        file_size = os.path.getsize(file_path)
        if file_size > 1048576:
            logger.debug(f"Large local image detected: {file_path} ({file_size} bytes)")
            # Process the image directly using the same logic as for URL images
            return await process_large_local_image(file_path, content_type, ctx, 裁剪框=裁剪框)
        
        # For smaller files, read the entire content
        with open(file_path, "rb") as f:
            file_data = f.read()
        
        logger.debug(f"Read local image from {file_path} with {len(file_data)} bytes")
        processed_image = await process_image_data(file_data, content_type, file_path, ctx, 裁剪框=裁剪框)
        
        if processed_image is None:
            return {"path": file_path, "error": "Failed to process image"}
        
        return {"path": file_path, "image": processed_image}
        
    except Exception as e:
        error_msg = f"Error processing local image {file_path}: {str(e)}"
        await ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}

async def process_large_local_image(
    file_path: str,
    content_type: str,
    ctx: Context,
    裁剪框: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Process a large local image file directly without loading it entirely into memory."""
    temp_path = None
    try:
        # Create a temporary file path for processing
        temp_path = os.path.join(TEMP_DIR, f"temp_local_{os.path.basename(file_path)}")
        
        # First pass: get dimensions and basic info
        with PILImage.open(file_path) as img:
            orig_width, orig_height = img.size
            orig_format = img.format
            orig_mode = img.mode
            has_transparency = _image_has_transparency(img)
            logger.debug(f"Original large local image dimensions from {file_path}: {orig_width}x{orig_height}")
            logger.debug(f"Original image format: {orig_format}, mode: {orig_mode}")

        # 裁剪框基于原图完整尺寸校验；后续 initial_scale/压缩都作用在已裁剪的图上。
        归一化裁剪框 = _规范裁剪框(裁剪框, orig_width, orig_height)
        
        # Calculate optimal resize factor if image is very large
        max_dimension = max(orig_width, orig_height)
        initial_scale = 1.0
        if max_dimension > 4000:
            initial_scale = 4000 / max_dimension
            logger.debug(f"Very large image detected, will start with scale factor: {initial_scale}")
        
        # Second pass: process the image
        with PILImage.open(file_path) as img:
            if not has_transparency and img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # 先裁剪再缩放，避免大图先被 initial_scale 缩小导致裁剪区域细节丢失。
            if 归一化裁剪框 is not None:
                x, y, w, h = 归一化裁剪框
                img = img.crop((x, y, x + w, y + h))
                logger.debug(f"Cropped large local image {file_path} with 裁剪框={裁剪框} -> {img.size}")
            
            # Apply initial scale if needed
            if initial_scale < 1.0:
                width = int(img.size[0] * initial_scale)
                height = int(img.size[1] * initial_scale)
                img = img.resize((width, height), PILImage.LANCZOS)
            else:
                width, height = img.size
            
            quality = 75  # Start with lower quality for large images
            scale_factor = 1.0
            
            while True:
                # Create a copy for this iteration to avoid accumulating transforms
                if scale_factor < 1.0:
                    current_width = int(width * scale_factor)
                    current_height = int(height * scale_factor)
                    current_img = img.resize((current_width, current_height), PILImage.LANCZOS)
                else:
                    current_img = img
                    current_width, current_height = width, height
                
                processed_data, output_format = _save_processed_image(current_img, has_transparency, quality)
                
                # Clean up the temporary image if we created one
                if scale_factor < 1.0 and hasattr(current_img, 'close'):
                    current_img.close()
                
                # Target 800KB to leave buffer for any MCP overhead
                if len(processed_data) <= 819200:  # 800KB
                    logger.debug(f"Successfully compressed large local image {file_path} to {len(processed_data)} bytes (quality={quality}, dimensions={current_width}x{current_height})")
                    return {"path": file_path, "image": Image(data=processed_data, format=output_format)}
                
                # 修复本地透明图压缩时 alpha 被 JPEG 路径抹掉的问题。
                if not has_transparency and quality > 30:
                    quality -= 10
                    logger.debug(f"Reducing quality to {quality} for {file_path}")
                else:
                    # Then try scaling down
                    scale_factor *= 0.8
                    if current_width * scale_factor < 200 or current_height * scale_factor < 200:
                        error_msg = f"Unable to compress large local image {file_path} to acceptable size while maintaining quality"
                        await ctx.error(error_msg)
                        logger.error(error_msg)
                        return {"path": file_path, "error": error_msg}
                    
                    logger.debug(f"Applying scale factor {scale_factor} to image {file_path}")
                    quality = 85  # Reset quality when changing size
    
    except MemoryError as e:
        error_msg = f"Out of memory processing large local image {file_path}: {str(e)}"
        await ctx.error(error_msg)
        logger.error(error_msg)
        return {"path": file_path, "error": error_msg}
    except Exception as e:
        error_msg = f"Error processing large local image {file_path}: {str(e)}"
        await ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}
    
    finally:
        # Clean up temporary file if it exists
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

async def fetch_single_image(
    url: str,
    client: httpx.AsyncClient,
    ctx: Context,
    svg_dpi: int = 150,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
    裁剪框: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Fetches and processes a single image asynchronously."""
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme in ['http', 'https'], parsed.netloc]):
            error_msg = f"Invalid URL: {url}"
            await ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        response = await client.get(url)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            error_msg = f"Not an image (got {content_type})"
            await ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        logger.debug(f"Fetched image from {url} with {len(response.content)} bytes")
        logger.debug(f"Content-Type from server: {content_type}")

        if content_type.startswith("image/svg") or content_type.endswith("+xml"):
            SVG处理结果 = await _处理SVG字节(
                response.content,
                url,
                ctx,
                svg_dpi,
                浏览器路径=浏览器路径,
                表情字体路径=表情字体路径,
                裁剪框=裁剪框,
            )
            SVG处理结果["url"] = url
            return SVG处理结果
        
        # Extract the format from content-type
        format_type = content_type.split('/')[-1]
        logger.debug(f"Extracted format type: {format_type}")
        
        processed_image = await process_image_data(response.content, format_type, url, ctx, 裁剪框=裁剪框)
        
        if processed_image is None:
            return {"url": url, "error": "Failed to process image"}
        
        return {"url": url, "image": processed_image}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        await ctx.error(error_msg)
        return {"url": url, "error": error_msg}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        await ctx.error(error_msg)
        return {"url": url, "error": error_msg}

def is_url(path_or_url: str) -> bool:
    """Determine if the given string is a URL or a local file path."""
    parsed = urlparse(path_or_url)
    return bool(parsed.scheme and parsed.netloc)


def _输入包含SVG(image_sources: List[str]) -> bool:
    # 修复 fetch_images 将 svg_dpi 无条件声明为必填，导致非 SVG 输入也被错误约束的问题。
    for source in image_sources:
        candidate_path = urlparse(source).path if is_url(source) else source
        if candidate_path.lower().endswith(".svg"):
            return True
    return False

async def process_images_async(
    image_sources: List[str],
    ctx: Context,
    svg_dpi: int = 150,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
    裁剪框: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Process multiple images (URLs or local files) concurrently."""
    if not image_sources:
        raise ValueError("No image sources provided")
    
    # Separate URLs from local file paths
    urls = [src for src in image_sources if is_url(src)]
    local_paths = [src for src in image_sources if not is_url(src)]
    
    results = []
    
    # Process URLs if any
    if urls:
        logger.debug(f"Processing {len(urls)} URLs")
        async with httpx.AsyncClient() as client:
            url_tasks = [
                fetch_single_image(
                    url,
                    client,
                    ctx,
                    svg_dpi=svg_dpi,
                    浏览器路径=浏览器路径,
                    表情字体路径=表情字体路径,
                    裁剪框=裁剪框,
                )
                for url in urls
            ]
            url_results = await asyncio.gather(*url_tasks)
            results.extend(url_results)
    
    # Process local files if any
    if local_paths:
        logger.debug(f"Processing {len(local_paths)} local files")
        local_tasks = [
            process_local_image(
                path,
                ctx,
                svg_dpi=svg_dpi,
                浏览器路径=浏览器路径,
                表情字体路径=表情字体路径,
                裁剪框=裁剪框,
            )
            for path in local_paths
        ]
        local_results = await asyncio.gather(*local_tasks)
        results.extend(local_results)
    
    # Ensure results are in the same order as input sources
    ordered_results = []
    for src in image_sources:
        for result in results:
            if (src == result.get("url", None)) or (src == result.get("path", None)):
                ordered_results.append(result)
                break
    
    return ordered_results

@mcp.tool()
# Note: Do not add return type annotation `-> List[Image | str]` to this tool function.
# FastMCP uses Pydantic to inspect return types and generate JSON schemas for the MCP protocol.
# Since `Image` is an arbitrary type and does not have `__get_pydantic_core_schema__` implemented,
# adding a type hint like `List[Image]` will crash the server at startup with a PydanticSchemaGenerationError.
async def fetch_images(
    image_sources: List[str],
    ctx: Context,
    # 修复非 SVG 输入也被强制要求提供 svg_dpi 的问题；仅在输入包含 SVG 时才要求显式传入。
    svg_dpi: Optional[int] = None,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
    裁剪框: Optional[List[int]] = None,
):
    """
    Fetch and process images from URLs or local file paths, returning them in a format suitable for LLMs.
    
    This tool accepts a list of image sources which can be either:
    1. URLs pointing to web-hosted images (http:// or https://)
    2. Local file paths pointing to images stored on the local filesystem (e.g., "C:/images/photo1.jpg")
    
    For a single image, provide a one-element list. The function will process images in parallel
    when multiple sources are provided. Images that exceed the size limit (1MB) will be automatically 
    compressed while maintaining aspect ratio and reasonable quality.
    
    Args:
        image_sources: A list of image URLs or local file paths. For a single image, provide a one-element list.
        svg_dpi: Required only when image_sources contains SVG files. Higher values produce clearer images but larger files.
        浏览器路径: 可选。单次调用覆盖默认浏览器路径。
        表情字体路径: 可选。单次调用覆盖默认 emoji 字体路径。
        裁剪框: 可选，[x, y, width, height] 四个整数（像素），只返回该裁剪方框内的图像部分，对列表中所有图像源生效。坐标空间为完整解码后图像的像素空间：位图即解码后的图像像素；SVG 则是按 svg_dpi 渲染完整图像后的像素空间（例如 300 DPI 下 SVG 物理宽度 10㎜ 约对应 118 像素）。超出图像边缘的部分会自动裁剪到边缘；宽或高 ≤ 0、起点 (x, y) 超出图像范围时，对应源返回错误字符串。SVG 优先采用矢量级裁剪（重写 viewBox，只渲染裁剪区域），细节无损。
        
    Returns:
        A list in the same order as the input sources.
        Each item is either an Image object (success) or an error string (failure).
    """
    try:
        start_time = asyncio.get_event_loop().time()
        
        # Validate input
        if not image_sources:
            await ctx.error("No image sources provided")
            logger.error("fetch_images called with empty source list")
            return []

        if _输入包含SVG(image_sources):
            if svg_dpi is None:
                # 修复 SVG 输入缺少 svg_dpi 时继续带着空值向下游传递的问题。
                error_message = "svg_dpi is required when image_sources contains SVG files"
                await ctx.error(error_message)
                logger.error(error_message)
                return [error_message for _ in image_sources]
        else:
            svg_dpi = 150
        
        # Log the types of sources we're processing
        url_count = sum(1 for src in image_sources if is_url(src))
        local_count = len(image_sources) - url_count
        logger.debug(f"Processing {len(image_sources)} image sources: {url_count} URLs and {local_count} local files")
        
        # Process all images
        results = await process_images_async(
            image_sources,
            ctx,
            svg_dpi=svg_dpi,
            浏览器路径=浏览器路径,
            表情字体路径=表情字体路径,
            裁剪框=裁剪框,
        )
        
        # Extract Image objects, or explicit error messages for failures
        image_results = []
        for result in results:
            if "image" in result:
                image_results.append(result["image"])
            elif "error" in result:
                image_results.append(result["error"])
            else:
                source = result.get("path") or result.get("url") or "unknown source"
                error = result.get("error") or "Unknown error"
                error_message = f"Error processing {source}: {error}"
                await ctx.error(error_message)
                image_results.append(error_message)
        
        elapsed = asyncio.get_event_loop().time() - start_time
        success_count = sum(1 for r in image_results if isinstance(r, Image))
        
        logger.debug(
            f"Processed {len(image_sources)} images in {elapsed:.2f} seconds. "
            f"Success: {success_count}, Failed: {len(image_sources) - success_count}"
        )
        
        return image_results
    except Exception as e:
        logger.exception("Error in fetch_images")
        await ctx.error(f"Failed to process images: {str(e)}")
        return [f"Failed to process source: {src}. Error: {str(e)}" for src in image_sources]

# 多源竞速下载用的镜像源（实测 simple 索引与官方同步到 2.0）。按名称+根地址列出；各源并发查询、并发下载，首个完成的胜出，其余立即强制中止。
_PyMuPDF下载源: List[tuple] = [
    ("PyPI 官方", "https://pypi.org"),
    ("阿里云", "https://mirrors.aliyun.com/pypi"),
    ("中科大", "https://mirrors.ustc.edu.cn/pypi"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi"),
    ("华为云", "https://repo.huaweicloud.com/repository/pypi"),
    ("清华", "https://pypi.tuna.tsinghua.edu.cn"),
]


def _平台wheel标签() -> List[str]:
    """返回适配当前平台的 wheel platform_tag 候选列表（按优先级）。"""
    import platform as _platform
    机器 = _platform.machine().lower()
    if sys.platform == "win32":
        return ["win_amd64", "win32"] if 机器 in ("amd64", "x86_64") else ["win32"]
    if sys.platform == "darwin":
        return ["macosx_11_0_arm64", "macosx_10_9_x86_64"] if 机器 == "arm64" else ["macosx_10_9_x86_64"]
    return ["manylinux_2_28_x86_64", "musllinux_1_2_x86_64", "manylinux_2_28_aarch64"]


def _从文件列表选wheel(文件列表: List[dict]) -> Optional[dict]:
    """在 PyPI JSON 的 urls 列表里选出适配平台的 wheel，返回 {url,size,filename}。"""
    for 标签 in _平台wheel标签():
        for f in 文件列表:
            if f.get("packagetype") == "bdist_wheel" and 标签 in f.get("filename", ""):
                if f.get("size", 0) > 0 and f.get("url"):
                    return {"url": f["url"], "size": f["size"], "filename": f["filename"]}
    for f in 文件列表:
        if f.get("packagetype") == "bdist_wheel" and f.get("size", 0) > 0 and f.get("url"):
            return {"url": f["url"], "size": f["size"], "filename": f["filename"]}
    return None


def _查询单源wheel信息(源名: str, 根地址: str, 超时秒: float = 8) -> Optional[dict]:
    """查询单个源的适配 wheel 信息。优先 JSON 接口，失败则解析 simple 索引。

    返回 {源名, url, size, filename}；size 未知时为 0（竞速按下载完成判胜，不依赖预知大小）。
    """
    import json as _json
    import re as _re
    from urllib.parse import urljoin as _urljoin
    from urllib.request import Request as _Request, urlopen as _urlopen

    UA = {"User-Agent": "pip/24.0"}

    # 1) JSON 接口（含确切 size）
    try:
        with _urlopen(_Request(f"{根地址}/pypi/pymupdf/json", headers=UA), timeout=超时秒) as resp:
            数据 = _json.loads(resp.read().decode())
        选中 = _从文件列表选wheel(数据.get("urls", []) or 数据.get("info", {}).get("urls", []))
        if 选中:
            选中["源名"] = 源名
            return 选中
    except Exception:
        pass

    # 2) simple 索引（只有文件名，无 size）
    try:
        simple地址 = f"{根地址}/simple/pymupdf/"
        with _urlopen(_Request(simple地址, headers=UA), timeout=超时秒) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        链接们 = _re.findall(r'href="([^"]+)"', html)
        for 标签 in _平台wheel标签():
            匹配 = [u for u in 链接们 if u.split("#")[0].endswith(".whl") and 标签 in u]
            if 匹配:
                # 取列表最后一个（simple 索引按版本升序，末尾即最新）
                链接 = 匹配[-1].split("#")[0]
                文件名 = 链接.rsplit("/", 1)[-1]
                return {"源名": 源名, "url": _urljoin(simple地址, 链接), "size": 0, "filename": 文件名}
    except Exception:
        pass
    return None


def _单源下载进程入口(源名: str, 根地址: str, 子目录: str, 目录: str, 结果队列) -> None:
    """子进程入口：独立完成「查元数据 → 下载」全流程，从元数据阶段就参与竞速。刻意做成模块级顶层函数，保证 Windows spawn 子进程能 pickle 启动。子进程只依赖标准库（httpx 可选），不 import 整个 mcp_image，冷启动快。

    设计要点：元数据查询不再集中在主进程串行等待，而是下放到每个子进程，
    使不同源从第一步（查元数据）就开始并行竞速；最快的源（元数据快+下载快）率先完成即胜出，慢源/超时源来不及拖慢整体。不同源下载的 wheel 往往同名，故每个源写入自己独立的子目录，避免同名覆盖，也避免主进程清理非胜源时误删胜源文件。
    """
    try:
        import httpx as _httpx
    except ImportError:
        _httpx = None

    try:
        # 第一步：查本源的元数据（JSON 优先，失败回退 simple 索引）。
        信息 = _查询单源wheel信息(源名, 根地址)
        if not 信息:
            结果队列.put({"源名": 源名, "完成": False, "阶段": "元数据"})
            return

        本源目录 = os.path.join(目录, 子目录)
        os.makedirs(本源目录, exist_ok=True)
        目标路径 = os.path.join(本源目录, 信息["filename"])

        # 第二步：下载 wheel。
        if _httpx is not None:
            with _httpx.Client(follow_redirects=True, timeout=_httpx.Timeout(None, connect=20)) as client:
                with client.stream("GET", 信息["url"]) as resp:
                    resp.raise_for_status()
                    with open(目标路径, "wb") as f:
                        for 块 in resp.iter_bytes(chunk_size=262144):
                            f.write(块)
        else:
            from urllib.request import Request as _Request, urlopen as _urlopen
            with _urlopen(_Request(信息["url"], headers={"User-Agent": "pip/24.0"}), timeout=30) as resp:
                with open(目标路径, "wb") as f:
                    while True:
                        块 = resp.read(262144)
                        if not 块:
                            break
                        f.write(块)
        大小 = os.path.getsize(目标路径)
        # 只有完整写完才上报“完成”。主进程收到此消息即认定该源胜出。
        结果队列.put({"源名": 源名, "path": 目标路径, "size": 大小, "完成": True})
    except Exception:
        # 失败/被中止：尽力清理半成品，并上报失败让主进程计数。
        try:
            if os.path.isfile(目标路径):
                os.remove(目标路径)
        except Exception:
            pass
        try:
            结果队列.put({"源名": 源名, "完成": False})
        except Exception:
            pass


def _竞速下载wheel(目录: str, 源列表: Optional[List[tuple]] = None,
                   总超时秒: float = 300) -> Optional[dict]:
    """多源进程级竞速下载 wheel。首个完整下载完成的源胜出，其余进程立即被 terminate。返回 {源名, path, size} 或 None（全部失败）。

    为什么用进程而不是线程：Windows 上无法可靠强杀阻塞在 socket 读的线程，而 Process.terminate() 就是 TerminateProcess，无论子进程卡在网络读、DNS还是任何 C 层阻塞，都能立刻结束，不会留下杀不掉的悬挂任务。竞速从元数据阶段就开始：每个子进程独立完成「查元数据→下载」全流程，主进程一开始就把所有源的进程同时撒出去，不做集中的元数据查询等待。

    中止语义：只有已经产生胜者之后才 terminate 其余进程；只要还没有任何源下载完成，就继续等待，绝不提前杀掉仍在正常下载的进程。总超时秒 是最后兜底。
    """
    import multiprocessing as _mp

    源列表 = 源列表 or _PyMuPDF下载源

    logger.info("开始多源竞速下载 PyMuPDF（每源一进程，含元数据查询），源: %s", "、".join(名 for 名, _ in 源列表))

    # 直接为每个源起一个独立进程，子进程内部自行查元数据并下载到独立子目录。
    # spawn 上下文：Windows 默认即 spawn；子进程入口是顶层函数可 pickle。
    上下文 = _mp.get_context("spawn")
    结果队列 = 上下文.Queue()
    进程表 = {}  # Process -> (源名, 子目录)
    for 序号, (源名, 根地址) in enumerate(源列表):
        子目录 = f"src{序号}"
        p = 上下文.Process(target=_单源下载进程入口, args=(源名, 根地址, 子目录, 目录, 结果队列), daemon=True)
        p.start()
        进程表[p] = (源名, 子目录)

    胜出: Optional[dict] = None
    失败数 = 0
    开始时间 = time.time()

    try:
        # 没产生胜者且还有进程在跑，就继续等队列消息。
        while 胜出 is None and 失败数 < len(进程表):
            剩余 = 总超时秒 - (time.time() - 开始时间)
            if 剩余 <= 0:
                logger.warning("等待胜源超时（%s 秒），仍有源未完成", 总超时秒)
                break
            try:
                消息 = 结果队列.get(timeout=min(剩余, 1.0))
            except Exception:
                # 每秒醒一次，顺便检查是否所有进程都已退出却无结果（异常兜底）。
                if not any(p.is_alive() for p in 进程表):
                    break
                continue

            if 消息.get("完成"):
                # 首个上报完成的源即胜者。文件已完整落在独立子目录，主进程直接用。
                胜出 = {"源名": 消息["源名"], "path": 消息["path"], "size": 消息["size"]}
                logger.info("源 %s 下载完成，判定为胜源，正在 terminate 其余进程...", 消息["源名"])
                break
            else:
                失败数 += 1
    finally:
        # 收尾：无论胜/负/超时，终止所有非胜源进程。有胜者时只杀其它进程；无胜者时全部杀掉，绝不留悬挂下载进程。
        for p, (名, _子目录) in 进程表.items():
            if 胜出 is not None and 名 == 胜出["源名"]:
                continue
            if p.is_alive():
                try:
                    p.terminate()
                except Exception:
                    pass
        # 给胜源一个正常退出的机会；其它进程 join 确保被回收。
        for p, (名, _子目录) in 进程表.items():
            try:
                p.join(timeout=3 if (胜出 and 名 == 胜出["源名"]) else 1)
            except Exception:
                pass
        try:
            结果队列.close()
            结果队列.join_thread()
        except Exception:
            pass

        # 修复被 terminate 的子进程来不及清理自己半成品文件的问题：主进程统一删除非胜源的整个子目录。各源写独立子目录，删非胜源不会误伤胜源文件。
        for p, (名, 子目录) in 进程表.items():
            if 胜出 is not None and 名 == 胜出["源名"]:
                continue
            try:
                shutil.rmtree(os.path.join(目录, 子目录), ignore_errors=True)
            except Exception:
                pass

    if 胜出 is not None:
        return 胜出

    # 全部失败：清掉共享目录里可能的半成品文件。
    try:
        for 名 in os.listdir(目录):
            路径 = os.path.join(目录, 名)
            if os.path.isdir(路径):
                shutil.rmtree(路径, ignore_errors=True)
            else:
                try:
                    os.remove(路径)
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _格式化大小(字节数: int) -> str:
    if 字节数 >= 1_000_000:
        return f"{字节数 / 1_000_000:.2f} MB"
    if 字节数 >= 1_000:
        return f"{字节数 / 1_000:.1f} KB"
    return f"{字节数} B"


# ---- PyMuPDF 按需安装的并发控制 ----
# 进程内锁：串行化同一进程里的并发首次调用。
_pymupdf线程锁 = threading.Lock()
# 就绪标志：装好后本进程后续调用直接返回，不再重复 import 检查。
_pymupdf就绪 = False
# 全局 fitz 模块引用：_pdf单页转图像/_pdf获取总页数 里的 fitz.open 依赖它。
fitz = None


def _标记pymupdf就绪() -> None:
    """标记 PyMuPDF 就绪，并把 fitz 模块绑定到全局供 fitz.open 使用。

    修复重写 _ensure_pymupdf 时删去 global fitz 后，_pdf单页转图像/_pdf获取总页数 中 fitz.open 报 NameError 的问题。
    """
    global _pymupdf就绪, fitz
    import fitz as _fitz模块
    fitz = _fitz模块
    _pymupdf就绪 = True


@contextmanager
def _持有pymupdf安装锁(超时秒: float = 900.0):
    """跨进程互斥安装 PyMuPDF。

    锁文件放在系统临时目录（tempfile.gettempdir()），不能用 TEMP_DIR：uvx运行时 TEMP_DIR 位于 uv 缓存快照内，快照重建后路径改变，锁会失效。持锁进程崩溃时操作系统会随文件句柄关闭自动释放锁，不会死锁。超时仅作兜底，避免异常情况下调用方无限等待。
    """
    锁目录 = tempfile.gettempdir()
    os.makedirs(锁目录, exist_ok=True)
    锁路径 = os.path.join(锁目录, "mcp-image-pymupdf-install.lock")

    with open(锁路径, "a+b") as 锁文件:
        # msvcrt.locking 需要文件至少有 1 字节才能锁住该区域。
        锁文件.seek(0, os.SEEK_END)
        if 锁文件.tell() == 0:
            锁文件.write(b"0")
            锁文件.flush()
        锁文件.seek(0)

        开始时间 = time.time()
        首次等待 = True
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt as _msvcrt
                    _msvcrt.locking(锁文件.fileno(), _msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl as _fcntl
                    _fcntl.flock(锁文件.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - 开始时间 > 超时秒:
                    raise TimeoutError(
                        f"等待 PyMuPDF 安装锁超时（{超时秒} 秒）: {锁路径}。"
                        "可能有其它进程正在安装，或上次安装异常中断。"
                    )
                if 首次等待:
                    logger.info("另一进程正在安装 PyMuPDF，等待其完成后复用...")
                    首次等待 = False
                time.sleep(0.5)

        try:
            yield
        finally:
            try:
                锁文件.seek(0)
                if sys.platform == "win32":
                    import msvcrt as _msvcrt
                    _msvcrt.locking(锁文件.fileno(), _msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl as _fcntl
                    _fcntl.flock(锁文件.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass


def _选择pymupdf安装目录() -> str:
    """选择可写的安装目录。

    修复在 Microsoft Store 版 Python 等场景下 purelib 指向受保护系统目录、解压安装报 PermissionError [WinError 5] 拒绝访问 的问题：优先用 purelib（uvx 临时环境可写、且与运行环境一致）；若不可写，回退到用户级 site-packages（pip --user 的安装位置，无需管理员权限）。
    """
    import site as _site
    import sysconfig as _sysconfig

    候选 = []
    try:
        候选.append(_sysconfig.get_path("purelib"))
    except Exception:
        pass
    try:
        候选.append(_site.getusersitepackages())
    except Exception:
        pass

    for 目录 in 候选:
        if not 目录:
            continue
        try:
            os.makedirs(目录, exist_ok=True)
            # 实际写一个临时文件验证可写（仅判断目录存在不可靠）
            探针 = os.path.join(目录, f".mcp-write-test-{os.getpid()}")
            with open(探针, "w") as f:
                f.write("")
            os.remove(探针)
            return 目录
        except Exception:
            continue

    # 都不可写时退回 purelib，让后续报错信息指向它。
    return _sysconfig.get_path("purelib")


def _安装本地wheel(wheel路径: str) -> bool:
    """直接将 wheel 解压到可写的 site-packages，绕过 pip/uv 子进程避免卡死。

    调用方须已持有 PyMuPDF 安装锁（见 _ensure_pymupdf）：解压不是原子操作，靠锁保证不会有其它进程同时读写同一批文件。
    """
    import zipfile as _zipfile

    _目标目录 = _选择pymupdf安装目录()
    logger.info("正在安装 PyMuPDF 到 %s...", _目标目录)
    for _h in logger.handlers:
        _h.flush()

    try:
        with _zipfile.ZipFile(wheel路径, "r") as _zf:
            # 跳过 .dist-info 的 RECORD 校验（路径可能不匹配）
            _成员列表 = [m for m in _zf.infolist() if not m.is_dir()]
            _总文件数 = len(_成员列表)
            for _i, _成员 in enumerate(_成员列表):
                _目标文件 = os.path.join(_目标目录, _成员.filename)
                os.makedirs(os.path.dirname(_目标文件), exist_ok=True)
                with _zf.open(_成员) as _源, open(_目标文件, "wb") as _目标:
                    _目标.write(_源.read())
                if (_i + 1) % 10 == 0 or _i == _总文件数 - 1:
                    logger.debug("安装 PyMuPDF: %d/%d 文件", _i + 1, _总文件数)
        logger.info("PyMuPDF 安装完成")
        return True
    except Exception:
        logger.exception("解压安装 PyMuPDF 失败")
        return False


def _ensure_pymupdf():
    """修复 pymupdf（约 70 MB）作为必装依赖导致初始安装过大的问题；改为首次调用 PDF 功能时按需安装。

    并发安全（修复并发首次调用导致重复下载、wheel 互相覆盖的问题）：
    - 进程内用 threading.Lock 串行化；
    - 跨进程用系统临时目录下的文件锁互斥（uvx 下 TEMP_DIR 随缓存快照变化，不能用作锁位置）；
    - 拿到锁后再次尝试 import：若另一进程/线程已装好，直接返回，不重复下载；
    - 下载与解压都在锁内完成。
    """
    # 进程内快速路径：已就绪则连锁都不用拿。
    if _pymupdf就绪:
        return

    with _pymupdf线程锁:
        if _pymupdf就绪:
            return
        try:
            import fitz  # noqa: F401
            _标记pymupdf就绪()
            return
        except ImportError:
            pass

        # 跨进程锁：同一台机器上多个 MCP 服务器进程可能同时首次调用 PDF。锁内会再次 import 检查，因此拿到锁时发现已被别的进程装好就直接返回。
        with _持有pymupdf安装锁():
            try:
                import fitz  # noqa: F401
                _标记pymupdf就绪()
                logger.debug("PyMuPDF 已由其它进程安装完成，直接复用")
                return
            except ImportError:
                pass

            _成功 = False

            # 多源并行竞速下载 wheel 后解压安装。竞速函数内部并行查询各源元数据并下载，首个完成者胜出、其余立即强制中止；不再保留 uv/pip 回退安装策略。
            with tempfile.TemporaryDirectory(prefix="pymupdf-wheel-") as _下载目录:
                _胜出 = _竞速下载wheel(_下载目录)
                if _胜出:
                    logger.info("使用源 %s 的 wheel（%s）进行安装", _胜出["源名"], _格式化大小(_胜出["size"]))
                    if _安装本地wheel(_胜出["path"]):
                        _成功 = True

            if not _成功:
                raise RuntimeError(
                    "多源竞速下载 PyMuPDF 均失败或解压安装失败。请检查网络是否能访问任一 PyPI 镜像源，或手动 pip install pymupdf 后重试。"
                )

            try:
                import fitz  # noqa: F401
            except ImportError as 导入异常:
                # 装完仍无法导入时给出环境信息，便于定位“装到了别的环境”这类问题。
                import sysconfig as _sysconfig
                raise ImportError(
                    "PyMuPDF 安装后仍无法导入 fitz。"
                    f"当前解释器: {sys.executable}；purelib: {_sysconfig.get_path('purelib')}。"
                    "可能是安装目标环境与运行环境不一致。"
                ) from 导入异常
            _标记pymupdf就绪()
            logger.info("PyMuPDF 安装完成，继续处理 PDF")


def _pdf单页转图像(
    PDF路径: str,
    页码: int,
    输出格式: str = "png",
    DPI: int = 150,
) -> bytes:
    """将 PDF 的单个页面渲染为 PNG/JPEG 图像字节。
    
    参数:
        PDF路径: PDF 文件的绝对路径。
        页码: 目标页码，从 1 开始计数。
        输出格式: 输出图像格式，支持 "png" 或 "jpeg"。
        DPI: 渲染分辨率，默认 150。
    
    返回:
        渲染后的图像字节数据。
    """
    # 修复 PDF 文件不存在时 PyMuPDF 抛出晦涩错误的问题。
    if not os.path.isfile(PDF路径):
        raise FileNotFoundError(f"PDF 文件不存在: {PDF路径}")

    _ensure_pymupdf()
    文档 = None
    try:
        文档 = fitz.open(PDF路径)
        总页数 = 文档.page_count

        if 页码 < 1 or 页码 > 总页数:
            raise ValueError(f"页码 {页码} 超出范围，PDF 共 {总页数} 页（页码从 1 开始）")

        # 获取目标页面（PyMuPDF 内部页码从 0 开始）
        页面 = 文档[页码 - 1]

        # 将页面渲染为像素图
        像素图 = 页面.get_pixmap(dpi=DPI)

        if 输出格式 == "jpeg":
            图像字节 = 像素图.tobytes("jpeg")
        else:
            图像字节 = 像素图.tobytes("png")

        return 图像字节
    finally:
        if 文档 is not None:
            文档.close()


def _pdf获取总页数(PDF路径: str) -> int:
    """只读取 PDF 总页数，不渲染任何页面。

    参数:
        PDF路径: PDF 文件的绝对路径。

    返回:
        PDF 的总页数。
    """
    if not os.path.isfile(PDF路径):
        raise FileNotFoundError(f"PDF 文件不存在: {PDF路径}")

    _ensure_pymupdf()
    文档 = None
    try:
        文档 = fitz.open(PDF路径)
        return int(文档.page_count)
    finally:
        if 文档 is not None:
            文档.close()


@mcp.tool()
async def PDF转图像(
    PDF文件路径: str,
    页码: Optional[List[int]] = None,
    DPI: Optional[int] = None,
    ctx: Context = None,
):
    """
    将 PDF 文件的指定页面转换为图像，返回给智能体。

    本工具支持将 PDF 文档的一页或多页渲染为 PNG 图像。
    如果不指定页码范围，则不渲染任何页面，仅返回 PDF 的总页数。
    渲染默认使用 150 DPI，在清晰度与文件大小之间取得平衡。

    参数:
        PDF文件路径: PDF 文件的绝对路径（如 "C:/文档/报告.pdf"）。
        页码: 可选，需要转换的页码列表，页码从 1 开始计数。
              例如 [1, 3, 5] 将转换第 1、3、5 页。
              如果不指定此参数，则不渲染任何页面，仅返回总页数提示（用于先摸清页数再按需取页）。
        DPI: 可选，渲染分辨率，默认 150。更高值图像更清晰但文件更大。

    返回:
        指定页码时：图像列表，顺序与请求的页码一致，
        每个元素为一个 Image 对象（成功时）或错误描述字符串（失败时）。
        未指定页码时：一段说明 PDF 总页数的文字，不包含任何图像。
    """
    try:
        # 修复 PDF 路径为空时下游报错不明确的问题。
        if not PDF文件路径:
            return ["错误：PDF文件路径 不能为空"]

        if not os.path.isfile(PDF文件路径):
            return [f"错误：PDF 文件不存在: {PDF文件路径}"]

        # 修复 subprocess.call 在 async 上下文中阻塞事件循环的问题；将安装放到线程池执行。
        await asyncio.to_thread(_ensure_pymupdf)

        # 未指定页码时不再渲染任何页面，只返回总页数，避免整本 PDF 被一次性灌进上下文。
        if 页码 is None:
            # 与 _ensure_pymupdf 同样放线程池执行，避免 fitz.open 阻塞事件循环。
            总页数 = await asyncio.to_thread(_pdf获取总页数, PDF文件路径)
            logger.debug(f"未指定页码，不渲染页面，仅返回总页数 {总页数}: {PDF文件路径}")
            if 总页数 <= 0:
                return f"该 PDF 不含任何页面: {PDF文件路径}"
            # 示例页码按实际页数生成，避免给 1 页 PDF 提示 [1, 2, 3] 这类越界示例。
            示例页码 = list(range(1, min(3, 总页数) + 1))
            return (
                f"未指定页码，本次未渲染任何页面。该 PDF 共 {总页数} 页（有效页码范围 1-{总页数}）。"
                f"请通过 页码 参数指定需要转换的页面，例如 {示例页码}。"
            )

        输出格式 = "png"
        实际DPI = 150 if DPI is None else max(50, min(DPI, 1200))

        # 转换指定页面
        图像结果: List[Any] = []
        for 单页页码 in 页码:
            try:
                图像字节 = _pdf单页转图像(PDF文件路径, 单页页码, 输出格式, 实际DPI)
                处理后图像 = await process_image_data(
                    图像字节, 输出格式,
                    f"{PDF文件路径} 第{单页页码}页", ctx
                )
                if 处理后图像 is not None:
                    图像结果.append(处理后图像)
                else:
                    图像结果.append(f"处理第{单页页码}页图像失败")
            except Exception as 异常:
                错误信息 = f"转换第{单页页码}页失败: {str(异常)}"
                await ctx.error(错误信息)
                logger.exception(错误信息)
                图像结果.append(错误信息)

        return 图像结果
    except Exception as 异常:
        错误信息 = f"PDF 转图像时发生错误: {str(异常)}"
        await ctx.error(错误信息)
        logger.exception(错误信息)
        return [错误信息]


def _补丁工具参数名大小写不敏感(工具函数名: str) -> None:
    """修复 VS Code MCP 客户端将参数名中 ASCII 字符强制转为小写导致 Pydantic 校验失败的问题。
    
    通过给工具的 call_fn_with_arg_validation 方法添加一个包装层，
    在校验前将传入参数名按大小写不敏感的方式映射回 Python 函数签名中的原始参数名。
    """
    import types as _types
    # 修复 mcp 2.0 中 FuncMetadata 移到 mcpserver 子包导致的导入失败。
    from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata as _FuncMetadata

    工具对象 = mcp._tool_manager._tools.get(工具函数名)
    if 工具对象 is None:
        return

    arg_model = 工具对象.fn_metadata.arg_model
    字段名列表 = list(arg_model.model_fields.keys())

    # 构建 小写键 → 原始键 的映射（仅当小写后不同时才加入）
    小写到原始: dict[str, str] = {}
    for 名称 in 字段名列表:
        小写名称 = 名称.lower()
        if 小写名称 != 名称:
            小写到原始[小写名称] = 名称

    if not 小写到原始:
        return  # 无需补丁

    原始方法 = 工具对象.fn_metadata.call_fn_with_arg_validation

    # 修复 mcp 2.0 的 call_fn_with_arg_validation 新增第 5 个可选参数 pre_validated；包装签名须透传。
    async def _大小写不敏感调用(
        self: _FuncMetadata,
        fn,
        fn_is_async,
        arguments_to_validate,
        arguments_to_pass_directly,
        pre_validated=None,
    ):
        规范化参数 = {}
        for 键, 值 in arguments_to_validate.items():
            小写键 = 键.lower()
            if 小写键 in 小写到原始:
                规范化参数[小写到原始[小写键]] = 值
            else:
                规范化参数[键] = 值
        return await 原始方法(fn, fn_is_async, 规范化参数, arguments_to_pass_directly, pre_validated)

    # 修复 Pydantic FuncMetadata 不允许直接设置未注册字段的问题；绕过 Pydantic 的 __setattr__。
    object.__setattr__(工具对象.fn_metadata, "call_fn_with_arg_validation", _types.MethodType(
        _大小写不敏感调用, 工具对象.fn_metadata
    ))


def main():
    剩余命令行参数 = _解析启动参数(sys.argv[1:])
    sys.argv = [sys.argv[0], *剩余命令行参数]
    _补丁工具参数名大小写不敏感("PDF转图像")
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
