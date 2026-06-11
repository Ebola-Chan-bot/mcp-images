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
import httpx
import logging
from io import BytesIO
from datetime import datetime
from PIL import Image as PILImage
from pathlib import Path
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree

TEMP_DIR = "./Temp"
DATA_DIR = "./data"
CDP渲染脚本路径 = os.path.join(os.path.dirname(__file__), "SVG转PNG渲染器.py")
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
    调试端口 = _获取空闲TCP端口()

    with tempfile.TemporaryDirectory(prefix="svg-cdp-", dir=TEMP_DIR) as 工作目录:
        包装页路径 = os.path.join(工作目录, "wrapper.html")
        PNG输出路径 = os.path.join(工作目录, "rendered.png")
        报告路径 = os.path.join(工作目录, "render-report.json")
        状态路径 = os.path.join(工作目录, "render-status.json")
        浏览器配置目录 = os.path.join(工作目录, "profile")
        SVG文本 = svg_data.decode("utf-8", errors="replace")
        HTML文本 = _构建SVG包装HTML(SVG文本, 目标宽度, 目标高度, 表情字体文件)
        with open(包装页路径, "w", encoding="utf-8") as handle:
            handle.write(HTML文本)

        命令 = [
            sys.executable,
            CDP渲染脚本路径,
            浏览器可执行文件,
            Path(包装页路径).resolve().as_uri(),
            PNG输出路径,
            报告路径,
            状态路径,
            浏览器配置目录,
            str(调试端口),
            str(目标宽度),
            str(目标高度),
        ]
        # 修复大 SVG 在 CDP 截图阶段只能盲等子进程退出，父进程无法区分慢渲染和卡死的问题。
        渲染进程 = subprocess.Popen(
            命令,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        开始时间 = time.time()
        上次状态时间戳 = None
        最近状态描述 = "尚未收到状态"

        while 渲染进程.poll() is None:
            if os.path.isfile(状态路径):
                try:
                    with open(状态路径, "r", encoding="utf-8") as 状态文件:
                        当前状态 = json.load(状态文件)
                    最近状态描述 = 当前状态.get("阶段", 最近状态描述)
                    上次状态时间戳 = 当前状态.get("时间戳", 上次状态时间戳)
                except Exception:
                    pass

            已等待秒数 = time.time() - 开始时间
            if 已等待秒数 > 240:
                渲染进程.kill()
                标准输出, 标准错误 = 渲染进程.communicate()
                raise RuntimeError(
                    f"CDP 渲染超时；最近状态：{最近状态描述}；标准错误：{(标准错误 or 标准输出 or '无')[:500]}"
                )

            if 上次状态时间戳 and time.time() - 上次状态时间戳 > 30:
                渲染进程.kill()
                标准输出, 标准错误 = 渲染进程.communicate()
                raise RuntimeError(
                    f"CDP 渲染状态心跳已停止；最近状态：{最近状态描述}；标准错误：{(标准错误 or 标准输出 or '无')[:500]}"
                )

            time.sleep(0.5)

        标准输出, 标准错误 = 渲染进程.communicate()
        if 渲染进程.returncode != 0:
            失败详情 = (标准错误 or 标准输出 or "CDP 渲染失败").strip()
            raise RuntimeError(失败详情)
        if not os.path.isfile(PNG输出路径):
            raise RuntimeError("CDP 渲染器未生成输出 PNG")

        with open(PNG输出路径, "rb") as handle:
            return handle.read()


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
) -> Dict[str, Any]:
    try:
        svg_dpi = max(50, min(svg_dpi, 1200))
        logger.debug("检测到 SVG 图片: %s，按 %s DPI 通过 CDP 渲染", image_source, svg_dpi)
        png_data = await asyncio.to_thread(
            通过CDP渲染SVG到PNG,
            svg_data,
            svg_dpi,
            浏览器路径,
            表情字体路径,
        )
        处理后图片 = await process_image_data(png_data, "png", image_source, ctx)
        if 处理后图片 is None:
            return {"error": "处理 SVG 图片失败"}
        return {"image": 处理后图片}
    except Exception as exc:
        错误信息 = f"转换 SVG {image_source} 时出错: {str(exc)}"
        ctx.error(错误信息)
        logger.exception(错误信息)
        return {"error": 错误信息}


# Create a FastMCP server instance
mcp = FastMCP("image-service")

async def process_image_data(data: bytes, content_type: str, image_source: str, ctx: Context) -> Image | None:
    """Process image data and return an MCP Image object."""
    try:
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
                            ctx.error("Unable to compress image to acceptable size while maintaining quality")
                            logger.error(f"Failed processing image from {image_source}: dimensions too small")
                            return None
                        logger.debug(f"Applying scale factor {scale_factor} to image from {image_source}")
                        quality = 85  # Reset quality when changing size
        except MemoryError as e:
            ctx.error(f"Out of memory processing large image: {str(e)}")
            logger.error(f"MemoryError processing image from {image_source}: {str(e)}")
            return None
        except Exception as e:
            ctx.error(f"Image processing error: {str(e)}")
            logger.exception(f"Exception processing image from {image_source}")
            return None
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except Exception as e:
        ctx.error(f"Error processing image: {str(e)}")
        logger.exception(f"Unexpected error processing {image_source}")
        return None

async def process_local_image(
    file_path: str,
    ctx: Context,
    svg_dpi: int = 150,
    浏览器路径: Optional[str] = None,
    表情字体路径: Optional[str] = None,
) -> Dict[str, Any]:
    """Processes a local image file and returns a dictionary with the result."""
    try:
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            ctx.error(error_msg)
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
                )
                SVG处理结果["path"] = file_path
                return SVG处理结果
            except Exception as e:
                error_msg = f"转换 SVG {file_path} 时出错: {str(e)}"
                ctx.error(error_msg)
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
            return await process_large_local_image(file_path, content_type, ctx)
        
        # For smaller files, read the entire content
        with open(file_path, "rb") as f:
            file_data = f.read()
        
        logger.debug(f"Read local image from {file_path} with {len(file_data)} bytes")
        processed_image = await process_image_data(file_data, content_type, file_path, ctx)
        
        if processed_image is None:
            return {"path": file_path, "error": "Failed to process image"}
        
        return {"path": file_path, "image": processed_image}
        
    except Exception as e:
        error_msg = f"Error processing local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}

async def process_large_local_image(file_path: str, content_type: str, ctx: Context) -> Dict[str, Any]:
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
            
            # Apply initial scale if needed
            if initial_scale < 1.0:
                width = int(orig_width * initial_scale)
                height = int(orig_height * initial_scale)
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
                        ctx.error(error_msg)
                        logger.error(error_msg)
                        return {"path": file_path, "error": error_msg}
                    
                    logger.debug(f"Applying scale factor {scale_factor} to image {file_path}")
                    quality = 85  # Reset quality when changing size
    
    except MemoryError as e:
        error_msg = f"Out of memory processing large local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.error(error_msg)
        return {"path": file_path, "error": error_msg}
    except Exception as e:
        error_msg = f"Error processing large local image {file_path}: {str(e)}"
        ctx.error(error_msg)
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
) -> Dict[str, Any]:
    """Fetches and processes a single image asynchronously."""
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme in ['http', 'https'], parsed.netloc]):
            error_msg = f"Invalid URL: {url}"
            ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        response = await client.get(url)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            error_msg = f"Not an image (got {content_type})"
            ctx.error(error_msg)
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
            )
            SVG处理结果["url"] = url
            return SVG处理结果
        
        # Extract the format from content-type
        format_type = content_type.split('/')[-1]
        logger.debug(f"Extracted format type: {format_type}")
        
        processed_image = await process_image_data(response.content, format_type, url, ctx)
        
        if processed_image is None:
            return {"url": url, "error": "Failed to process image"}
        
        return {"url": url, "image": processed_image}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        ctx.error(error_msg)
        return {"url": url, "error": error_msg}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        ctx.error(error_msg)
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
        
    Returns:
        A list in the same order as the input sources.
        Each item is either an Image object (success) or an error string (failure).
    """
    try:
        start_time = asyncio.get_event_loop().time()
        
        # Validate input
        if not image_sources:
            ctx.error("No image sources provided")
            logger.error("fetch_images called with empty source list")
            return []

        if _输入包含SVG(image_sources):
            if svg_dpi is None:
                # 修复 SVG 输入缺少 svg_dpi 时继续带着空值向下游传递的问题。
                error_message = "svg_dpi is required when image_sources contains SVG files"
                ctx.error(error_message)
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
                ctx.error(error_message)
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
        ctx.error(f"Failed to process images: {str(e)}")
        return [f"Failed to process source: {src}. Error: {str(e)}" for src in image_sources]

def _获取pymupdf信息() -> Optional[dict]:
    """查询 PyPI 获取适配当前平台的 pymupdf wheel 的 URL、大小和文件名。"""
    import json as _json
    import platform as _platform
    from urllib.request import urlopen as _urlopen

    _机器 = _platform.machine().lower()
    _系统 = sys.platform

    # 根据当前平台构建候选 platform_tag 列表，按优先级排序
    if _系统 == "win32":
        _候选标签 = ["win_amd64", "win32"] if _机器 in ("amd64", "x86_64") else ["win32"]
    elif _系统 == "darwin":
        _候选标签 = ["macosx_11_0_arm64", "macosx_10_9_x86_64"] if _机器 == "arm64" else ["macosx_10_9_x86_64"]
    else:
        _候选标签 = ["manylinux_2_28_x86_64", "musllinux_1_2_x86_64", "manylinux_2_28_aarch64"]

    try:
        with _urlopen("https://pypi.org/pypi/pymupdf/json", timeout=5) as _resp:
            _data = _json.loads(_resp.read().decode())
        _files = _data.get("urls", []) or _data.get("info", {}).get("urls", [])

        for _标签 in _候选标签:
            for _f in _files:
                if _f.get("packagetype") == "bdist_wheel" and _标签 in _f.get("filename", ""):
                    _size = _f.get("size", 0)
                    _url = _f.get("url", "")
                    _filename = _f.get("filename", "")
                    if _size > 0 and _url:
                        return {"url": _url, "size": _size, "filename": _filename}

        # 无精确匹配时取任意 wheel（回退用，安装可能失败但由 pip 兜底）
        for _f in _files:
            if _f.get("packagetype") == "bdist_wheel":
                _size = _f.get("size", 0)
                _url = _f.get("url", "")
                _filename = _f.get("filename", "")
                if _size > 0 and _url:
                    return {"url": _url, "size": _size, "filename": _filename}
        return None
    except Exception:
        return None


def _格式化大小(字节数: int) -> str:
    if 字节数 >= 1_000_000:
        return f"{字节数 / 1_000_000:.2f} MB"
    if 字节数 >= 1_000:
        return f"{字节数 / 1_000:.1f} KB"
    return f"{字节数} B"


def _下载wheel(url: str, 目标路径: str, 总大小: int) -> bool:
    """用 httpx 流式下载 wheel，实时报告已下载/总大小/速度。返回是否成功。"""
    try:
        import httpx as _httpx
        _开始时间 = time.time()
        _已下载 = 0
        _上次报告时间 = 0.0

        with _httpx.Client(follow_redirects=True, timeout=_httpx.Timeout(600, connect=30)) as _client:
            with _client.stream("GET", url) as _resp:
                _resp.raise_for_status()
                with open(目标路径, "wb") as _f:
                    for _块 in _resp.iter_bytes(chunk_size=8192):
                        _f.write(_块)
                        _已下载 += len(_块)
                        _现在 = time.time()
                        # 每 5 秒报告一次进度
                        if _现在 - _上次报告时间 >= 5:
                            _耗时 = _现在 - _开始时间
                            _速度 = _已下载 / _耗时 if _耗时 > 0 else 0
                            _百分比 = _已下载 / 总大小 * 100 if 总大小 > 0 else 0
                            logger.info(
                                "下载 PyMuPDF: %s / %s (%.0f%%)，速度 %s/s",
                                _格式化大小(_已下载), _格式化大小(总大小), _百分比, _格式化大小(int(_速度)),
                            )
                            for _h in logger.handlers:
                                _h.flush()
                            _上次报告时间 = _现在

        _总耗时 = time.time() - _开始时间
        _平均速度 = _已下载 / _总耗时 if _总耗时 > 0 else 0
        logger.info("PyMuPDF 下载完成: %s，耗时 %.0f 秒，平均速度 %s/s", _格式化大小(_已下载), _总耗时, _格式化大小(int(_平均速度)))
        return True
    except Exception:
        logger.exception("下载 PyMuPDF wheel 失败")
        return False


def _安装本地wheel(wheel路径: str) -> bool:
    """直接将 wheel 解压到 site-packages，绕过 pip/uv 子进程避免卡死。"""
    import zipfile as _zipfile
    import site as _site

    _目标目录 = _site.getsitepackages()[0] if _site.getsitepackages() else os.path.join(sys.prefix, "Lib", "site-packages")
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
    """修复 pymupdf（约 70 MB）作为必装依赖导致初始安装过大的问题；改为首次调用 PDF 功能时按需安装。"""
    global fitz
    try:
        import fitz
    except ImportError:
        _信息 = _获取pymupdf信息()
        if _信息:
            logger.info("首次使用 PDF 功能，正在安装 PyMuPDF（%s）...", _格式化大小(_信息["size"]))
        else:
            logger.info("首次使用 PDF 功能，正在安装 PyMuPDF...")
        for _h in logger.handlers:
            _h.flush()

        import subprocess as _subprocess
        _成功 = False

        # 策略1：自己下载 wheel 后直接解压安装（最快、可控进度、不怕子进程卡死）
        if _信息:
            _wheel路径 = os.path.join(TEMP_DIR, _信息["filename"])
            try:
                if _下载wheel(_信息["url"], _wheel路径, _信息["size"]):
                    if _安装本地wheel(_wheel路径):
                        _成功 = True
            finally:
                try:
                    os.remove(_wheel路径)
                except Exception:
                    pass

        # 策略2：让 pip/uv 自己下载安装（回退）
        if not _成功:
            logger.info("改用 pip 安装 PyMuPDF...")
            _返回码 = _subprocess.call([sys.executable, "-m", "pip", "install", "pymupdf>=1.24.0"])
            if _返回码 != 0:
                _返回码 = _subprocess.call(["uv", "pip", "install", "pymupdf>=1.24.0"])
                if _返回码 != 0:
                    raise _subprocess.CalledProcessError(_返回码, ["uv", "pip", "install", "pymupdf>=1.24.0"])

        import fitz
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


def _pdf全部页转图像(
    PDF路径: str,
    输出格式: str = "png",
    DPI: int = 150,
) -> List[Dict[str, Any]]:
    """将 PDF 的全部页面渲染为图像列表。

    参数:
        PDF路径: PDF 文件的绝对路径。
        输出格式: 输出图像格式。

    返回:
        列表，每项包含 "页码" 和 "图像字节"。
    """
    if not os.path.isfile(PDF路径):
        raise FileNotFoundError(f"PDF 文件不存在: {PDF路径}")

    _ensure_pymupdf()
    文档 = None
    try:
        文档 = fitz.open(PDF路径)
        总页数 = 文档.page_count
        结果列表: List[Dict[str, Any]] = []

        for 索引 in range(总页数):
            页面 = 文档[索引]
            像素图 = 页面.get_pixmap(dpi=DPI)
            if 输出格式 == "jpeg":
                图像字节 = 像素图.tobytes("jpeg")
            else:
                图像字节 = 像素图.tobytes("png")
            结果列表.append({"页码": 索引 + 1, "图像字节": 图像字节})

        return 结果列表
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
    如果不指定页码范围，则默认转换全部页面。
    渲染使用 150 DPI，在清晰度与文件大小之间取得平衡。

    参数:
        PDF文件路径: PDF 文件的绝对路径（如 "C:/文档/报告.pdf"）。
        页码: 可选，需要转换的页码列表，页码从 1 开始计数。
              例如 [1, 3, 5] 将转换第 1、3、5 页。
              如果不指定此参数，则转换 PDF 的全部页面。
        DPI: 可选，渲染分辨率，默认 150。更高值图像更清晰但文件更大。

    返回:
        图像列表，顺序与请求的页码一致。
        每个元素为一个 Image 对象（成功时）或错误描述字符串（失败时）。
    """
    try:
        # 修复 PDF 路径为空时下游报错不明确的问题。
        if not PDF文件路径:
            return ["错误：PDF文件路径 不能为空"]

        if not os.path.isfile(PDF文件路径):
            return [f"错误：PDF 文件不存在: {PDF文件路径}"]

        # 修复 subprocess.call 在 async 上下文中阻塞事件循环的问题；将安装放到线程池执行。
        await asyncio.to_thread(_ensure_pymupdf)

        输出格式 = "png"
        实际DPI = 150 if DPI is None else max(50, min(DPI, 1200))

        # 如果未指定页码，则转换全部页面
        if 页码 is None:
            logger.debug(f"未指定页码，将转换 PDF 全部页面: {PDF文件路径}")
            全部结果 = _pdf全部页转图像(PDF文件路径, 输出格式, 实际DPI)
            图像结果: List[Any] = []
            for 条目 in 全部结果:
                处理后图像 = await process_image_data(
                    条目["图像字节"], 输出格式,
                    f"{PDF文件路径} 第{条目['页码']}页", ctx
                )
                if 处理后图像 is not None:
                    图像结果.append(处理后图像)
                else:
                    图像结果.append(f"处理第{条目['页码']}页图像失败")
            return 图像结果

        # 转换指定页面
        图像结果 = []
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
                ctx.error(错误信息)
                logger.exception(错误信息)
                图像结果.append(错误信息)

        return 图像结果
    except Exception as 异常:
        错误信息 = f"PDF 转图像时发生错误: {str(异常)}"
        ctx.error(错误信息)
        logger.exception(错误信息)
        return [错误信息]


def _补丁工具参数名大小写不敏感(工具函数名: str) -> None:
    """修复 VS Code MCP 客户端将参数名中 ASCII 字符强制转为小写导致 Pydantic 校验失败的问题。
    
    通过给工具的 call_fn_with_arg_validation 方法添加一个包装层，
    在校验前将传入参数名按大小写不敏感的方式映射回 Python 函数签名中的原始参数名。
    """
    import types as _types
    from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata as _FuncMetadata

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

    async def _大小写不敏感调用(
        self: _FuncMetadata,
        fn,
        fn_is_async,
        arguments_to_validate,
        arguments_to_pass_directly,
    ):
        规范化参数 = {}
        for 键, 值 in arguments_to_validate.items():
            小写键 = 键.lower()
            if 小写键 in 小写到原始:
                规范化参数[小写到原始[小写键]] = 值
            else:
                规范化参数[键] = 值
        return await 原始方法(fn, fn_is_async, 规范化参数, arguments_to_pass_directly)

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
