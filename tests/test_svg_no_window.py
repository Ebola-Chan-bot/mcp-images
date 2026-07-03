"""
SVG 渲染无窗口回归测试（带高频闪现检测）。

渲染前启动后台线程，每 ~3ms 用 EnumWindows 轮询 Edge 的
Chrome_WidgetWin 窗口坐标。一旦窗口 left 在 -30000..10000 范围
（即在屏幕内），记录为"闪现"。渲染完成后统一报告。

用法:
    python tests/test_svg_no_window.py [SVG文件路径]
"""
import os
import sys
import time
import ctypes
import socket
import threading
import tempfile
import subprocess
from pathlib import Path
from ctypes import wintypes
from typing import List, Dict, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

脚本路径 = os.path.join(os.path.dirname(os.path.dirname(__file__)), "SVG转PNG渲染器.py")
import importlib.util as _iu
_spec = _iu.spec_from_file_location("svg_renderer", 脚本路径)
_svg = _iu.module_from_spec(_spec)
_spec.loader.exec_module(_svg)

import mcp_image

user32 = ctypes.windll.user32


class 闪现检测器:
    """后台高频轮询，追踪指定 PID 的 Chrome_WidgetWin 窗口位置。"""

    def __init__(self, 目标PID: int):
        self.目标PID = 目标PID
        self.停止 = threading.Event()
        self.闪现记录: List[Dict] = []
        self._锁 = threading.Lock()

    def _轮询(self):
        计数 = 0

        def cb(hwnd, lparam):
            nonlocal 计数
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != self.目标PID:
                    return True
                cn = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cn, 256)
                if not cn.value.startswith("Chrome_WidgetWin_1"):
                    return True
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                # 只报告非零尺寸且在屏幕内的主窗口（这才是肉眼可见的白方块）
                宽 = r.right - r.left
                高 = r.bottom - r.top
                if 宽 > 10 and 高 > 10 and r.left > -30000 and r.left < 10000:
                    计数 += 1
                    with self._锁:
                        self.闪现记录.append({
                            "时间": time.time(),
                            "HWND": hwnd,
                            "类名": cn.value,
                            "左": r.left, "上": r.top,
                            "右": r.right, "下": r.bottom,
                        })
            except Exception:
                pass
            return True

        W = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(W(cb), 0)
        return 计数

    def 运行(self):
        while not self.停止.is_set():
            self._轮询()
            self.停止.wait(0.003)

    def 启动(self) -> threading.Thread:
        t = threading.Thread(target=self.运行, daemon=True)
        t.start()
        return t

    def 结果(self) -> List[Dict]:
        with self._锁:
            return list(self.闪现记录)


def 空闲端口() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def 获取Edge进程树PID(根PID: int) -> Set[int]:
    pids = {根PID}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -match 'svg-cdp' }} | "
             f"Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=10, encoding="utf-8",
        )
        for line in r.stdout.strip().splitlines():
            if line.strip().isdigit():
                pids.add(int(line.strip()))
    except Exception:
        pass
    return pids


def main() -> None:
    svg_path = Path.home() / "Documents" / "MATLAB" / "Transfer-learning" / "中文图" / "中文图31A.svg"
    if len(sys.argv) > 1:
        svg_path = Path(sys.argv[1])

    print(f"SVG: {svg_path}  存在: {svg_path.is_file()}")

    # 准备渲染参数
    浏览器 = mcp_image.解析SVG浏览器路径()
    字体 = mcp_image.解析表情字体路径()
    svg_data = svg_path.read_bytes()
    宽, 高 = mcp_image._获取SVG目标尺寸(svg_data, 300)
    端口 = 空闲端口()
    工作目录 = tempfile.mkdtemp(prefix="svg-cdp-test-", dir=mcp_image.TEMP_DIR)
    配置目录 = os.path.join(工作目录, "profile")
    svg_text = svg_data.decode("utf-8", errors="replace")
    html = mcp_image._构建SVG包装HTML(svg_text, 宽, 高, 字体)
    html_path = os.path.join(工作目录, "wrapper.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    url = Path(html_path).resolve().as_uri()

    # 启动 Edge
    print("\n===== 启动 Edge =====")
    edge = subprocess.Popen(
        _svg.构建Edge命令(浏览器, 端口, 配置目录),
        **_svg.构建浏览器启动参数(),
    )
    print(f"  Edge PID: {edge.pid}")

    # 立即启动高频监控（在隐藏 API 之前）
    检测器 = 闪现检测器(edge.pid)
    监控线程 = 检测器.启动()
    time.sleep(0.01)  # 让监控线程先跑起来

    print("===== 高频监控已启动 (~3ms 轮询) =====")

    try:
        png = _svg.内联CDP渲染(浏览器, url, 宽, 高, 端口, 配置目录)
        print(f"  PNG: {len(png)} 字节")
    except Exception:
        edge.kill()
        检测器.停止.set()
        监控线程.join(timeout=2)
        raise

    # 渲染后继续监控 1 秒（覆盖 load 事件后创建的新窗口）
    time.sleep(1)

    检测器.停止.set()
    监控线程.join(timeout=2)

    闪现 = 检测器.结果()
    pids = 获取Edge进程树PID(edge.pid)

    # 报告
    print(f"\n===== 结果 =====")
    print(f"  Edge PID: {edge.pid} alive={edge.poll() is None}")
    print(f"  进程树: {len(pids)} 个进程")
    print(f"  监控期间在屏幕内捕获 Chrome_WidgetWin: {len(闪现)} 次")

    if 闪现:
        # 按 HWND 去重
        seen = {}
        for f in 闪现:
            hwnd = f["HWND"]
            if hwnd not in seen:
                seen[hwnd] = f
        print(f"\n  *** FAIL: {len(seen)} 个窗口在屏幕内闪现! ***")
        for hwnd, f in sorted(seen.items()):
            print(f"    HWND=0x{hwnd:08X} class={f['类名']} "
                  f"rect=({f['左']},{f['上']})-({f['右']},{f['下']})")
    else:
        print(f"\n  PASS: 窗口从未出现在屏幕内")

    print(f"\n  手动杀进程: taskkill /pid {edge.pid} /f /t")
    print(f"  Edge alive: {edge.poll() is None}")


if __name__ == "__main__":
    main()
