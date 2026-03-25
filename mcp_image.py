#!/usr/bin/env python3

import os
import sys
import re
import json
import asyncio
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
SVG浏览器环境变量 = ("MCP图片浏览器路径",)
SVG表情字体环境变量 = ("MCP图片表情字体路径",)
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
            浏览器配置目录,
            str(调试端口),
            str(目标宽度),
            str(目标高度),
        ]
        # 修复 MCP stdio 模式下子渲染进程继承服务 stdin 管道后可能卡住的问题。
        运行结果 = subprocess.run(
            命令,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if 运行结果.returncode != 0:
            失败详情 = (运行结果.stderr or 运行结果.stdout or "CDP 渲染失败").strip()
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
    # 修复 Agent 调用时 svg_dpi 被当作可选参数导致调用约束不明确的问题。
    svg_dpi: int,
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
        svg_dpi: Required DPI for SVG to PNG conversion. Higher values produce clearer images but larger files.
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

def main():
    剩余命令行参数 = _解析启动参数(sys.argv[1:])
    sys.argv = [sys.argv[0], *剩余命令行参数]
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
