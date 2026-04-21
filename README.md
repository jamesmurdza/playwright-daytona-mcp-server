# Daytona Playwright MCP Server

An MCP (Model Context Protocol) server that lets you control a full Chromium browser running inside a [Daytona](https://daytona.io) cloud sandbox. Use it with Claude Code, Claude Desktop, or any MCP-compatible client to browse the web, take screenshots, fill forms, and more.

## Features

- **Full Chromium Browser**: Runs a real Chromium instance (not headless) in a virtual display
- **Cloud Sandbox**: Browser runs securely in a Daytona sandbox, isolated from your local machine
- **Rich Tool Set**: Navigate, click, type, scroll, take screenshots, extract content, manage tabs
- **Screenshot Support**: Returns screenshots as images that Claude can see and analyze
- **Multiple Transports**: Works with stdio (default), SSE, or HTTP

## Quick Start

### 1. Install the Package

```bash
pipx install git+https://github.com/jamesmurdza/playwright-daytona-mcp-server.git
```

### 2. Get a Daytona API Key

1. Sign up at [daytona.io](https://daytona.io)
2. Go to your dashboard and generate an API key
3. Set it as an environment variable:

```bash
export DAYTONA_API_KEY="your-api-key-here"
```

### 3. Configure Claude Code / Claude Desktop

Add to your MCP settings:

```json
{
  "mcpServers": {
    "daytona-playwright": {
      "command": "daytona-playwright-mcp",
      "env": {
        "DAYTONA_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

## Usage

Once configured, you can ask Claude to browse the web:

```
"Start a browser and go to https://news.ycombinator.com"

"Take a screenshot of the page"

"Click on the first article link"

"Search for 'AI news' on Google and show me the results"

"Fill out the contact form on example.com with test data"
```

### Workflow

1. **Start the browser**: Claude will call `browser_start` to create a Daytona sandbox with Chromium
2. **Navigate and interact**: Use navigation, clicking, typing, and other tools
3. **Take screenshots**: See what's on the page with `browser_screenshot`
4. **Clean up**: Call `browser_stop` when done to delete the sandbox

## Available Tools

### Browser Lifecycle
| Tool | Description |
|------|-------------|
| `browser_start` | Start a new browser session in a Daytona sandbox |
| `browser_stop` | Stop the browser and clean up the sandbox |
| `browser_status` | Check if the browser is running |

### Navigation
| Tool | Description |
|------|-------------|
| `browser_navigate` | Navigate to a URL |
| `browser_back` | Go back in history |
| `browser_forward` | Go forward in history |
| `browser_refresh` | Refresh the current page |

### Interaction
| Tool | Description |
|------|-------------|
| `browser_click` | Click on an element (CSS, XPath, or text selector) |
| `browser_type` | Type text into an input field |
| `browser_press` | Press keyboard keys (Enter, Tab, etc.) |
| `browser_hover` | Hover over an element |
| `browser_select` | Select from a dropdown |
| `browser_scroll` | Scroll the page or an element |

### Content Extraction
| Tool | Description |
|------|-------------|
| `browser_screenshot` | Take a screenshot (full page or element) |
| `browser_get_text` | Get text content from the page |
| `browser_get_html` | Get HTML content |
| `browser_get_attribute` | Get an element's attribute |
| `browser_evaluate` | Run JavaScript and get results |

### Waiting
| Tool | Description |
|------|-------------|
| `browser_wait_for_selector` | Wait for an element to appear/disappear |
| `browser_wait_for_navigation` | Wait for navigation to complete |

### Tab Management
| Tool | Description |
|------|-------------|
| `browser_new_tab` | Open a new tab |
| `browser_list_tabs` | List all open tabs |
| `browser_switch_tab` | Switch to a different tab |
| `browser_close_tab` | Close a tab |

### File Operations
| Tool | Description |
|------|-------------|
| `browser_upload_file` | Upload a file to a file input |

## Running with Different Transports

### Stdio (Default - for Claude Code/Desktop)

```bash
daytona-playwright-mcp
```

### HTTP Transport (for remote connections)

```bash
daytona-playwright-mcp --transport http --host 0.0.0.0 --port 8765
```

Then connect via: `http://localhost:8765/mcp`

### SSE Transport (legacy)

```bash
daytona-playwright-mcp --transport sse --host 0.0.0.0 --port 8765
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DAYTONA_API_KEY` | Your Daytona API key (required) | - |
| `DAYTONA_API_URL` | Daytona API server URL | `https://app.daytona.io/api` |

## Development

### Run from Source

```bash
# Clone the repository
git clone https://github.com/jamesmurdza/playwright-daytona-mcp-server.git
cd playwright-daytona-mcp-server

# Install dependencies
uv sync

# Run the server
uv run daytona-playwright-mcp
```

### Configure MCP for Development

When developing locally, use this MCP configuration:

```json
{
  "mcpServers": {
    "daytona-playwright": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/playwright-daytona-mcp-server", "daytona-playwright-mcp"],
      "env": {
        "DAYTONA_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Run Tests

```bash
uv run pytest
```

## How It Works

1. When you call `browser_start`, the server:
   - Creates a Daytona sandbox (default Python sandbox has Chromium + Xvfb pre-installed)
   - Launches Chromium with remote debugging enabled
   - Starts a TCP proxy to expose the CDP port externally
   - Connects to Chromium via CDP (Chrome DevTools Protocol) through Daytona's secure signed URLs

2. All browser commands are executed through the Playwright API connected to the remote browser

3. Screenshots are captured as PNG images and returned via MCP's image content type

4. When you call `browser_stop`, the sandbox is deleted and all resources are freed

## Troubleshooting

### "DAYTONA_API_KEY environment variable is not set"

Make sure your API key is configured in the MCP server settings, not just in your shell.

### Browser fails to start

- Check that your Daytona API key is valid
- The sandbox may take a minute to provision on first use
- Increase the `timeout` parameter if needed

### Screenshots not appearing

- Make sure you're using a recent version of Claude Code/Desktop that supports MCP images
- The `browser_screenshot` tool returns an Image type that should render automatically

### Connection timeouts

The default timeout is 60 seconds. For slower connections or first-time image builds, increase it:

```
"Start a browser with a 120 second timeout"
```

## License

MIT

## Credits

- Based on the [Daytona browser-in-sandbox pattern](https://gist.github.com/synacktraa/29e05d51363b40d55e4d163aea8feaae) by synacktraa
- Uses [Patchright](https://github.com/AnyBrowser/patchright) for Playwright CDP connectivity
- Built with [FastMCP](https://github.com/jlowin/fastmcp) for the MCP server
- Powered by [Daytona](https://daytona.io) cloud sandboxes
