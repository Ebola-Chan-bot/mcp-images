"""PyMuPDF 按需安装逻辑的回归测试；不会实际安装依赖。"""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_image


class PyMuPDF安装测试(unittest.TestCase):
    def setUp(self):
        self._环境补丁 = patch.dict(os.environ, {}, clear=True)
        self._环境补丁.start()

    def tearDown(self):
        self._环境补丁.stop()

    def test_uv使用当前解释器并优先备用镜像(self):
        with (
            patch("mcp_image.shutil.which", return_value="uv"),
            patch("subprocess.call", side_effect=[2, 0]) as 调用,
        ):
            mcp_image._通过包管理器安装pymupdf(优先备用镜像=True)

        命令列表 = [调用参数.args[0] for 调用参数 in 调用.call_args_list]
        self.assertEqual(len(命令列表), 2)
        self.assertEqual(
            命令列表[0][:5],
            ["uv", "pip", "install", "--python", sys.executable],
        )
        self.assertEqual(
            命令列表[0][5:7],
            ["--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        )
        self.assertEqual(命令列表[0][-1], "pymupdf>=1.24.0")
        self.assertEqual(
            命令列表[1],
            ["uv", "pip", "install", "--python", sys.executable, "pymupdf>=1.24.0"],
        )

    def test自定义uv软件源不会被默认源覆盖(self):
        自定义源 = "https://packages.example.com/simple"
        with (
            patch.dict(os.environ, {"UV_DEFAULT_INDEX": 自定义源}),
            patch("mcp_image.shutil.which", return_value="uv"),
            patch("subprocess.call", return_value=0) as 调用,
        ):
            mcp_image._通过包管理器安装pymupdf()

        调用.assert_called_once_with([
            "uv", "pip", "install", "--python", sys.executable,
            "--index-url", 自定义源, "pymupdf>=1.24.0",
        ])

    def test没有uv时回退到已有pip(self):
        with (
            patch("mcp_image.shutil.which", return_value=None),
            patch("mcp_image.importlib.util.find_spec", return_value=object()),
            patch("subprocess.call", return_value=0) as 调用,
        ):
            mcp_image._通过包管理器安装pymupdf()

        调用.assert_called_once_with([
            sys.executable, "-m", "pip", "install", "pymupdf>=1.24.0",
        ])

    def test没有可用安装器时给出明确错误(self):
        with (
            patch("mcp_image.shutil.which", return_value=None),
            patch("mcp_image.importlib.util.find_spec", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "没有 pip"):
                mcp_image._通过包管理器安装pymupdf()

    def test_wheel解压到解释器purelib目录(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            wheel路径 = os.path.join(临时目录, "pymupdf-test.whl")
            目标目录 = os.path.join(临时目录, "site-packages")
            with zipfile.ZipFile(wheel路径, "w") as wheel文件:
                wheel文件.writestr("fitz/__init__.py", "# test\n")

            with patch("sysconfig.get_path", return_value=目标目录) as 获取路径:
                self.assertTrue(mcp_image._安装本地wheel(wheel路径))

            获取路径.assert_called_once_with("purelib")
            self.assertTrue(os.path.isfile(os.path.join(目标目录, "fitz", "__init__.py")))


if __name__ == "__main__":
    unittest.main()
