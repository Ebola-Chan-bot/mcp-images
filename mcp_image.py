#!/usr/bin/env python3

import os
import sys
import re
import importlib
import asyncio
import httpx
import logging
from io import BytesIO
from datetime import datetime
from PIL import Image as PILImage
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context
from typing import List, Dict, Any, Union, Optional

WINDOWS_CAIRO_FALLBACK_DIRS = [
    r"C:\Program Files\GTK3-Runtime Win64\bin",
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Balabolka\utils",
]

_cairo_added_dirs: List[str] = []


def _add_cairo_dll_directory(path: str) -> None:
    if os.path.isdir(path):
        os.add_dll_directory(path)
        _cairo_added_dirs.append(path)


# Ensure Cairo DLL is findable on Windows.
# Set CAIRO_DLL_DIRS (os.pathsep-separated) to override DLL search paths.
if sys.platform == "win32":
    _cairo_env = os.getenv("CAIRO_DLL_DIRS")
    if _cairo_env:
        for _d in _cairo_env.split(os.pathsep):
            if _d and os.path.isdir(_d):
                _add_cairo_dll_directory(_d)
    else:
        for _d in WINDOWS_CAIRO_FALLBACK_DIRS:
            if os.path.isdir(_d):
                _add_cairo_dll_directory(_d)
                break

MAX_IMAGE_SIZE = 1024  # Maximum dimension size in pixels
TEMP_DIR = "./Temp"
DATA_DIR = "./data"

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

_cairosvg_module = None
_cairosvg_error = None


def _dedupe_paths(paths: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for path in paths:
        if not path:
            continue
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    return result


def _split_existing_dirs(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    parts = [part.strip().strip('"') for part in raw_value.split(os.pathsep)]
    return [part for part in parts if part]


def _find_cairo_dll_dirs(paths: List[str]) -> List[str]:
    matches: List[str] = []
    for path in paths:
        if os.path.isfile(os.path.join(path, "libcairo-2.dll")):
            matches.append(path)
    return _dedupe_paths(matches)


def _find_relevant_path_entries(paths: List[str]) -> List[str]:
    keywords = ("cairo", "gtk", "tesseract", "balabolka")
    matches: List[str] = []
    for path in paths:
        lower_path = path.lower()
        if any(keyword in lower_path for keyword in keywords):
            matches.append(path)
    return _dedupe_paths(matches)


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


def _format_debug_list(title: str, values: List[str], limit: int = 8) -> List[str]:
    if not values:
        return [f"{title}: <none>"]
    lines = [f"{title}:"]
    for value in values[:limit]:
        lines.append(f"  - {value}")
    if len(values) > limit:
        lines.append(f"  - ... ({len(values) - limit} more)")
    return lines


def _get_windows_cairo_diagnostics() -> str:
    cairo_env = os.getenv("CAIRO_DLL_DIRS", "")
    configured_dirs = _split_existing_dirs(cairo_env) if cairo_env else WINDOWS_CAIRO_FALLBACK_DIRS.copy()
    valid_configured_dirs = [path for path in configured_dirs if os.path.isdir(path)]
    invalid_configured_dirs = [path for path in configured_dirs if path and not os.path.isdir(path)]

    path_dirs = _split_existing_dirs(os.getenv("PATH", ""))
    relevant_path_dirs = _find_relevant_path_entries(path_dirs)
    dll_dirs = _dedupe_paths(_find_cairo_dll_dirs(valid_configured_dirs) + _find_cairo_dll_dirs(path_dirs))

    normalized_path_dirs = {os.path.normcase(os.path.normpath(path)) for path in path_dirs}
    path_contains_configured_dir = any(
        os.path.normcase(os.path.normpath(path)) in normalized_path_dirs for path in valid_configured_dirs
    )

    if cairo_env and not valid_configured_dirs:
        failure_type = "CAIRO_DLL_DIRS 已设置，但所有配置目录都无效或不存在。"
    elif dll_dirs:
        failure_type = "检测到了 libcairo-2.dll 候选文件，但加载仍然失败，通常表示依赖链缺失或 PATH 未包含所需运行库目录。"
    elif cairo_env:
        failure_type = "已设置 CAIRO_DLL_DIRS，但在这些目录及当前 PATH 中都没有找到可加载的 libcairo-2.dll。"
    else:
        failure_type = "当前进程环境中未发现可加载的 libcairo-2.dll。"

    lines = [
        "SVG support is unavailable because CairoSVG could not load libcairo on Windows.",
        "",
        f"Server: {mcp.name}",
        f"Process command line: {sys.executable} {' '.join(sys.argv)}".rstrip(),
        "",
        "Failure type:",
        f"  {failure_type}",
        "",
        "Detected environment:",
        f"  CAIRO_DLL_DIRS={cairo_env or '<not set>'}",
        f"  PATH contains configured dir: {str(path_contains_configured_dir).lower()}",
        f"  PATH contains Cairo-related dir: {str(bool(relevant_path_dirs)).lower()}",
    ]

    lines.extend(_format_debug_list("Valid configured dirs", valid_configured_dirs))
    lines.extend(_format_debug_list("Invalid configured dirs", invalid_configured_dirs))
    lines.extend(_format_debug_list("DLL directories added at startup", _cairo_added_dirs))
    lines.extend(_format_debug_list("Directories containing libcairo-2.dll", dll_dirs))
    lines.extend(_format_debug_list("Relevant PATH entries", relevant_path_dirs))

    lines.extend([
        "",
        "Suggested fix:",
        "  1. Install a Cairo runtime if it is not already installed (for example GTK runtime).",
        "  2. In mcp.json, set both:",
        "     - CAIRO_DLL_DIRS=<folder containing libcairo-2.dll>",
        "     - PATH=<same folder>;%PATH%",
        "  3. Restart the image-service MCP server.",
        "",
        "Original loader error:",
        f"  {_cairosvg_error}",
    ])

    return "\n".join(lines)


def get_svg_support_error_message() -> str:
    """Return a user-facing error message describing why SVG support is unavailable."""
    if isinstance(_cairosvg_error, ModuleNotFoundError):
        return (
            "SVG support is unavailable because the Python package 'cairosvg' is not installed. "
            "Install the package and restart the server."
        )

    if sys.platform == "win32":
        return _get_windows_cairo_diagnostics()

    return (
        "SVG support is unavailable because CairoSVG could not load the system Cairo/libcairo library. "
        "Install Cairo and restart the server, or set CAIRO_DLL_DIRS on Windows."
    )


def get_cairosvg():
    """Load CairoSVG lazily so missing libcairo only disables SVG support."""
    global _cairosvg_module, _cairosvg_error

    if _cairosvg_module is not None:
        return _cairosvg_module

    if _cairosvg_error is not None:
        return None

    try:
        _cairosvg_module = importlib.import_module("cairosvg")
        return _cairosvg_module
    except Exception as exc:
        _cairosvg_error = exc
        logger.warning(
            "SVG support disabled because CairoSVG/libcairo could not be loaded: %s",
            exc,
        )
        return None

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

async def process_local_image(file_path: str, ctx: Context, svg_dpi: int = 150) -> Dict[str, Any]:
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
            svg_dpi = max(50, min(svg_dpi, 1200))
            logger.debug(f"SVG file detected: {file_path}, converting to PNG at {svg_dpi} DPI")
            cairosvg = get_cairosvg()
            if cairosvg is None:
                error_msg = get_svg_support_error_message()
                ctx.error(error_msg)
                logger.error("Skipping SVG %s: %s", file_path, error_msg)
                return {"path": file_path, "error": error_msg}
            try:
                with open(file_path, "rb") as f:
                    svg_data = f.read()

                # Prepend Emoji font and append CJK fallback fonts so special glyphs render,
                # while preserving the original font preference for normal text.
                text_data = svg_data.decode("utf-8", errors="ignore")
                emoji_pattern = re.compile(r"[\U0001F300-\U0001FAFF]")
                symbol_pattern = re.compile(r"[\u0391-\u03C9\u2206\u220F\u2211\u03BC]")

                def inject_font_family_attr(match: re.Match[str]) -> str:
                    quote = match.group(1)
                    font_value = match.group(2)
                    injected = f"{font_value}, 'Microsoft YaHei', 'PingFang SC', 'Arial Unicode MS'"
                    return f"font-family={quote}{injected}{quote}"

                def retag_text_node(match: re.Match[str]) -> str:
                    attrs = match.group(1)
                    content = match.group(2)
                    preferred_fonts = None

                    if emoji_pattern.search(content):
                        preferred_fonts = "'Segoe UI Emoji', 'Segoe UI Symbol'"
                    elif symbol_pattern.search(content):
                        preferred_fonts = "'Arial', 'Cambria Math', 'Segoe UI Symbol'"

                    if preferred_fonts is None:
                        return match.group(0)

                    def prepend_attr_font(attr_match: re.Match[str]) -> str:
                        quote = attr_match.group(1)
                        value = attr_match.group(2)
                        return f"font-family={quote}{preferred_fonts}, {value}{quote}"

                    def prepend_style_font(style_match: re.Match[str]) -> str:
                        # 修复 style 内 font-family 未被定向覆盖，导致 emoji/symbol 节点仍被 Arial 抢占而显示为方块。
                        return f"font-family: {preferred_fonts}, {style_match.group(1)}"

                    updated_attrs = re.sub(
                        r"font-family\s*=\s*([\"'])(.*?)\1",
                        prepend_attr_font,
                        attrs,
                        count=1,
                    )
                    updated_attrs = re.sub(
                        r"font-family:\s*([^;\"'\>\<\}]+)",
                        prepend_style_font,
                        updated_attrs,
                        count=1,
                    )
                    if updated_attrs == attrs:
                        updated_attrs = f'{attrs} font-family="{preferred_fonts}"'

                    return f"<text{updated_attrs}>{content}</text>"

                text_data = re.sub(
                    r"font-family\s*=\s*([\"'])(.*?)\1",
                    inject_font_family_attr,
                    text_data,
                )
                text_data = re.sub(
                    r"font-family:\s*([^;\"'\>\<\}]+)",
                    r"font-family: \1, 'Microsoft YaHei', 'PingFang SC', 'Arial Unicode MS'",
                    text_data,
                )
                text_data = re.sub(r"<text([^>]*)>(.*?)</text>", retag_text_node, text_data, flags=re.DOTALL)
                svg_data_with_fonts = text_data.encode("utf-8")

                png_data = cairosvg.svg2png(bytestring=svg_data_with_fonts, dpi=svg_dpi)
                logger.debug(f"Converted SVG to PNG: {len(png_data)} bytes")
                processed_image = await process_image_data(png_data, "png", file_path, ctx)
                if processed_image is None:
                    return {"path": file_path, "error": "Failed to process SVG image"}
                return {"path": file_path, "image": processed_image}
            except Exception as e:
                error_msg = f"Error converting SVG {file_path}: {str(e)}"
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

async def fetch_single_image(url: str, client: httpx.AsyncClient, ctx: Context) -> Dict[str, Any]:
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

async def process_images_async(image_sources: List[str], ctx: Context, svg_dpi: int = 150) -> List[Dict[str, Any]]:
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
            url_tasks = [fetch_single_image(url, client, ctx) for url in urls]
            url_results = await asyncio.gather(*url_tasks)
            results.extend(url_results)
    
    # Process local files if any
    if local_paths:
        logger.debug(f"Processing {len(local_paths)} local files")
        local_tasks = [process_local_image(path, ctx, svg_dpi=svg_dpi) for path in local_paths]
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
async def fetch_images(image_sources: List[str], ctx: Context, svg_dpi: int = 150):
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
        svg_dpi: DPI for SVG to PNG conversion. Higher values produce clearer images but larger files. Default: 150.
        
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
        results = await process_images_async(image_sources, ctx, svg_dpi=svg_dpi)
        
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
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
