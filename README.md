# MCP Server - Image
A Model Context Protocol (MCP) server that provides tools for fetching and processing images from URLs, local file paths (including SVG files). The server includes a tool called fetch_images that returns images in a format suitable for LLMs.

## Support Us

If you find this project helpful and would like to support future projects, consider buying us a coffee! Your support helps us continue building innovative AI solutions.

<a href="https://www.buymeacoffee.com/blazzmocompany"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=blazzmocompany&button_colour=40DCA5&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00"></a>

Your contributions go a long way in fueling our passion for creating intelligent and user-friendly applications.

## Table of Contents
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the Server](#running-the-server)
  - [Direct Method](#1-direct-method)
  - [Configure for Windsurf/Cursor](#2-configure-for-windsurfcursor)
- [Available Tools](#available-tools)
  - [Usage Examples](#usage-examples)
- [Debugging](#debugging)
- [Contributing](#contributing)
- [License](#license)

## Features
- Fetch images from URLs (http/https)
- Load images from local file paths
- **SVG support** — automatically converts SVG files to PNG via `svglib` + `reportlab`, with configurable DPI
- Specialized handling for large local images
- Automatic image compression for large images (>1MB)
- Parallel processing of multiple images
- Proper MIME type mapping for different file extensions
- Comprehensive error handling and logging
## Prerequisites
- Python 3.10+
## Installation
This server is designed to be used without cloning the repository. You can use it directly via `uvx` (recommended) or install it via `pip`.

### Option A: Using uvx (Zero-install, Recommended)
If you have [uv](https://docs.astral.sh/uv/) installed, you can run the server directly:
```bash
uvx MCP读图
```

### Option B: Using pip (User-level or Global)
You can install the package directly from PyPI (once published):
```bash
pip install MCP读图
```
Then start the server using the compiled executable:
```bash
mcp-image-server
```

## Running the Server
The easiest way to use this is to configure it in your MCP client (like Cursor, Windsurf, or Claude Desktop).

### Configure for Windsurf/Cursor
#### Windsurf
To add this MCP server to Windsurf, edit the configuration file at `~/.codeium/windsurf/mcp_config.json`:
```json
{
  "mcpServers": {
    "image": {
      "command": "uvx",
      "args": ["MCP读图"]
    }
  }
}
```

#### Cursor
To add this MCP server to Cursor:
1. Open Cursor and go to *Settings* (Navbar → Cursor Settings)
2. Navigate to *Features* → *MCP Servers*
3. Click on + Add New MCP Server
4. Enter the following configuration:
```json
{
  "mcpServers": {
    "image": {
      "command": "uvx",
      "args": ["MCP读图"]
    }
  }
}
```

#### VS Code (Claude Dev)
Add to your user or workspace `mcp.json` (settings):
```json
// Using uvx (Recommended):
{
  "mcpServers": {
    "image-service": {
      "command": "uvx",
      "args": ["MCP读图"]
    }
  }
}

// Or using system Python if installed via PIP:
{
  "mcpServers": {
    "image-service": {
      "command": "mcp-image-server",
      "args": []
    }
  }
}
```

## Available Tools
The server provides the following tools:

**fetch_images**: Fetch and process images from URLs or local file paths

Parameters:
- `image_sources` (required): List of URLs or file paths to images (including `.svg` files)
- `svg_dpi` (optional, default: `150`): DPI for SVG to PNG conversion. Higher values produce clearer images but larger files.

Returns:
- List of processed Image objects suitable for LLM consumption

### Usage Examples
You can now use commands like:

- "Fetch these images: [list of URLs or file paths]"
- "Load and process this local image: [file_path]"

#### Examples
```
# URL-only test
[
  "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Chocolate_%28blue_background%29.jpg/400px-Chocolate_%28blue_background%29.jpg",
  "https://imgs.search.brave.com/Sz7BdlhBoOmU4wZjnUkvgestdwmzOzrfc3GsiMr27Ik/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWdj/ZG4uc3RhYmxlZGlm/ZnVzaW9ud2ViLmNv/bS8yMDI0LzEwLzE4/LzJmOTY3NTViLTM0/YmQtNDczNi1iNDRh/LWJlMTVmNGM5MDBm/My5qcGc",
  "https://shigacare.fukushi.shiga.jp/mumeixxx/img/main.png"
]

# Mixed URL and local file test
[
  "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Chocolate_%28blue_background%29.jpg/400px-Chocolate_%28blue_background%29.jpg",
  "C:\\Users\\username\\Pictures\\image1.jpg",
  "https://imgs.search.brave.com/Sz7BdlhBoOmU4wZjnUkvgestdwmzOzrfc3GsiMr27Ik/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWdj/ZG4uc3RhYmxlZGlm/ZnVzaW9ud2ViLmNv/bS8yMDI0LzEwLzE4/LzJmOTY3NTViLTM0/YmQtNDczNi1iNDRh/LWJlMTVmNGM5MDBm/My5qcGc",
  "C:\\Users\\username\\Pictures\\image2.jpg"
]
```

## Debugging
If you encounter any issues:

1. Check that all dependencies are installed correctly
2. Verify that the server is running and listening for connections
3. For local image loading issues, ensure the file paths are correct and accessible
4. For "Unsupported image type" errors, verify the content type handling
5. Look for any error messages in the server output
## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.