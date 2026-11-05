"""Render an SVG from stdin to PNG on stdout with a headless Chromium browser.

The main server runs this script in a separate process so it can stop a slow render.
The browser uses an isolated temporary profile and CDP screenshots, matching the
rendering method used by the archived mcp-images repository.

Usage: python mcp_image_svg.py MAX_DIMENSION TARGET_DIMENSION MAX_PIXELS CPU_SECONDS [DPI] [CROP_BOX_JSON] [EMOJI_FONT_PATH] [BROWSER_PATH]
Exit status 3 means Chromium or the CDP client dependency is unavailable.
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image

CAIRO_MISSING = 3
EMBEDDED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")
MEMORY_LIMIT = 1_500_000_000
MAX_FONT_BYTES = 30_000_000
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF]")
SYMBOL_PATTERN = re.compile(r"[\u0391-\u03C9\u2206\u220F\u2211]")
BROWSER_CANDIDATES = {
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


class RenderError(Exception):
    """A reason the SVG cannot be rendered, reported as-is."""


def _wait_for_json(url: str, timeout_seconds: float = 10):
    """Poll a local Chromium debugging endpoint until it returns JSON."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)
    raise RenderError(f"timed out waiting for the Chromium endpoint: {url}")


class CDPClient:
    """A small synchronous Chrome DevTools Protocol WebSocket client."""

    def __init__(self, websocket_url: str):
        from websocket import create_connection

        self.websocket = create_connection(websocket_url, timeout=5)
        self.next_id = 1
        self.events = []

    def send(self, method: str, params: dict | None = None, timeout_seconds: float = 15) -> dict:
        from websocket import WebSocketTimeoutException

        message_id = self.next_id
        self.next_id += 1
        self.websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout_seconds
        self.websocket.settimeout(0.5)
        while time.monotonic() < deadline:
            try:
                message = json.loads(self.websocket.recv())
            except WebSocketTimeoutException:
                continue
            if "id" not in message:
                self.events.append(message)
                continue
            if message["id"] != message_id:
                continue
            if "error" in message:
                raise RenderError(message["error"].get("message", "CDP request failed"))
            return message.get("result", {})
        raise RenderError(f"timed out waiting for CDP method {method}")

    def wait_for_event(self, method: str, timeout_seconds: float = 15):
        from websocket import WebSocketTimeoutException

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for index, event in enumerate(self.events):
                if event.get("method") == method:
                    return self.events.pop(index).get("params", {})
            self.websocket.settimeout(0.2)
            try:
                message = json.loads(self.websocket.recv())
                if "id" not in message:
                    self.events.append(message)
            except WebSocketTimeoutException:
                pass
        raise RenderError(f"timed out waiting for CDP event {method}")

    def close(self) -> None:
        try:
            self.websocket.close()
        except Exception:
            pass


def find_browser(browser_path: str | None = None) -> str:
    """Resolve a Chromium executable from an explicit path, environment, or standard locations."""
    candidates = [browser_path, os.environ.get("MCP_IMAGE_BROWSER_PATH"), os.environ.get("CHROME_PATH")]
    candidates.extend(BROWSER_CANDIDATES.get(sys.platform, BROWSER_CANDIDATES["linux"]))
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.expanduser(candidate.strip().strip('"'))
        if os.path.isabs(expanded) or os.sep in expanded or (os.altsep and os.altsep in expanded):
            if os.path.isfile(expanded):
                return expanded
        else:
            resolved = shutil.which(expanded)
            if resolved:
                return resolved
    raise RenderError(
        "no Chromium browser was found; install Edge, Chrome, or Chromium, or set MCP_IMAGE_BROWSER_PATH to its executable"
    )


def browser_command(browser_path: str, port: int, profile_directory: str) -> list[str]:
    """Build a headless Chromium command equivalent to the archived renderer."""
    command = [
        browser_path,
        "--headless",
        "--window-position=-32000,-32000",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-features=Translate",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-default-browser-check",
        "--no-first-run",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_directory}",
        "about:blank",
    ]
    if sys.platform == "win32":
        command.insert(1, "--disable-gpu")
    return command


def browser_popen_options() -> dict:
    """Keep browser output out of the MCP stdio channel and hide its console on Windows."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        options["startupinfo"] = startup_info
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def render_with_cdp(port: int, width: int, height: int, data: bytes, timeout_seconds: float) -> bytes:
    """Load an inline SVG document in a Chromium tab and capture it with Page.captureScreenshot."""
    page_url = "data:text/html;charset=utf-8," + urllib.parse.quote_from_bytes(data)
    version = _wait_for_json(f"http://127.0.0.1:{port}/json/version", timeout_seconds)
    browser_websocket = version.get("webSocketDebuggerUrl", "")
    if not browser_websocket:
        raise RenderError("Chromium did not expose a browser WebSocket URL")

    browser = CDPClient(browser_websocket)
    try:
        target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    finally:
        browser.close()

    page_target = None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and page_target is None:
        try:
            targets = _wait_for_json(f"http://127.0.0.1:{port}/json/list", 2)
            page_target = next(
                (target for target in targets if target.get("id") == target_id and target.get("type") == "page" and target.get("webSocketDebuggerUrl")),
                None,
            )
        except RenderError:
            pass
        if page_target is None:
            time.sleep(0.1)
    if page_target is None:
        raise RenderError("could not find the Chromium rendering tab")

    client = CDPClient(page_target["webSocketDebuggerUrl"])
    try:
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
        client.send("Page.navigate", {"url": page_url})
        client.wait_for_event("Page.loadEventFired")
        client.send("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
            "scale": 1,
        })
        client.send("Runtime.evaluate", {
            "expression": (
                "(() => {"
                "const svg = document.querySelector('svg');"
                "if (!svg) { throw new Error('No SVG root element exists in the rendering document'); }"
                "document.documentElement.style.margin = '0';"
                "document.documentElement.style.background = 'transparent';"
                "document.body.style.margin = '0';"
                "document.body.style.background = 'transparent';"
                "svg.style.display = 'block';"
                f"svg.style.width = '{width}px';"
                f"svg.style.height = '{height}px';"
                "svg.style.overflow = 'visible';"
                "})()"
            ),
            "returnByValue": True,
        })
        screenshot = client.send("Page.captureScreenshot", {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        }, timeout_seconds)
        return base64.b64decode(screenshot["data"])
    finally:
        client.close()
        try:
            browser = CDPClient(browser_websocket)
            try:
                browser.send("Target.closeTarget", {"targetId": target_id}, 5)
            finally:
                browser.close()
        except Exception:
            pass


def _normalize_crop_box(crop_box: list[int] | None, image_width: int, image_height: int) -> tuple[int, int, int, int] | None:
    """Validate a crop box in [x, y, width, height] form and clamp it to the rendered image bounds."""
    if crop_box is None:
        return None
    if len(crop_box) != 4:
        raise RenderError(f"crop_box must contain four integers [x, y, width, height], got {crop_box}")
    try:
        x, y, width, height = (int(value) for value in crop_box)
    except (TypeError, ValueError) as error:
        raise RenderError(f"crop_box must contain four integers, got {crop_box}") from error
    if width <= 0 or height <= 0:
        raise RenderError("crop_box width and height must be positive")
    if x >= image_width or y >= image_height:
        raise RenderError(f"crop_box origin ({x}, {y}) is outside the {image_width}x{image_height} SVG rendering")
    x, y = max(0, x), max(0, y)
    return x, y, min(width, image_width - x), min(height, image_height - y)


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _replace_root_attribute(tag: str, name: str, value: str) -> str:
    pattern = rf'(\s{name}\s*=\s*)(["\'])[^"\']*\2'
    if re.search(pattern, tag):
        return re.sub(pattern, lambda match: match.group(1) + match.group(2) + value + match.group(2), tag, count=1)
    return tag[:-1] + f' {name}="{value}">'


def _rewrite_svg_crop(data: bytes, crop_box: tuple[int, int, int, int], full_width: int, full_height: int) -> bytes | None:
    """Rewrite the root viewBox and dimensions so only the requested vector region is rendered."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    root_match = re.search(r"<(?:[\w.-]+:)?svg\b[^>]*>", text)
    if not root_match:
        return None
    root_tag = root_match.group(0)
    viewbox_match = re.search(
        r"""viewBox\s*=\s*["']\s*([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)""",
        root_tag,
        re.IGNORECASE,
    )
    if not viewbox_match:
        return None
    try:
        view_x, view_y, view_width, view_height = (float(value) for value in viewbox_match.groups())
    except ValueError:
        return None
    if view_width <= 0 or view_height <= 0:
        return None
    x, y, width, height = crop_box
    scale_x, scale_y = view_width / full_width, view_height / full_height
    new_viewbox = " ".join(
        _format_number(value)
        for value in (view_x + x * scale_x, view_y + y * scale_y, width * scale_x, height * scale_y)
    )
    new_tag = _replace_root_attribute(root_tag, "viewBox", new_viewbox)
    new_tag = _replace_root_attribute(new_tag, "width", str(width))
    new_tag = _replace_root_attribute(new_tag, "height", str(height))
    return (text[:root_match.start()] + new_tag + text[root_match.end():]).encode("utf-8")


def _inject_svg_fonts(data: bytes, emoji_font_path: str | None) -> bytes:
    """Inject emoji and symbol fonts without permitting external SVG resources."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if not EMOJI_PATTERN.search(text) and not SYMBOL_PATTERN.search(text):
        return data

    font_declaration = ""
    emoji_fonts = "'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji'"
    if emoji_font_path:
        font_file = Path(emoji_font_path)
        try:
            font_data = font_file.read_bytes()
        except OSError as error:
            raise RenderError(f"could not read the emoji font: {error}") from error
        if len(font_data) > MAX_FONT_BYTES:
            raise RenderError(f"emoji font exceeds the {MAX_FONT_BYTES / 1_000_000:g} MB limit")
        font_type = {".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}.get(font_file.suffix.lower(), "font/ttf")
        font_url = f"data:{font_type};base64,{base64.b64encode(font_data).decode('ascii')}"
        font_declaration = f"<style type=\"text/css\">@font-face{{font-family:'MCP Emoji';src:url('{font_url}');}}</style>"
        emoji_fonts = "'MCP Emoji', " + emoji_fonts

    def rewrite_text_node(match: re.Match[str]) -> str:
        attributes, content = match.group(1), match.group(2)
        has_emoji = bool(EMOJI_PATTERN.search(content))
        if not has_emoji and not SYMBOL_PATTERN.search(content):
            return match.group(0)
        preferred_fonts = emoji_fonts if has_emoji else "'Segoe UI Symbol', 'Cambria Math', 'Arial Unicode MS'"
        new_attributes = re.sub(
            r"font-family\s*=\s*([\"'])(.*?)\1",
            lambda font: f"font-family={font.group(1)}{preferred_fonts}, {font.group(2)}{font.group(1)}",
            attributes,
            count=1,
        )
        new_attributes = re.sub(
            r"font-family\s*:\s*([^;\"'<>}]+)",
            lambda font: f"font-family: {preferred_fonts}, {font.group(1)}",
            new_attributes,
            count=1,
        )
        if new_attributes == attributes:
            new_attributes = f'{attributes} font-family="{preferred_fonts}"'
        return f"<text{new_attributes}>{content}</text>"

    text = re.sub(r"<text([^>]*)>(.*?)</text>", rewrite_text_node, text, flags=re.DOTALL)
    if font_declaration:
        root_match = re.search(r"<(?:[\w.-]+:)?svg\b[^>]*>", text)
        if root_match:
            text = text[:root_match.end()] + font_declaration + text[root_match.end():]
    return text.encode("utf-8")


def _root_uses_pixel_size(data: bytes) -> bool:
    root_match = re.search(rb"<(?:[\w.-]+:)?svg\b[^>]*>", data, re.IGNORECASE)
    if not root_match:
        return False
    root_tag = root_match.group(0)
    for attribute in (b"width", b"height"):
        match = re.search(rb"\s" + attribute + rb"\s*=\s*([\"'])(.*?)\1", root_tag, re.IGNORECASE)
        if not match or not re.fullmatch(rb"\s*[-+\d.]+(?:px)?\s*", match.group(2), re.IGNORECASE):
            return False
    return True


def _parse_svg_length(value: str | None, dpi: float) -> float | None:
    """Convert an SVG width or height with physical units to CSS pixels."""
    if not value:
        return None
    match = re.fullmatch(r"\s*([-+\d.]+)\s*([a-z%]*)\s*", value)
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).lower() or "px"
    factors = {"px": 1, "in": dpi, "cm": dpi / 2.54, "mm": dpi / 25.4, "pt": dpi / 72, "pc": dpi / 6}
    return number * factors[unit] if unit in factors else None


def _svg_dimensions(data: bytes, dpi: float) -> tuple[float, float]:
    """Read declared SVG dimensions without executing scripts or fetching resources."""
    from defusedxml import ElementTree

    root = ElementTree.fromstring(data)
    width = _parse_svg_length(root.attrib.get("width"), dpi)
    height = _parse_svg_length(root.attrib.get("height"), dpi)
    if width is not None and height is not None:
        return width, height
    viewbox = root.attrib.get("viewBox")
    if viewbox:
        parts = re.split(r"[\s,]+", viewbox.strip())
        if len(parts) == 4:
            return float(parts[2]) * dpi / 96, float(parts[3]) * dpi / 96
    raise RenderError("SVG has no usable width, height, or viewBox")


def _validate_embedded_images(data: bytes, max_pixels: int) -> None:
    """Reject embedded raster bombs before Chromium decodes them."""
    for match in re.finditer(rb"data:(image/(?:png|jpeg|gif|webp|bmp));base64,([A-Za-z0-9+/=\s]+)", data, re.IGNORECASE):
        try:
            content = base64.b64decode(match.group(2))
            with Image.open(BytesIO(content), formats=EMBEDDED_FORMATS):
                pass
        except Image.DecompressionBombError:
            raise RenderError(f"an embedded image is over {max_pixels / 1_000_000:g} megapixels") from None
        except Exception:
            raise RenderError("embedded images must be valid PNG, JPEG, GIF, WebP, or BMP data") from None
    if re.search(rb"(?:href|src)\s*=\s*[\"']\s*(?:https?:|file:)", data, re.IGNORECASE):
        raise RenderError("external image URLs and file paths are not allowed in SVGs")


def render(
    data: bytes,
    max_dimension: int,
    target: int,
    max_pixels: int,
    dpi: int | None = None,
    crop_box: list[int] | None = None,
    emoji_font_path: str | None = None,
    browser_path: str | None = None,
    timeout_seconds: float = 10,
) -> bytes:
    """Render the document through headless Chromium and return a PNG screenshot."""
    data = _inject_svg_fonts(data, emoji_font_path)
    _validate_embedded_images(data, max_pixels)
    width, height = _svg_dimensions(data, 96 if dpi is None else max(50, min(dpi, 1200)))
    if width <= 0 or height <= 0:
        raise RenderError("SVG has no width or height")
    if width * height > max_pixels:
        raise RenderError(f"SVG declares {width:.0f}x{height:.0f}px; at most {max_pixels / 1_000_000:g} megapixels are supported")

    longest = max(width, height)
    if dpi is None:
        scale = min(max(longest, target), max_dimension) / longest
    else:
        # Honor DPI for physical SVG units while preserving the pixel semantics of explicitly pixel-sized documents.
        requested_scale = 1.0 if _root_uses_pixel_size(data) else max(50, min(dpi, 1200)) / 96.0
        scale = min(requested_scale, max_dimension / longest)
    full_width, full_height = max(1, round(width * scale)), max(1, round(height * scale))
    normalized_crop = _normalize_crop_box(crop_box, full_width, full_height)
    if normalized_crop:
        rewritten_data = _rewrite_svg_crop(data, normalized_crop, full_width, full_height)
        if rewritten_data is not None:
            data = rewritten_data
            _, _, full_width, full_height = normalized_crop
            normalized_crop = None

    executable = find_browser(browser_path)
    with tempfile.TemporaryDirectory(prefix="mcp-image-chromium-") as profile_directory:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(browser_command(executable, port, profile_directory), **browser_popen_options())
        try:
            png = render_with_cdp(port, full_width, full_height, data, timeout_seconds)
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    if normalized_crop:
        x, y, crop_width, crop_height = normalized_crop
        with Image.open(BytesIO(png)) as image:
            cropped = image.crop((x, y, x + crop_width, y + crop_height))
            output = BytesIO()
            cropped.save(output, "PNG")
            return output.getvalue()
    return png


def limit_resources(cpu_seconds: int) -> None:
    """Cap memory and CPU time in the kernel, so the renderer stays bounded even if the server dies.

    Only POSIX has these limits, and macOS does not enforce the memory one.
    """
    try:
        import resource
    except ImportError:
        return
    for which, soft, hard in (
        (resource.RLIMIT_AS, MEMORY_LIMIT, MEMORY_LIMIT),
        (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1),
    ):
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            pass


def main() -> None:
    max_dimension, target, max_pixels, cpu_seconds = (int(arg) for arg in sys.argv[1:5])
    dpi = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
    crop_box = json.loads(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None
    emoji_font_path = sys.argv[7] if len(sys.argv) > 7 and sys.argv[7] else None
    browser_path = sys.argv[8] if len(sys.argv) > 8 and sys.argv[8] else None
    limit_resources(cpu_seconds)
    Image.MAX_IMAGE_PIXELS = max_pixels // 2
    warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
    try:
        png = render(sys.stdin.buffer.read(), max_dimension, target, max_pixels, dpi, crop_box, emoji_font_path, browser_path, max(2, cpu_seconds - 5))
    except RenderError as e:
        sys.exit(str(e))
    except MemoryError:
        sys.exit(f"SVG needs more than {MEMORY_LIMIT / 1e9:g} GB of memory to render")
    except Exception as e:
        sys.exit(f"{type(e).__name__}: {e}")
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
