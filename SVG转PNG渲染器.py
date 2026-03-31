#!/usr/bin/env python3

import base64
import json
import os
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from websocket import WebSocketTimeoutException, create_connection


def 睡眠毫秒(毫秒数: int) -> None:
    time.sleep(毫秒数 / 1000)


def 等待JSON(URL地址: str, timeout_ms: int = 15000):
    截止时间 = time.time() + (timeout_ms / 1000)
    while time.time() < 截止时间:
        try:
            with urllib.request.urlopen(URL地址) as 响应:
                if 响应.status == 200:
                    return json.loads(响应.read().decode("utf-8"))
        except urllib.error.URLError:
            pass
        睡眠毫秒(200)
    raise RuntimeError(f"等待 {URL地址} 返回 JSON 超时")


def 写入状态文件(状态文件路径: str, 阶段: str, **附加信息) -> None:
    状态 = {
        "时间戳": time.time(),
        "阶段": 阶段,
    }
    状态.update(附加信息)
    with open(状态文件路径, "w", encoding="utf-8") as 文件句柄:
        json.dump(状态, 文件句柄, ensure_ascii=False)


def 调试端点可达(端口: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{端口}/json/version", timeout=1) as 响应:
            return 响应.status == 200
    except Exception:
        return False


class CDP客户端:
    def __init__(self, WebSocket地址: str):
        self.ws = create_connection(WebSocket地址, timeout=5)
        self.next_id = 1
        self.events = []

    def 发送(self, 方法名: str, 参数=None, timeout_ms: int = 15000, 心跳间隔_ms: int = 0, 心跳回调=None):
        消息编号 = self.next_id
        self.next_id += 1
        负载 = {
            "id": 消息编号,
            "method": 方法名,
            "params": 参数 or {},
        }
        self.ws.send(json.dumps(负载))

        截止时间 = time.time() + (timeout_ms / 1000)
        下次心跳时间 = time.time() + (心跳间隔_ms / 1000) if 心跳间隔_ms > 0 else None
        self.ws.settimeout(0.5)

        while time.time() < 截止时间:
            try:
                消息 = json.loads(self.ws.recv())
            except WebSocketTimeoutException:
                if 心跳回调 is not None and 下次心跳时间 is not None and time.time() >= 下次心跳时间:
                    # 修复大 SVG 截图阶段只能盲目等待单次超时，无法判断是慢渲染还是已卡死的问题。
                    心跳回调()
                    下次心跳时间 = time.time() + (心跳间隔_ms / 1000)
                continue

            if "id" in 消息:
                if 消息["id"] != 消息编号:
                    continue
                if "error" in 消息:
                    raise RuntimeError(消息["error"].get("message", "CDP 请求失败"))
                return 消息.get("result", {})
            self.events.append(消息)

        raise RuntimeError(f"等待 CDP 方法 {方法名} 返回超时")

    def 等待事件(self, 方法名: str, timeout_ms: int = 15000):
        截止时间 = time.time() + (timeout_ms / 1000)
        while time.time() < 截止时间:
            for 索引, 事件 in enumerate(self.events):
                if 事件.get("method") == 方法名:
                    return self.events.pop(索引).get("params", {})

            self.ws.settimeout(0.2)
            try:
                消息 = json.loads(self.ws.recv())
                if "id" not in 消息:
                    self.events.append(消息)
            except Exception:
                pass
        raise RuntimeError(f"等待事件 {方法名} 超时")

    def 关闭(self) -> None:
        self.ws.close()


def 读取PNG尺寸(文件路径: str) -> dict:
    with open(文件路径, "rb") as 文件句柄:
        文件头 = 文件句柄.read(24)
    宽度, 高度 = struct.unpack(">II", 文件头[16:24])
    return {"宽度": 宽度, "高度": 高度}


def 构建浏览器启动参数() -> dict:
    启动参数 = {
        # 修复浏览器继承 MCP stdio 管道句柄后在 Windows 上出现卡住的问题。
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        启动信息 = subprocess.STARTUPINFO()
        启动信息.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        启动参数["startupinfo"] = 启动信息
        启动参数["creationflags"] = subprocess.CREATE_NO_WINDOW
    return 启动参数


def main() -> int:
    参数列表 = sys.argv[1:]
    if len(参数列表) != 9:
        raise RuntimeError(
            "用法: python svg_cdp_renderer.py <浏览器路径> <页面URL> <输出PNG路径> <报告路径> <状态路径> <配置目录> <端口> <宽度> <高度>"
        )

    浏览器路径, 页面URL, 输出路径, 报告路径, 状态路径, 配置目录, 端口文本, 宽度文本, 高度文本 = 参数列表
    端口 = int(端口文本)
    宽度 = int(宽度文本)
    高度 = int(高度文本)

    Path(配置目录).mkdir(parents=True, exist_ok=True)
    Path(输出路径).parent.mkdir(parents=True, exist_ok=True)
    Path(报告路径).parent.mkdir(parents=True, exist_ok=True)
    Path(状态路径).parent.mkdir(parents=True, exist_ok=True)
    写入状态文件(状态路径, "准备启动浏览器", 端口=端口, 宽度=宽度, 高度=高度)

    浏览器进程 = subprocess.Popen(
        [
            浏览器路径,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            # 修复新版 Chromium 默认拒绝本地 CDP WebSocket 握手导致 SVG 渲染长时间无返回的问题。
            "--remote-allow-origins=*",
            f"--remote-debugging-port={端口}",
            f"--user-data-dir={配置目录}",
            "about:blank",
        ],
        **构建浏览器启动参数(),
    )

    客户端 = None
    try:
        目标列表 = 等待JSON(f"http://127.0.0.1:{端口}/json/list")
        写入状态文件(状态路径, "浏览器调试端点就绪", 目标数量=len(目标列表))
        页面目标 = next((target for target in 目标列表 if target.get("type") == "page"), None)
        if not 页面目标:
            raise RuntimeError("没有可用的页面目标")

        客户端 = CDP客户端(页面目标["webSocketDebuggerUrl"])
        写入状态文件(状态路径, "已连接CDP")
        客户端.发送("Page.enable")
        客户端.发送("Runtime.enable")
        # 修复 SVG 渲染强制依赖 Node.js 才能驱动 CDP 的问题。
        客户端.发送(
            "Emulation.setDefaultBackgroundColorOverride",
            {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
        )

        客户端.发送("Page.navigate", {"url": 页面URL})
        客户端.等待事件("Page.loadEventFired")
        写入状态文件(状态路径, "页面加载完成")
        睡眠毫秒(300)

        布局结果 = 客户端.发送(
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => {"
                    "const svg = document.querySelector('svg');"
                    "if (!svg) { throw new Error('包装页中不存在 SVG 元素'); }"
                    "document.documentElement.style.margin = '0';"
                    f"document.documentElement.style.width = '{宽度}px';"
                    f"document.documentElement.style.height = '{高度}px';"
                    "document.documentElement.style.background = 'transparent';"
                    "document.body.style.margin = '0';"
                    f"document.body.style.width = '{宽度}px';"
                    f"document.body.style.height = '{高度}px';"
                    "document.body.style.background = 'transparent';"
                    "svg.style.display = 'block';"
                    f"svg.style.width = '{宽度}px';"
                    f"svg.style.height = '{高度}px';"
                    "svg.style.overflow = 'visible';"
                    "const rect = svg.getBoundingClientRect();"
                    "return JSON.stringify({ x: rect.x, y: rect.y, width: rect.width, height: rect.height });"
                    "})()"
                ),
                "returnByValue": True,
            },
        )
        渲染矩形 = json.loads(布局结果["result"]["value"])
        写入状态文件(状态路径, "布局计算完成", 矩形=渲染矩形)

        客户端.发送(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 宽度,
                "height": 高度,
                "deviceScaleFactor": 1,
                "mobile": False,
                "scale": 1,
            },
        )

        截图开始时间 = time.time()

        def 截图心跳() -> None:
            已等待秒数 = round(time.time() - 截图开始时间, 2)
            写入状态文件(
                状态路径,
                "等待截图返回",
                已等待秒数=已等待秒数,
                浏览器仍在运行=浏览器进程.poll() is None,
                调试端点可达=调试端点可达(端口),
            )

        截图结果 = 客户端.发送(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": True,
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": 宽度,
                    "height": 高度,
                    "scale": 1,
                },
            },
            timeout_ms=180000,
            心跳间隔_ms=2000,
            心跳回调=截图心跳,
        )
        写入状态文件(状态路径, "截图完成")

        with open(输出路径, "wb") as 文件句柄:
            文件句柄.write(base64.b64decode(截图结果["data"]))

        报告内容 = {
            "浏览器路径": 浏览器路径,
            "页面URL": 页面URL,
            "输出路径": 输出路径,
            "目标CSS像素": {"宽度": 宽度, "高度": 高度},
            "调整后矩形": 渲染矩形,
            "实际PNG尺寸": 读取PNG尺寸(输出路径),
        }
        with open(报告路径, "w", encoding="utf-8") as 文件句柄:
            json.dump(报告内容, 文件句柄, indent=2, ensure_ascii=False)
        写入状态文件(状态路径, "渲染完成", 报告路径=报告路径)
        return 0
    finally:
        if 客户端 is not None:
            客户端.关闭()
        # 修复浏览器进程尚未完全退出时临时 profile 目录被立刻删除，导致 Windows 文件锁报错的问题。
        if 浏览器进程.poll() is None:
            浏览器进程.kill()
        try:
            浏览器进程.wait(timeout=5)
        except subprocess.TimeoutExpired:
            浏览器进程.kill()
            浏览器进程.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)