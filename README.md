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
- **SVG support** — automatically converts SVG files to PNG via `cairosvg`, with configurable DPI
- Specialized handling for large local images
- Automatic image compression for large images (>1MB)
- Parallel processing of multiple images
- Proper MIME type mapping for different file extensions
- Comprehensive error handling and logging
## Prerequisites
- Python 3.10+
- On Windows: a Cairo DLL must be discoverable at runtime. Common sources include GTK3 Runtime, Tesseract-OCR, or Balabolka. See [Environment Variables](#environment-variables) if the default search paths don't match your setup.

## Environment Variables

| Variable | Description |
|---|---|
| `CAIRO_DLL_DIRS` | **(Windows only)** `os.pathsep`-separated list of directories containing `libcairo-2.dll`. When set, these directories are added to the DLL search path. When not set, the program falls back to a built-in list of common locations (`GTK3-Runtime`, `Tesseract-OCR`, `Balabolka`). |

## Installation
1. Clone this repository
2. Install dependencies using one of the following methods:

### Option A: Using uv (with virtual environment)
`uv run` will automatically create a virtual environment and install everything from `pyproject.toml`:
```bash
uv run python mcp_image.py
```

### Option B: Using pip (user-level, no virtual environment)
```bash
pip install httpx "mcp[cli]" pillow cairosvg
```
Then run directly with system Python:
```bash
python mcp_image.py
```
## Running the Server
There are two ways to run the MCP server:

### 1. Direct Method
To start the MCP server directly:

```bash
# With uv (auto venv):
uv run python mcp_image.py

# Or with user-level pip:
python mcp_image.py
```
### 2. Configure for Windsurf/Cursor
#### Windsurf
To add this MCP server to Windsurf:

1. Edit the configuration file at ~/.codeium/windsurf/mcp_config.json
2. Add the following configuration:
```json
{
  "mcpServers": {
    "image": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-image", "run", "mcp_image.py"]
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
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-image", "run", "mcp_image.py"]
    }
  }
}
```
#### VS Code
Add to your user or workspace `mcp.json` (settings):
```json
// Using uv (auto venv):
{
  "servers": {
    "image-service": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-image", "python", "mcp_image.py"]
    }
  }
}

// Or using system Python (user-level pip):
{
  "servers": {
    "image-service": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/mcp-image/mcp_image.py"],
      "env": { "PYTHONUNBUFFERED": "1" }
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
- A list with one entry per item in `image_sources`. Each entry is either:
  - a processed Image object suitable for LLM consumption (on success), or
  - a human-readable error string describing why that particular image could not be fetched or processed (on failure).

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