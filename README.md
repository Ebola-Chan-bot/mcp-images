# MCP 图片服务

这是一个基于 Model Context Protocol（MCP）的图片读取服务，用于从网络地址或本地路径读取图片，并将结果以适合大语言模型消费的格式返回。项目支持常见位图格式，也支持将 SVG 自动转换为 PNG 后再处理。

## 功能特性

- 支持读取 HTTP/HTTPS 图片
- 支持读取本地图片文件
- 支持 SVG 自动转 PNG
- 支持批量并发处理多张图片
- 对大图自动压缩，降低传输体积
- 对错误进行显式返回，便于 Agent 诊断问题
- 在 Windows 上为 CairoSVG 提供更稳妥的字体与 DLL 兼容策略

## 运行要求

- Python 3.10 及以上
- 如果在 Windows 上运行，并且 Cairo 相关 DLL 不在系统默认搜索路径中，需要配置 `CAIRO_DLL_DIRS`

说明：如果系统中缺少 Cairo / libcairo，本服务仍可正常处理 PNG、JPG 等非 SVG 图片，但会自动禁用 SVG 转换功能，并在返回结果和日志中给出明确错误信息。

## 安装方式

推荐直接通过 GitHub 仓库使用 `uvx` 运行，或安装到当前用户环境，无需手动创建虚拟环境。

### 方式一：使用 uvx

安装好 [uv](https://docs.astral.sh/uv/) 后，可直接运行：

```bash
uvx --from git+https://github.com/Ebola-Chan-bot/mcp-images mcp-image-server
```

### 方式二：使用 pip 安装到用户环境

```bash
pip install --user git+https://github.com/Ebola-Chan-bot/mcp-images
```

安装完成后可直接启动：

```bash
mcp-image-server
```

## 环境变量

| 变量名 | 说明 |
| --- | --- |
| `CAIRO_DLL_DIRS` | 仅 Windows 使用。值为多个目录组成的列表，目录之间使用 `os.pathsep` 分隔。程序会把这些目录加入 DLL 搜索路径，用于定位 `libcairo-2.dll` 等 Cairo 依赖。 |

说明：如果未设置 `CAIRO_DLL_DIRS`，程序会尝试若干常见安装目录作为后备方案。
如果在 Windows 下仍然出现 SVG/Cairo 加载失败，通常还需要在 MCP 配置里同时把同一目录加入 `PATH`，否则即使 `libcairo-2.dll` 主文件存在，它依赖的其他 DLL 也可能无法被解析。

## MCP 客户端配置

### Windsurf

将以下内容加入 `~/.codeium/windsurf/mcp_config.json`：

```json
{
  "mcpServers": {
    "image": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Ebola-Chan-bot/mcp-images", "mcp-image-server"]
    }
  }
}
```

### Cursor

在 Cursor 的 MCP Servers 配置中加入：

```json
{
  "mcpServers": {
    "image": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Ebola-Chan-bot/mcp-images", "mcp-image-server"]
    }
  }
}
```

### VS Code

在用户级或工作区级 `mcp.json` 中加入：

```json
{
  "servers": {
    "image-service": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Ebola-Chan-bot/mcp-images", "mcp-image-server"]
    }
  }
}
```

如果你已经通过 `pip install --user` 安装，也可以直接使用可执行入口：

```json
{
  "servers": {
    "image-service": {
      "command": "mcp-image-server",
      "args": []
    }
  }
}
```

## 可用工具

### fetch_images

读取并处理网络图片或本地图片。

参数：

- `image_sources`：必填，字符串列表。每个元素可以是 URL 或本地文件路径。
- `svg_dpi`：可选，默认值为 `150`。用于控制 SVG 转 PNG 时的 DPI，数值越高，清晰度越高，但内存和输出体积也可能越大。

返回值：

- 返回一个与 `image_sources` 顺序一致的列表。
- 每一项要么是成功处理后的 `Image` 对象，要么是对应图片的错误字符串。

## 使用示例

```text
读取这些图片：
[
  "https://example.com/a.png",
  "C:/Users/username/Pictures/example.svg"
]
```

## 调试说明

如果运行失败，建议按以下顺序排查：

1. 确认依赖已经正确安装
2. 确认当前 Python 为用户环境或系统环境，而不是失效的虚拟环境
3. 确认本地图片路径存在且有读取权限
4. 在 Windows 上检查 Cairo DLL 是否可被找到
5. 查看程序标准错误输出和 `data` 目录下的日志文件

## 许可证

本项目使用 MIT 许可证。详见 [LICENSE](LICENSE)。
