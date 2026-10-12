# RapidWright MCP Server

An MCP (Model Context Protocol) server that provides AI assistant access to [RapidWright](https://github.com/Xilinx/RapidWright), an open-source FPGA design tool framework from AMD.

This server enables AI assistants like Cursor to interact with FPGA designs, query device information, analyze design checkpoints, and explore Xilinx/AMD device architectures through natural language.

## Features

- **Device Information**: Query supported FPGA devices and families, get detailed device specifications
- **Design Checkpoint Analysis**: Load Vivado .dcp files, inspect design statistics, search for cells
- **Device Architecture Exploration**: Get tile/site information, query device resources
- **Design Optimization**: LUT input cone optimization and high fanout net splitting

## Quick Start

### Prerequisites

- Python 3.12 for LATCH physical validation (package metadata permits >=3.10)
- Java 21.0.1 for the recorded LATCH toolchain
- Explicit Vivado 2025.1 and RapidWright at the gitlink version recorded in
  the [LATCH tool guide](../docs/latch-tools.md)

### Installation

Install Python dependencies from the source root with the chosen interpreter:

```bash
python3.12 -m pip install -e '.[tools]'
```

This installs the `tools` extra, including the `rapidwright` Python package for
JPype bridging. Configure a RapidWright installation at the recorded version.
The inherited `make setup` helper also builds the submodule and downloads
contest benchmarks; use it when you need that broader setup and have access
to those materials.

To rebuild RapidWright after modifying its source code:

```bash
make build-rapidwright
```

### MCP setup helper

```bash
cd RapidWrightMCP
PYTHON_EXE=/absolute/path/to/python3.12 ./setup.sh
```

The helper installs the same root `tools` extra and reports Java/submodule
configuration. Its Java check accepts Java 11 or later; the recorded LATCH
toolchain uses Java 21.0.1. `test_server.py` initializes the real JVM, queries
device data and writes temporary checkpoints.

## Usage with Cursor

Add to your MCP configuration file:

```json
{
  "mcpServers": {
    "rapidwright": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/RapidWrightMCP/server.py"],
      "env": {
        "JAVA_HOME": "/absolute/path/to/java-21.0.1",
        "RAPIDWRIGHT_PATH": "/absolute/path/to/RapidWright",
        "CLASSPATH": "/absolute/path/to/RapidWright/bin:/absolute/path/to/RapidWright/jars/*"
      }
    }
  }
}
```

Restart Cursor after saving.

## Core Tools

| Tool | Description |
|------|-------------|
| `initialize_rapidwright` | Initialize RapidWright (must be called first) |
| `get_supported_devices` | List all supported FPGA devices |
| `get_device_info` | Get detailed information about a specific device |
| `read_checkpoint` | Load a Vivado Design Checkpoint (.dcp) file |
| `write_checkpoint` | Save design to a .dcp file |
| `get_design_info` | Get statistics about the loaded design |
| `search_cells` | Search for cells by name or type |
| `get_tile_info` | Get information about a specific tile |
| `search_sites` | Search for sites by type on a device |
| `optimize_lut_input_cone` | Optimize LUT chains by combining into single LUTs |
| `optimize_fanout` | Split high fanout nets by replicating drivers |

## Example Usage

The following dialogue illustrates the workflow; device counts and optimization
results depend on the installation and design.

```
User: "Initialize RapidWright and show me what devices are available"
AI: [calls initialize_rapidwright and get_supported_devices]
    "RapidWright supports 50+ devices including xcvu3p, xcvu9p, xcku040..."

User: "Load my design from ~/my_design.dcp and tell me how many LUTs it uses"
AI: [calls read_checkpoint, then get_design_info and search_cells]
    "Your design uses 15,432 LUT6 cells..."

User: "Optimize the LUT input cone for pin 'top/cpu/alu/result[0]'"
AI: [calls optimize_lut_input_cone]
    "Successfully combined 3 chained LUTs into a single LUT6."

User: "The net 'clk_enable' has very high fanout. Split it into 4 parts."
AI: [calls optimize_fanout]
    "Split 'clk_enable' (original fanout: 2,456) into 4 nets with ~614 loads each."
```

## Tips

- **Initialize once** per session before loading or querying a design
- **Use absolute paths** for .dcp files
- **Be specific** with device names (e.g., "xcvu9p" not "vu9p")
- **Chain requests**: "Load design X and tell me Y"
- Use natural language - no need for exact tool names

## Common Cell & Site Types

| Cell Type | Description |
|-----------|-------------|
| LUT6 | 6-input Lookup Table |
| FDRE | D Flip-Flop with Clock Enable and Sync Reset |
| FDCE | D Flip-Flop with Clock Enable and Async Clear |
| CARRY8 | Carry Logic (UltraScale+) |
| DSP48E2 | DSP Block (UltraScale+) |
| RAMB36E2 | 36Kb Block RAM (UltraScale+) |
| BUFGCE | Global Clock Buffer |

| Site Type | Description |
|-----------|-------------|
| SLICEL | Logic Slice (LUTs, FFs, Carry) |
| SLICEM | Memory Slice (+ Distributed RAM) |
| DSP48E2 | DSP/Math Block |
| RAMB36/RAMB18 | Block RAM |
| URAM288 | 288Kb Ultra RAM |

---

## Design Optimization Guide

### LUT Input Cone Optimization

Combines chained small LUTs into a single larger LUT (up to LUT6) to reduce logic depth.

```
Before:  Input -> LUT2 -> LUT3 -> LUT4 -> Output  (3 logic levels)
After:   Input -> LUT6 -> Output                   (1 logic level)
```

**When to use:**
- Critical path optimization when timing shows multiple LUT levels
- Post-place-and-route ECO fixes without full re-synthesis

**Parameters:**
- `hierarchical_input_pins`: List of pins to optimize (e.g., `["top/cpu/alu/result[0]"]`)

The tool changes the loaded design in memory. Save it with `write_checkpoint`
to a new output path, then check routing, timing and functionality with the
LATCH validation flow.

**Limitations:**
- Maximum 6 inputs (LUT6 is the largest)
- Only works on paths driven exclusively by LUTs
- Cannot optimize paths with flip-flops, DSPs, or other non-LUT elements

### Fanout Optimization

Splits high-fanout nets by replicating the source driver. Each replica drives a subset of loads.

```
Before:  Driver -> [1000 loads]
After:   Driver_1 -> [250 loads]
         Driver_2 -> [250 loads]
         Driver_3 -> [250 loads]
         Driver_4 -> [250 loads]
```

**When to use:**
- High-fanout enable/control signals
- Routing congestion from heavily loaded nets
- Timing closure for high-fanout paths

**Parameters:**
- `net_name`: Physical net name in the loaded design
- `split_factor`: Number of driver copies (`k`)

Start with a small split factor and compare the resulting routing and timing.
The best value depends on load placement and available resources.

**Trade-offs:**
- More copies can reduce fanout per driver and improve routing or timing
- Additional driver cells increase resource use and may affect power
- A successful tool call does not guarantee timing improvement or a valid route

**Limitations:**
- Only works on routed nets
- Replication may provide little benefit when the original fanout is small

As with LUT cone optimization, export the changed design with `write_checkpoint`
and validate the candidate before using it as a result.

### Optimization Troubleshooting

| Error | Solution |
|-------|----------|
| "Pin not found" | Check hierarchical path, use full name including top module |
| "No optimization possible" | Pin not driven by LUTs, or already optimal single LUT |
| "6 maximum inputs" | Cone requires >6 inputs; try optimizing a later stage |
| "Net not found" | Use physical net name, not hierarchical logical name |

---

## Architecture

```
┌─────────────────┐
│     Cursor      │
│  AI Assistant   │
└────────┬────────┘
         │ MCP Protocol (JSON-RPC over stdio)
┌────────▼────────┐
│  server.py      │  ← MCP Server
└────────┬────────┘
┌────────▼────────────┐
│ rapidwright_tools.py│  ← Tool Wrappers
└────────┬────────────┘
┌────────▼────────┐
│  RapidWright    │  ← pip package (JPype + Java libs)
└─────────────────┘
```

The `rapidwright` pip package provides the JPype/Python bridge, while the
`RAPIDWRIGHT_PATH` and `CLASSPATH` environment variables redirect it to use
Java classes compiled from the local `RapidWright/` git submodule. This allows
contestants to modify and rebuild RapidWright source code directly.

## Development

### Project Structure

```
RapidWrightMCP/
├── server.py              # Main MCP server
├── rapidwright_tools.py   # RapidWright wrapper functions
├── setup.sh               # Installs the source root's pyproject tools extra
├── test_server.py        # Test suite
└── rapidwright_mcp.log   # Log file (created at runtime)

../RapidWright/            # Git submodule (Xilinx/RapidWright source)
├── src/                   # Java source (modifiable by contestants)
├── bin/                   # Compiled classes (after ./gradlew compileJava)
├── jars/                  # Third-party dependencies
└── gradlew               # Gradle wrapper for building
```

### Adding New Tools

To add a new tool:

1. **Add the function** to `rapidwright_tools.py`:
   ```python
   def my_new_tool(param1: str) -> Dict[str, Any]:
       """Your tool implementation."""
       if not _initialized:
           return {"error": "RapidWright not initialized"}
       # ... implementation
       return {"status": "success", "result": ...}
   ```

2. **Register the tool** in `server.py` `list_tools()`:
   ```python
   Tool(
       name="my_new_tool",
       description="What your tool does",
       inputSchema={
           "type": "object",
           "properties": {
               "param1": {"type": "string", "description": "..."}
           },
           "required": ["param1"]
       }
   )
   ```

3. **Add the handler** in `call_tool()`:
   ```python
   elif name == "my_new_tool":
       result = rw.my_new_tool(arguments["param1"])
   ```

### Running Tests

```bash
/absolute/path/to/venv/bin/python test_server.py
tail -f rapidwright_mcp.log
```

The test runs RapidWright and creates temporary DCPs; it is separate from offline
mock tests.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "RapidWright not initialized" | Call `initialize_rapidwright` first; check the configured Java installation |
| Server not appearing | Use absolute paths in config; restart Cursor/Claude completely |
| Out of memory | Increase `jvm_max_memory` (e.g., "8G", "16G") |
| Local changes not picked up | Rebuild: `make build-rapidwright` from the repo root |
| Installation issues | Check the chosen interpreter with `/absolute/path/to/venv/bin/python -m pip show rapidwright`; reinstall the root `tools` extra if needed |

**Checklist:**
- [ ] Configured Python 3.12 interpreter available?
- [ ] Configured Java 21.0.1 available?
- [ ] RapidWright pip bridge installed in that Python environment?
- [ ] RapidWright submodule present? (`ls RapidWright/gradlew`)
- [ ] RapidWright compiled? (`ls RapidWright/build/libs/main.jar`)
- [ ] `RAPIDWRIGHT_PATH` set? (`echo $RAPIDWRIGHT_PATH`)
- [ ] Config paths absolute?
- [ ] Cursor restarted?
- [ ] Check logs? (`cat rapidwright_mcp.log`)


## Resources

- [RapidWright Documentation](https://www.rapidwright.io/docs/)
- [RapidWright Javadoc](https://www.rapidwright.io/javadoc/)
- [RapidWright GitHub](https://github.com/Xilinx/RapidWright)
- [Model Context Protocol](https://modelcontextprotocol.io/)

