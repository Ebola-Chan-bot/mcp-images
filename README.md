# MCP 图片服务

这是一个基于 Model Context Protocol (MCP) 的图片读取服务，用于把网络图片或本地图片转换成适合大模型消费的 `Image` 对象。项目支持常见位图格式，也支持把 SVG 通过 Chromium DevTools Protocol 渲染成 PNG 后再返回。

## 功能特性

- 支持 HTTP / HTTPS 图片
- 支持本地文件路径
- 支持 SVG 转 PNG
- 支持批量并发处理
- 对大图自动压缩，尽量控制返回体积
- 保留透明背景，避免透明图在压缩时丢失 alpha
- 支持为 SVG 文本显式注入彩色 emoji 字体
- 日志输出到 `data` 目录，便于排查问题

## 运行依赖

服务启动和 SVG 渲染依赖如下：

- Python 3.10 及以上
- 一个可用的 Chromium 内核浏览器，常见候选项包括：
  - Windows: Edge、Chrome、Chromium
  - macOS: Chrome、Edge、Chromium
  - Linux: google-chrome、microsoft-edge、chromium
- 可选的彩色 emoji 字体文件。如果系统默认字体探测失败，可以手动指定

说明：

- 普通 PNG、JPG、WebP 等位图不依赖浏览器渲染。
- SVG 渲染会调用 [SVG转PNG渲染器.py](SVG转PNG渲染器.py) 并通过浏览器截图生成 PNG，因此 Chromium 浏览器是 SVG 能否工作的关键前提。

## 推荐启动方式

推荐使用 uv 直接从 GitHub 的发布 tag 获取并运行，无需手动克隆仓库。

先安装 uv：

```bash
pip install --user uv
```

然后直接启动服务：

```bash
uvx --from git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0 mcp-image-server
```

如果你要确保服务使用指定浏览器或指定 emoji 字体，可以在启动时显式传入参数：

```bash
uvx --from git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0 mcp-image-server --浏览器路径 "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --表情字体路径 "C:\Windows\Fonts\seguiemj.ttf"
```

如果你希望把命令安装到本机，也可以使用：

```bash
uv tool install --from git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0 mcp-image-server
```

安装后可直接运行：

```bash
mcp-image-server
```

升级到新版本时，把命令中的 `@v1.0.0` 替换成新的发布 tag 即可。

本地开发或调试时，才需要在仓库目录中直接运行源码。

## 启动参数与环境变量

服务支持两类启动配置。

### 启动参数

```text
--浏览器路径 <浏览器可执行文件路径>
--表情字体路径 <emoji 字体文件路径>
```

### 环境变量

| 变量名 | 说明 |
| --- | --- |
| `浏览器路径` | 指定 Chromium 浏览器可执行文件路径 |
| `表情字体路径` | 指定彩色 emoji 字体路径 |

参数优先级如下：

1. `fetch_images` 工具调用时传入的 `浏览器路径` / `表情字体路径`
2. 服务进程启动参数 `--浏览器路径` / `--表情字体路径`
3. 环境变量
4. 默认探测路径

## VS Code 配置

在 VS Code 中，可以这样配置 MCP 服务：

```json
{
  "servers": {
    "image-service": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0", "mcp-image-server"]
    }
  },
  "inputs": []
}
```

如果浏览器或字体不在默认探测路径中，可以改成：

```json
{
  "servers": {
    "image-service": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0",
        "mcp-image-server",
        "--浏览器路径",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "--表情字体路径",
        "C:\\Windows\\Fonts\\seguiemj.ttf"
      ]
    }
  },
  "inputs": []
}
```

## 其他 MCP 客户端

其他 MCP 客户端可参考以下原则配置：

- `command` 使用 `uvx`
- `args` 使用 `--from git+https://github.com/Ebola-Chan-bot/mcp-images@v1.0.0 mcp-image-server`
- 如果需要，再额外追加 `--浏览器路径` 和 `--表情字体路径`

## 可用工具

### fetch_images

读取并处理网络图片或本地图片。

参数：

- `image_sources`：必填，字符串列表。每个元素可以是 URL 或本地文件路径
- `svg_dpi`：当 `image_sources` 中包含 SVG 时必填。控制 SVG 转 PNG 时的渲染 DPI
- `浏览器路径`：可选。单次调用覆盖服务默认浏览器路径
- `表情字体路径`：可选。单次调用覆盖服务默认 emoji 字体路径

返回值：

- 返回结果顺序与 `image_sources` 一致
- 每一项要么是成功处理后的 `Image` 对象，要么是错误字符串

## 使用示例

```text
读取这些图片：
[
  "https://example.com/a.png",
  "C:/Users/username/Pictures/example.svg"
]
```

如果你知道某个 SVG 需要特定浏览器或 emoji 字体，也可以在调用工具时单独传参。

## 调试说明

如果服务启动或 SVG 渲染失败，建议按这个顺序排查：

1. 确认 Python 依赖已经安装完成
2. 确认系统中存在可用的 Edge、Chrome 或 Chromium
3. 如果是 emoji 或字体显示异常，显式指定 `--表情字体路径`
4. 如果是某些机器的浏览器安装路径比较特殊，显式指定 `--浏览器路径`
5. 查看 `data` 目录中的日志文件

## 项目文件说明

- [mcp_image.py](mcp_image.py)：MCP 服务主程序
- [SVG转PNG渲染器.py](SVG转PNG渲染器.py)：通过 CDP 控制 Chromium 渲染 SVG 的辅助脚本
- [.vscode/mcp.json](.vscode/mcp.json)：VS Code MCP 配置示例

## 许可证

本项目使用 MIT 许可证。详见 [LICENSE](LICENSE)。
