#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0
# Modified for LATCH: extracted from upstream dcp_optimizer.py and adapted as
# the shared backend lifecycle/flow host; see SOURCE_CREDITS.md.

"""Legacy Xilinx MCP lifecycle and behavior-preserved flow host.

The autonomous ``DCPOptimizer`` agent is archived. ``DCPOptimizerBase`` and
``FPGAOptimizerTest`` preserve the real MCP/Vivado flow lifecycle while the
new backend contract is adopted by the runtime.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except Exception:  # pragma: no cover - optional in local unit-test environments
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

from optimizer_pipeline.execution.vivado_utilization import parse_pblock_utilization_text, utilization_delta
from optimizer_pipeline.capabilities.intervention.manual import ManualActionValidator, ManualPrimitiveCatalog, ManualPrimitiveKind
from optimizer_pipeline.capabilities.intervention.recipe import PrimitiveCatalog, PrimitiveKind, RecipeSpec


class TeeStream:
    """Duplicates writes to both an original stream and a log file."""

    def __init__(self, original_stream, log_file):
        self.original_stream = original_stream
        self.log_file = log_file

    def write(self, data):
        self.original_stream.write(data)
        try:
            self.log_file.write(data)
            self.log_file.flush()
        except Exception:
            pass
        return len(data) if isinstance(data, (str, bytes)) else 0

    def flush(self):
        self.original_stream.flush()
        try:
            self.log_file.flush()
        except Exception:
            pass

    def fileno(self):
        return self.original_stream.fileno()

    def isatty(self):
        try:
            return self.original_stream.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


def setup_terminal_log(run_dir: Path) -> "tuple[object, object, object]":
    """
    Redirect stdout and stderr to also write to terminal.log inside *run_dir*.

    Returns (log_file, original_stdout, original_stderr) so the caller can
    restore streams and close the log file in a finally block.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "terminal.log"
    log_file = open(log_path, "w", buffering=1)  # line-buffered text mode

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)

    # Re-wire the root logger handler so it also sees the new stderr
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = sys.stderr

    print(f"Terminal log: {log_path}")
    return log_file, original_stdout, original_stderr


def restore_terminal_log(log_file, original_stdout, original_stderr):
    """Restore stdout/stderr and close the terminal log file."""
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = sys.stderr
    try:
        log_file.close()
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

MCP_INIT_TIMEOUT = 30.0
REPO_ROOT = Path(__file__).resolve().parents[3]
XILINX_FLOW_NAMES = (
    "fanout",
    "pblock",
    "vexriscv",
    "phys_opt",
    "place_route",
    "auto_pblock",
    "lut_cone",
    "retime",
    "net_swap",
    "memory_path_opt",
    "hardblock_relocate",
    "congestion_spread",
    "memory_path_opt_rw",
    "hardblock_relocate_rw",
    "congestion_spread_rw",
    "auto_top3",
    "rescue_place_reset_retime",
    "synthesized_recipe",
    "manual_action",
)


def ensure_vivado_on_path(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return env with VIVADO_EXEC's directory prepended to PATH when available.

    Some RapidWright APIs shell out to `vivado` while creating readable EDIFs
    from DCPs.  The server may know VIVADO_EXEC but still fail if `vivado` is
    not on PATH. Keeping this helper with the Xilinx backend fixes both normal
    test-mode runs and research frontier-expansion runs.
    """
    base = dict(os.environ if env is None else env)
    vivado_exec = base.get("VIVADO_EXEC") or os.environ.get("VIVADO_EXEC")
    if not vivado_exec:
        return base
    vivado_bin = Path(vivado_exec).expanduser().parent
    if not vivado_bin.exists():
        return base
    path = base.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if str(vivado_bin) not in parts:
        base["PATH"] = str(vivado_bin) + (os.pathsep + path if path else "")
    return base


def parse_timing_summary_static(timing_report: str) -> dict:
    """
    Parse timing summary report to extract WNS, TNS, and failing endpoints.
    Returns dict with keys: wns, tns, failing_endpoints
    
    Parses the Design Timing Summary table:
        WNS(ns)      TNS(ns)  TNS Failing Endpoints  ...
        -------      -------  ---------------------  ...
         -0.099       -1.449                     42  ...
    
    This is a shared utility function used by both FPGAOptimizer and FPGAOptimizerTest.
    """
    result = {
        "wns": None,
        "tns": None,
        "failing_endpoints": None
    }
    
    lines = timing_report.split('\n')
    
    # Find the line with "WNS(ns)" header
    header_idx = -1
    for i, line in enumerate(lines):
        if 'WNS(ns)' in line and 'TNS(ns)' in line:
            header_idx = i
            break
    
    if header_idx == -1:
        return result
    
    # The data line should be 2 lines after the header (skipping the dashes line)
    # Format: whitespace + values separated by whitespace
    data_idx = header_idx + 2
    if data_idx >= len(lines):
        return result
    
    data_line = lines[data_idx].strip()
    if not data_line:
        return result
    
    # Split by whitespace and extract first 3 values: WNS, TNS, TNS Failing Endpoints
    parts = data_line.split()
    if len(parts) >= 3:
        try:
            result["wns"] = float(parts[0])
            result["tns"] = float(parts[1])
            result["failing_endpoints"] = int(parts[2])
        except (ValueError, IndexError):
            # If parsing fails, leave as None
            pass
    
    return result


class DCPOptimizerBase:
    """Base class with shared functionality for FPGA optimization."""
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None):
        self.debug = debug
        
        # Create run directory if not provided
        if run_dir is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.run_dir = (Path.cwd() / f"dcp_optimizer_run-{timestamp}").resolve()
            self.run_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created run directory: {self.run_dir}")
        else:
            self.run_dir = Path(run_dir).resolve()
            self.run_dir.mkdir(parents=True, exist_ok=True)
        
        self.exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None
        
        # Use run directory for all temporary files
        self.temp_dir = self.run_dir
        logger.info(f"Working directory: {self.temp_dir}")
        
        # Timing tracking
        self.initial_wns = None
        self.initial_tns = None
        self.initial_failing_endpoints = None
        self.high_fanout_nets = []
        self.clock_period = None
        self.target_clock = None  # Set to clock name (e.g. "clk_fpl26contest") for clock-specific Fmax
        self.initial_utilization: Dict[str, Optional[int]] = {}
        self.final_utilization: Dict[str, Optional[int]] = {}
        
        # Log file handles
        self._rw_log_file = None
        self._v_log_file = None

    async def _initialize_mcp_session(
        self,
        session: ClientSession,
        server_name: str,
        diagnostics_target: str,
        timeout: float = MCP_INIT_TIMEOUT,
    ):
        """Initialize an MCP session with a clear timeout error."""
        try:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"{server_name} MCP handshake timed out after {timeout:.0f}s. "
                f"Check {diagnostics_target} for startup diagnostics."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"{server_name} MCP handshake failed. "
                f"Check {diagnostics_target} for startup diagnostics. "
                f"Original error: {exc}"
            ) from exc

    async def start_servers(self, log_prefix: str = ""):
        """Start and connect to both MCP servers."""
        # MCP servers and the RapidWright checkout remain repository-owned.
        # Resolve them explicitly because this implementation no longer lives
        # in the repository root.
        script_dir = REPO_ROOT
        
        # Create log files in run directory
        rapidwright_log = self.run_dir / "rapidwright.log"
        rapidwright_mcp_log = self.run_dir / "rapidwright-mcp.log"
        vivado_log = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"
        vivado_mcp_log = self.run_dir / "vivado-mcp.log"
        
        # Open log files (if not in debug mode, redirect stderr to log)
        if self.debug:
            self._rw_log_file = None
            self._v_log_file = None
            logger.info("Debug mode: MCP server output will be shown in console")
            if log_prefix:
                print(f"{log_prefix} Debug mode: MCP server output will be shown in console")
        else:
            self._rw_log_file = open(rapidwright_mcp_log, 'w')
            self._v_log_file = open(vivado_mcp_log, 'w')
            logger.info(f"RapidWright Java output: {rapidwright_log}")
            logger.info(f"RapidWright MCP output: {rapidwright_mcp_log}")
            logger.info(f"Vivado output: {vivado_log}")
            logger.info(f"Vivado journal: {vivado_journal}")
            logger.info(f"Vivado MCP output: {vivado_mcp_log}")
            print(f"Log files in {self.run_dir.name}/: {rapidwright_log.name}, {rapidwright_mcp_log.name}, {vivado_log.name}, {vivado_journal.name}, {vivado_mcp_log.name}")
        
        # RapidWright MCP server config
        rapidwright_args = [str(script_dir / "RapidWrightMCP" / "server.py")]
        if not self.debug:
            rapidwright_args.extend([
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log)
            ])
        
        env = ensure_vivado_on_path({**os.environ})
        rapidwright_submodule = script_dir / "RapidWright"
        if rapidwright_submodule.is_dir() and "RAPIDWRIGHT_PATH" not in env:
            env["RAPIDWRIGHT_PATH"] = str(rapidwright_submodule)
            env["CLASSPATH"] = f"{rapidwright_submodule}/bin:{rapidwright_submodule}/jars/*"
        
        rapidwright_config = {
            "command": sys.executable,
            "args": rapidwright_args,
            "cwd": str(self.run_dir),
            "env": env
        }
        
        # Vivado MCP server config
        vivado_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            vivado_args.extend([
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal)
            ])
        
        vivado_config = {
            "command": sys.executable,
            "args": vivado_args,
            "cwd": str(self.run_dir),
            "env": ensure_vivado_on_path({**os.environ})
        }
        
        # Start RapidWright MCP
        logger.info("Starting RapidWright MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting RapidWright MCP server...")
        start_time = time.time()
        
        rw_params = StdioServerParameters(**rapidwright_config)
        rw_transport = await self.exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=self._rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self.exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        rw_diagnostics = "console output" if self.debug else str(rapidwright_mcp_log)
        await self._initialize_mcp_session(
            self.rapidwright_session,
            "RapidWright",
            rw_diagnostics,
        )
        
        elapsed = time.time() - start_time
        logger.info(f"RapidWright MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} RapidWright MCP server started in {elapsed:.2f}s")
        
        # Start Vivado MCP
        logger.info("Starting Vivado MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting Vivado MCP server...")
        start_time = time.time()
        
        vivado_params = StdioServerParameters(**vivado_config)
        vivado_transport = await self.exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=self._v_log_file)
        )
        v_read, v_write = vivado_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        v_diagnostics = "console output" if self.debug else str(vivado_mcp_log)
        await self._initialize_mcp_session(
            self.vivado_session,
            "Vivado",
            v_diagnostics,
        )
        
        elapsed = time.time() - start_time
        logger.info(f"Vivado MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} Vivado MCP server started in {elapsed:.2f}s")
        
        logger.info("Both MCP servers connected")
        if log_prefix:
            print(f"{log_prefix} Both MCP servers connected successfully")
    
    async def cleanup(self):
        """Clean up resources."""
        await self.exit_stack.aclose()
        
        if self._rw_log_file:
            self._rw_log_file.close()
        if self._v_log_file:
            self._v_log_file.close()
        
        logger.info(f"Run directory preserved at: {self.run_dir}")
    
    def calculate_fmax(self, wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
        """
        Calculate achievable fmax in MHz based on WNS and clock period.
        
        fmax = 1 / (clock_period - WNS) when WNS < 0 (timing violation)
        fmax = 1 / clock_period when WNS >= 0 (timing met)
        
        Returns fmax in MHz, or None if cannot be calculated.
        """
        if clock_period is None or clock_period <= 0:
            return None
        if wns is None:
            return None
        
        achievable_period_ns = clock_period - wns
        if achievable_period_ns <= 0:
            return None
        
        return 1000.0 / achievable_period_ns
    
    async def get_clock_period(self, call_tool_fn) -> Optional[float]:
        """
        Query the clock period of the target clock from Vivado in nanoseconds.
        
        First checks for the contest clock 'clk_fpl26contest'. If found, uses its
        period and sets self.target_clock. Otherwise falls back to the endpoint clock
        of the worst setup timing path.
        
        Args:
            call_tool_fn: Function to call Vivado tools, should accept (tool_name, arguments)
        
        Returns the period of the target clock, or None if no clocks found.
        """
        tcl_cmd = (
            "set contest_clk [get_clocks -quiet clk_fpl26contest]; "
            "if {$contest_clk ne {}} { "
            "  puts \"CLOCK:clk_fpl26contest\"; "
            "  puts [get_property PERIOD $contest_clk]; "
            "} else { "
            "  set tp [get_timing_paths -max_paths 1 -setup]; "
            "  if {$tp ne {}} { "
            "    set clk [get_property ENDPOINT_CLOCK $tp]; "
            "    if {$clk ne {}} { "
            "      puts \"CLOCK:$clk\"; "
            "      puts [get_property PERIOD [get_clocks $clk]]; "
            "    } "
            "  } "
            "}"
        )
        try:
            result = await call_tool_fn("run_tcl", {"command": tcl_cmd})
            
            clock_name = None
            for token in result.strip().split():
                if token.startswith('CLOCK:'):
                    clock_name = token[len('CLOCK:'):]
                    continue
                if token.startswith('ERROR') or token.startswith('WARNING'):
                    continue
                try:
                    period = float(token)
                    if period > 0:
                        if clock_name:
                            self.target_clock = clock_name
                            logger.info(f"Target clock: {clock_name}, period: {period:.3f} ns")
                        else:
                            logger.info(f"Critical clock period: {period:.3f} ns")
                        return period
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get clock period: {e}")
        
        logger.warning("Could not determine clock period from Vivado")
        return None
    
    async def get_wns_for_target_clock(self, call_tool_fn) -> Optional[float]:
        """
        Get WNS specifically for the target clock domain.
        
        When target_clock is set (e.g. 'clk_fpl26contest'), queries WNS filtered
        to that clock's timing paths. Falls back to overall WNS if no target clock.
        
        Args:
            call_tool_fn: Function to call Vivado tools, should accept (tool_name, arguments)
        
        Returns WNS in nanoseconds, or None if query fails.
        """
        if self.target_clock:
            tcl_cmd = (
                f"set clk_obj [get_clocks -quiet {{{self.target_clock}}}]; "
                f"if {{$clk_obj ne {{}}}} {{ "
                f"  set tp [get_timing_paths -max_paths 1 -setup -to $clk_obj]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts 0.0}} "
                f"}} else {{ "
                f"  set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts 0.0}} "
                f"}}"
            )
        else:
            tcl_cmd = (
                "set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                "if {[llength $tp] > 0} {get_property SLACK $tp} else {puts 0.0}"
            )
        
        try:
            result = await call_tool_fn("run_tcl", {"command": tcl_cmd})
            for token in result.strip().split('\n'):
                token = token.strip()
                if not token or token.startswith('ERROR') or token.startswith('WARNING'):
                    continue
                try:
                    wns = float(token)
                    clock_info = f" (clock: {self.target_clock})" if self.target_clock else ""
                    logger.info(f"WNS{clock_info}: {wns:.3f} ns")
                    return wns
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get WNS for target clock: {e}")
        
        return None
    
    def parse_high_fanout_nets(self, report: str) -> list[tuple[str, int, int]]:
        """
        Parse high fanout nets report and return list of (net_name, fanout, path_count).
        """
        nets = []
        lines = report.split('\n')
        in_net_section = False
        
        for line in lines:
            if 'Paths' in line and 'Fanout' in line and 'Parent Net Name' in line:
                in_net_section = True
                continue
            
            if in_net_section:
                if line.startswith('---') or not line.strip():
                    continue
                if line.startswith('==='):
                    break
                
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        path_count = int(parts[0])
                        fanout = int(parts[1])
                        net_name = parts[2]
                        
                        if (net_name and 
                            '/' in net_name and
                            not net_name.startswith('get_') and
                            not net_name.startswith('ERROR') and
                            not net_name.startswith('WARNING')):
                            nets.append((net_name, fanout, path_count))
                    except ValueError:
                        continue
        
        return nets

    async def collect_utilization(self, call_tool_fn, timeout: float = 300.0) -> Dict[str, Optional[int]]:
        """Fetch and parse utilization through a provided Vivado tool caller."""
        try:
            raw_report = await call_tool_fn("report_utilization_for_pblock", {"timeout": timeout})
        except Exception as exc:
            logger.warning(f"Failed to collect utilization: {exc}")
            return {}
        parsed = parse_pblock_utilization_text(raw_report)
        if not any(value is not None for value in parsed.values()):
            logger.warning("Utilization report parsed but no numeric fields were found")
        return parsed

    def utilization_delta(self) -> Dict[str, Optional[int]]:
        """Return final-minus-initial resource deltas for tracked utilization."""
        return utilization_delta(self.initial_utilization, self.final_utilization)

    def _format_fmax_results(
        self,
        clock_period: Optional[float],
        initial_wns: Optional[float],
        result_wns: Optional[float],
        result_label: str = "Final",
    ) -> list[str]:
        """Format Fmax/WNS results block as a list of lines.
        
        """
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        result_fmax = self.calculate_fmax(result_wns, clock_period)
        result_fmax_label = f"{result_label} Fmax:"
        result_wns_label = f"{result_label} WNS:"
        
        lines: list[str] = []
        if initial_fmax is not None and result_fmax is not None:
            target_fmax = 1000.0 / clock_period
            fmax_change = result_fmax - initial_fmax
            lines.append(f"  {'Target Fmax:':<21s}{target_fmax:8.2f} MHz  (clock period: {clock_period:.3f} ns)")
            lines.append(f"  {'Initial Fmax:':<21s}{initial_fmax:8.2f} MHz  (WNS: {initial_wns:.3f} ns)")
            lines.append(f"  {result_fmax_label:<21s}{result_fmax:8.2f} MHz  (WNS: {result_wns:.3f} ns)")
            lines.append(f"  {'Fmax Improvement:':<21s}{fmax_change:+8.2f} MHz  (WNS: {result_wns - initial_wns:+.3f} ns)")
        else:
            if clock_period is not None:
                target_fmax = 1000.0 / clock_period
                lines.append(f"  {'Clock period:':<21s}{clock_period:8.3f} ns (target: {target_fmax:.2f} MHz)")
            if initial_wns is not None:
                fmax_str = f"  (fmax: {initial_fmax:.2f} MHz)" if initial_fmax else ""
                lines.append(f"  {'Initial WNS:':<21s}{initial_wns:8.3f} ns{fmax_str}")
            if result_wns is not None:
                fmax_str = f"  (fmax: {result_fmax:.2f} MHz)" if result_fmax else ""
                lines.append(f"  {result_wns_label:<21s}{result_wns:8.3f} ns{fmax_str}")
            if initial_wns is not None and result_wns is not None:
                lines.append(f"  {'WNS Improvement:':<21s}{result_wns - initial_wns:+8.3f} ns")
        
        return lines

    def _format_utilization_delta_results(self) -> list[str]:
        """Format compact utilization before/after/delta lines for test summaries."""
        initial = self.initial_utilization or {}
        final = self.final_utilization or {}
        deltas = self.utilization_delta()
        resources = [
            ("LUTs", "luts"),
            ("FFs", "ffs"),
            ("DSPs", "dsps"),
            ("BRAMs", "brams"),
            ("URAMs", "urams"),
        ]
        lines: list[str] = []
        for label, key in resources:
            before = initial.get(key)
            after = final.get(key)
            delta = deltas.get(key)
            if before is None and after is None:
                continue
            before_str = f"{before:,}" if isinstance(before, int) else "-"
            after_str = f"{after:,}" if isinstance(after, int) else "-"
            delta_str = f"{delta:+,}" if isinstance(delta, int) else "-"
            lines.append(f"  {label:<8s}{before_str:>10s} -> {after_str:>10s}  (delta: {delta_str})")
        return lines
    
    
    def print_wns_change(
        self,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float]
    ):
        """Print Fmax/WNS change comparison with improvement/regression status."""
        if final_wns is None or initial_wns is None:
            return
        
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        final_fmax = self.calculate_fmax(final_wns, clock_period)
        
        if initial_fmax is not None and final_fmax is not None:
            fmax_improvement = final_fmax - initial_fmax
            pct = (fmax_improvement / initial_fmax) * 100 if initial_fmax else 0
            print(f"\n*** Fmax: {initial_fmax:.2f} -> {final_fmax:.2f} MHz ({fmax_improvement:+.2f} MHz, {pct:+.1f}%) ***")
            print(f"*** WNS:  {initial_wns:.3f} -> {final_wns:.3f} ns ***")
            if fmax_improvement > 0:
                print(f"IMPROVEMENT: Fmax improved by {fmax_improvement:.2f} MHz")
            elif fmax_improvement < 0:
                print(f"REGRESSION: Fmax got worse by {-fmax_improvement:.2f} MHz")
            else:
                print("NO CHANGE: Fmax is the same")
        else:
            wns_improvement = final_wns - initial_wns
            print(f"\n*** WNS: {initial_wns:.3f} -> {final_wns:.3f} ns ({wns_improvement:+.3f} ns) ***")
            if wns_improvement > 0:
                print(f"IMPROVEMENT: WNS improved by {wns_improvement:.3f} ns")
            elif wns_improvement < 0:
                print(f"REGRESSION: WNS got worse by {-wns_improvement:.3f} ns")
            else:
                print("NO CHANGE")
    
    def print_fmax_status(self, label: str, wns: Optional[float]):
        """Print Fmax (primary) and WNS (secondary) for a given measurement point."""
        if wns is None:
            print(f"*** {label}: WNS unknown ***")
            return
        fmax = self.calculate_fmax(wns, self.clock_period)
        clock_info = f" (clock: {self.target_clock})" if self.target_clock else ""
        if fmax is not None:
            print(f"*** {label} Fmax{clock_info}: {fmax:.2f} MHz (WNS: {wns:.3f} ns) ***")
        else:
            print(f"*** {label} WNS{clock_info}: {wns:.3f} ns ***")
    
    def print_test_summary(
        self,
        title: str,
        elapsed_seconds: float,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float],
        extra_info: str = ""
    ):
        """Print formatted test summary."""
        print("\n" + "="*70)
        print(title)
        print("="*70)
        print(f"Total runtime: {elapsed_seconds:.2f} seconds ({elapsed_seconds/60:.2f} minutes)")
        
        result_lines = self._format_fmax_results(clock_period, initial_wns, final_wns)
        if result_lines:
            print(f"\nFmax Results:")
            print("\n".join(result_lines))

        util_lines = self._format_utilization_delta_results()
        if util_lines:
            print("\nUtilization Delta:")
            print("\n".join(util_lines))
        
        if extra_info:
            print(f"\n{extra_info}")
        print("="*70)

    def save_run_summary(
        self,
        input_dcp: Path,
        output_dcp: Path,
        mode: str,
        flow: Optional[str] = None,
        optimizers_tried: Optional[List[Dict]] = None,
        extra: Optional[Dict] = None,
    ):
        """Save key run information to run_summary.json in the run directory.

        Args:
            input_dcp: Path to the input DCP file.
            output_dcp: Path to the output (optimized) DCP file.
            mode: 'test' or 'llm'.
            flow: Name of the flow/strategy used (test mode).
            optimizers_tried: List of per-flow result dicts (auto_top3 mode).
            extra: Any extra key/value pairs to merge into the summary.
        """
        initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
        final_wns = getattr(self, "final_wns", None) or (
            self.best_wns if hasattr(self, "best_wns") and self.best_wns > float("-inf") else None
        )
        final_fmax = self.calculate_fmax(final_wns, self.clock_period)

        wns_improvement = None
        if self.initial_wns is not None and final_wns is not None:
            wns_improvement = final_wns - self.initial_wns

        fmax_improvement = None
        if initial_fmax is not None and final_fmax is not None:
            fmax_improvement = final_fmax - initial_fmax

        total_runtime = None
        start = getattr(self, "start_time", None) or getattr(self, "_run_start_time", None)
        end = getattr(self, "end_time", None)
        if start is not None:
            total_runtime = (end or time.time()) - start

        summary = {
            "run_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "flow": flow,
            "input_dcp": str(input_dcp.resolve()),
            "output_dcp": str(output_dcp.resolve()),
            "run_dir": str(self.run_dir),
            "clock_period_ns": self.clock_period,
            "target_clock": getattr(self, "target_clock", None),
            "initial_wns_ns": self.initial_wns,
            "final_wns_ns": final_wns,
            "wns_improvement_ns": wns_improvement,
            "initial_fmax_mhz": initial_fmax,
            "final_fmax_mhz": final_fmax,
            "fmax_improvement_mhz": fmax_improvement,
            "total_runtime_seconds": total_runtime,
            "initial_utilization": self.initial_utilization or None,
            "final_utilization": self.final_utilization or None,
            "utilization_delta": self.utilization_delta() if (self.initial_utilization or self.final_utilization) else None,
        }

        if mode == "llm":
            summary["model"] = getattr(self, "model", None)
            summary["total_llm_calls"] = getattr(self, "llm_call_count", None)
            summary["total_tokens"] = getattr(self, "total_tokens", None)
            summary["total_cost_usd"] = getattr(self, "total_cost", None)

        if optimizers_tried:
            # Serialise Path objects so JSON can encode them
            clean = []
            for r in optimizers_tried:
                entry = dict(r)
                if "output" in entry:
                    entry["output"] = str(entry["output"])
                clean.append(entry)
            summary["optimizers_tried"] = clean
        elif flow:
            summary["optimizers_tried"] = [{"flow": flow, "final_wns_ns": final_wns, "final_fmax_mhz": final_fmax}]

        if extra:
            summary.update(extra)

        out_path = self.run_dir / "run_summary.json"
        try:
            with open(out_path, "w") as f:
                json.dump(summary, f, indent=2)
            logger.info(f"Run summary saved to {out_path}")
            print(f"Run summary: {out_path}")
        except Exception as e:
            logger.warning(f"Failed to save run summary: {e}")

    def save_analysis_context(
        self,
        input_dcp: Path,
        flow: str,
        extra_sections: Optional[Dict] = None,
    ) -> Path:
        """Save design analysis context to run_info_<benchmarkname>_<timestamp>.json in the run directory.

        Captures the data already collected during the analysis phase of a flow so
        the LLM (or any post-run tooling) can inspect it without re-running Vivado.

        Args:
            input_dcp: The input DCP that was analysed.
            flow: Name of the optimizer flow (e.g. 'phys_opt', 'vexriscv', 'llm').
            extra_sections: Optional dict of flow-specific sections to merge into
                            the top-level JSON (e.g. high_fanout_nets, detour_analysis).

        Returns the path of the written file.
        """
        target_fmax = (1000.0 / self.clock_period) if self.clock_period else None
        achievable_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
        timing_met = (self.initial_wns >= 0) if self.initial_wns is not None else None

        context: Dict = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "flow": flow,
            "input_dcp": str(input_dcp.name),
            "timing": {
                "clock_name": getattr(self, "target_clock", None),
                "clock_period_ns": self.clock_period,
                "target_fmax_mhz": round(target_fmax, 3) if target_fmax is not None else None,
                "wns_ns": self.initial_wns,
                "tns_ns": self.initial_tns,
                "failing_endpoints": self.initial_failing_endpoints,
                "achievable_fmax_mhz": round(achievable_fmax, 3) if achievable_fmax is not None else None,
                "timing_met": timing_met,
            },
        }

        if self.initial_utilization:
            context["timing"]["initial_utilization"] = self.initial_utilization
            context["initial_utilization"] = self.initial_utilization
        if self.final_utilization:
            context["timing"]["final_utilization"] = self.final_utilization
            context["final_utilization"] = self.final_utilization
            context["utilization_summary"] = {
                "luts": self.final_utilization.get("luts"),
                "ffs": self.final_utilization.get("ffs"),
                "dsps": self.final_utilization.get("dsps"),
                "brams": self.final_utilization.get("brams"),
                "urams": self.final_utilization.get("urams"),
            }
        if self.initial_utilization or self.final_utilization:
            context["utilization_delta"] = self.utilization_delta()

        if extra_sections:
            context.update(extra_sections)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = self.run_dir / f"run_info_{input_dcp.stem}_{timestamp}.json"
        try:
            with open(out_path, "w") as f:
                json.dump(context, f, indent=2)
            logger.info(f"Analysis context saved to {out_path}")
            print(f"Analysis context: {out_path}")
        except Exception as e:
            logger.warning(f"Failed to save analysis context: {e}")

        return out_path


class MCPToolCallError(RuntimeError):
    """An explicit tool-level error, not a per-object optimization outcome."""

    def __init__(self, backend: str, tool_name: str, reason: str):
        self.backend = backend
        self.tool_name = tool_name
        self.reason = reason
        # Do not copy arbitrary tool payloads into exception/summary messages.
        super().__init__(f"{backend} {tool_name} failed: {reason}")


def _mcp_member(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _checked_mcp_text(result: Any, backend: str, tool_name: str) -> str:
    """Preserve successful legacy text while surfacing explicit MCP/server errors.

    Only the response envelope and top-level JSON status are interpreted. Nested
    ``results`` (including zero or partial LUT-cone optimization) remain outcomes
    owned by the calling flow, not transport errors.
    """
    if _mcp_member(result, "isError", False):
        raise MCPToolCallError(backend, tool_name, "MCP isError")

    content = _mcp_member(result, "content", []) or []
    texts = [
        text for block in content
        if isinstance((text := _mcp_member(block, "text")), str)
    ]
    payloads = [_mcp_member(result, "structuredContent")]
    for text in texts:
        stripped = text.strip()
        # The legacy server's exception handler returns an entire Error: block.
        if stripped.startswith("Error:"):
            raise MCPToolCallError(backend, tool_name, "server Error response")
        if backend == "Vivado":
            # These are complete responses emitted by this server, not words
            # to search for inside a successful report or a user Tcl print.
            if stripped == f"Unknown tool: {tool_name}":
                raise MCPToolCallError(backend, tool_name, "server unknown tool response")
            if tool_name == "get_wns" and stripped == "ERROR: clock not found":
                raise MCPToolCallError(backend, tool_name, "target clock not found")
            # Vivado returns command errors as numbered ERROR lines even when
            # the MCP envelope reports success. Warnings and mere mentions of
            # ERROR inside a successful report are not this protocol marker.
            code = re.search(r"^\s*ERROR:\s*(\[[A-Za-z_][A-Za-z_0-9 .-]* \d+-\d+\])", text, re.MULTILINE)
            if code:
                raise MCPToolCallError(backend, tool_name, f"Vivado {code.group(1)}")
        try:
            payloads.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            pass
    for payload in payloads:
        if isinstance(payload, dict) and (
            payload.get("error") or payload.get("status") == "error"
        ):
            raise MCPToolCallError(backend, tool_name, "top-level JSON error")
    return "\n".join(texts) if content else "(no output)"


def _checkpoint_signature(path: Path) -> Optional[Tuple[int, ...]]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _require_written_checkpoint(path: Path, before: Optional[Tuple[int, ...]], backend: str) -> None:
    """Reject missing/empty or visibly unchanged output after a checkpoint write.

    This is a local artifact check, not proof of a valid design or an atomic
    write. It follows stat's existing target semantics; input/path protection
    belongs to the execution boundary. Use fresh output directories; coarse
    timestamps may not distinguish an overwrite of an equally sized file.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        raise MCPToolCallError(backend, "write_checkpoint", "checkpoint was not created") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise MCPToolCallError(backend, "write_checkpoint", "checkpoint is not a nonempty regular file")
    if before is not None and _checkpoint_signature(path) == before:
        raise MCPToolCallError(backend, "write_checkpoint", "checkpoint was not observably updated")


class FPGAOptimizerTest(DCPOptimizerBase):
    """
    Test mode for FPGA Design Optimization - hardcodes all tool calls to diagnose issues.
    
    This class runs a deterministic optimization flow without using any LLM, 
    making it easier to identify where MCP servers or Vivado might hang.
    """
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None):
        super().__init__(debug=debug, run_dir=run_dir)
        self.final_wns = None
    
    async def start_servers(self):
        """Start and connect to both MCP servers."""
        await super().start_servers(log_prefix="[TEST]")
    
    async def call_vivado_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a Vivado tool call with timing and logging."""
        logger.info(f"[VIVADO] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling vivado_{tool_name}...")
        start_time = time.time()
        
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            text = _checked_mcp_text(result, "Vivado", tool_name)
            
            elapsed = time.time() - start_time
            logger.info(f"[VIVADO] {tool_name} completed in {elapsed:.2f}s")
            print(f"[TEST] vivado_{tool_name} completed in {elapsed:.2f}s")
            
            return text
            
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: vivado_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: vivado_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise
    
    async def call_rapidwright_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a RapidWright tool call with timing and logging."""
        logger.info(f"[RAPIDWRIGHT] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling rapidwright_{tool_name}...")
        start_time = time.time()
        
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            text = _checked_mcp_text(result, "RapidWright", tool_name)
            
            elapsed = time.time() - start_time
            logger.info(f"[RAPIDWRIGHT] {tool_name} completed in {elapsed:.2f}s")
            print(f"[TEST] rapidwright_{tool_name} completed in {elapsed:.2f}s")
            
            return text
            
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: rapidwright_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: rapidwright_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise

    async def capture_utilization_for_checkpoint(
        self,
        dcp_path: Path,
        attr_name: str,
        label: str,
        timeout: float = 300.0,
    ) -> Dict[str, Optional[int]]:
        """Open a checkpoint, collect utilization, and store it on this instance."""
        try:
            await self.call_vivado_tool(
                "open_checkpoint",
                {"dcp_path": str(Path(dcp_path).resolve())},
                timeout=600.0,
            )

            async def _call(tool_name: str, arguments: dict) -> str:
                return await self.call_vivado_tool(tool_name, arguments, timeout=timeout)

            utilization = await self.collect_utilization(_call, timeout=timeout)
            setattr(self, attr_name, utilization)
            if utilization:
                print(
                    f"[TEST] {label} utilization: "
                    f"LUTs={utilization.get('luts')}, "
                    f"FFs={utilization.get('ffs')}, "
                    f"DSPs={utilization.get('dsps')}, "
                    f"BRAMs={utilization.get('brams')}, "
                    f"URAMs={utilization.get('urams')}"
                )
            else:
                print(f"[TEST] WARNING: {label} utilization unavailable")
            return utilization
        except Exception as exc:
            logger.warning(f"{label} utilization capture failed: {exc}")
            print(f"[TEST] WARNING: {label} utilization capture failed: {exc}")
            setattr(self, attr_name, {})
            return {}
    
    def parse_wns_from_timing_report(self, timing_report: str) -> Optional[float]:
        """Extract WNS from timing report using shared parsing logic."""
        return parse_timing_summary_static(timing_report)["wns"]
    
    async def _call_vivado_for_clock(self, tool_name: str, arguments: dict) -> str:
        """Helper to call Vivado tools for clock period query."""
        return await self.call_vivado_tool(tool_name, arguments, timeout=60.0)
    
    async def fetch_clock_period(self) -> Optional[float]:
        """Query clock period with test-mode logging."""
        period = await super().get_clock_period(self._call_vivado_for_clock)
        if period is not None:
            clock_info = f" (target clock: {self.target_clock})" if self.target_clock else ""
            print(f"[TEST] Clock period: {period:.3f} ns{clock_info}")
        else:
            print("[TEST] WARNING: Could not parse clock period from Vivado")
        return period

    async def ensure_placement_stage(self, label: str = "placement prep") -> None:
        """Force design into placement stage before pre-route-only phys_opt options."""
        print("\n" + "-"*60)
        print(f"STEP: Prepare placement stage ({label})")
        print("-"*60)

        unplace_result = await self.call_vivado_tool("run_tcl", {
            "command": "place_design -unplace"
        }, timeout=300.0)
        print(f"Unplace result:\n{unplace_result[:1200]}...")
        logger.info(f"Unplace ({label}): {unplace_result}")

        place_result = await self.call_vivado_tool("place_design", {
            "directive": "Default"
        }, timeout=3600.0)
        print(f"Place design result:\n{place_result[:1200]}...")
        logger.info(f"Place ({label}): {place_result}")

    async def build_rw_detour_prepass_checkpoint(
        self,
        input_dcp: Path,
        tag: str,
        num_paths: int = 30,
        detour_threshold: float = 1.8,
        max_candidates: int = 20,
    ) -> Optional[Path]:
        """Run a RapidWright detour-analysis + cell-placement pre-pass and return new DCP path."""
        print("\n" + "-"*60)
        print(f"RW PREPASS ({tag}): detour analysis + cell re-placement")
        print("-"*60)

        try:
            # Ensure both tools have the right design context.
            await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)

            await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)

            pins_file = Path(self.temp_dir) / f"{tag}_critical_pins.json"
            await self.call_vivado_tool("extract_critical_path_pins", {
                "num_paths": num_paths,
                "output_file": str(pins_file)
            }, timeout=600.0)

            detour_raw = await self.call_rapidwright_tool("analyze_net_detour", {
                "input_file": str(pins_file),
                "detour_threshold": detour_threshold
            }, timeout=300.0)

            try:
                detour_data = json.loads(detour_raw)
            except (json.JSONDecodeError, TypeError):
                detour_data = {}

            candidates = detour_data.get("candidates", []) if isinstance(detour_data, dict) else []
            if not candidates:
                print(f"RW PREPASS ({tag}): no detour candidates above threshold {detour_threshold}")
                return None

            # Keep unique cell order and cap candidate count.
            chosen_cells = []
            seen = set()
            for c in candidates:
                name = str(c.get("cell", "")).strip()
                if name and name not in seen:
                    seen.add(name)
                    chosen_cells.append(name)
                if len(chosen_cells) >= max_candidates:
                    break

            if not chosen_cells:
                print(f"RW PREPASS ({tag}): parsed no usable candidate cells")
                return None

            print(f"RW PREPASS ({tag}): relocating {len(chosen_cells)} candidate cells")
            await self.call_rapidwright_tool("optimize_cell_placement", {
                "cell_names": chosen_cells,
                "max_candidates": max_candidates
            }, timeout=600.0)

            rw_output = Path(self.temp_dir) / f"{tag}_rw_prepass.dcp"
            await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rw_output),
                "overwrite": True
            }, timeout=600.0)

            if rw_output.exists():
                print(f"RW PREPASS ({tag}): wrote {rw_output}")
                return rw_output

            print(f"RW PREPASS ({tag}): checkpoint not created, skipping pre-pass")
            return None

        except Exception as e:
            logger.warning(f"RW pre-pass ({tag}) failed, continuing without it: {e}")
            print(f"[WARN] RW pre-pass ({tag}) failed, continuing without it: {e}")
            return None
    
    async def run_test(self, input_dcp: Path, output_dcp: Path, max_nets_to_optimize: int = 5) -> bool:
        """
        Run the deterministic test optimization flow.
        
        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado
        3. Get the critical high fan out nets from Vivado
        4. Open the DCP in RapidWright
        5. Apply the fanout optimization for each high fanout net
        6. Write a DCP out from RapidWright
        7. Read the RapidWright generated DCP into Vivado
        8. Route the design in Vivado
        9. Report timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print(f"Max nets to optimize: {max_nets_to_optimize}")
        print("="*70 + "\n")
        
        overall_start = time.time()
        
        try:
            # ================================================================
            # Step 0: Initialize RapidWright (Vivado starts automatically)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            
            # Initialize RapidWright (Vivado will auto-start when first used)
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")
            
            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")
            
            # ================================================================
            # Step 2: Report timing in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report timing in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]
            
            # Get clock period for fmax calculation (also detects target clock)
            self.clock_period = await self.fetch_clock_period()
            
            # Get WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()
            
            # ================================================================
            # Step 3: Get critical high fanout nets
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Get critical high fanout nets")
            print("-"*60)
            
            result = await self.call_vivado_tool("get_critical_high_fanout_nets", {
                "num_paths": 50,
                "min_fanout": 100,
                "exclude_clocks": True
            }, timeout=600.0)
            print(f"High fanout nets report:\n{result}")
            logger.info(f"High fanout nets: {result}")
            
            # Parse the nets
            self.high_fanout_nets = self.parse_high_fanout_nets(result)
            print(f"\nParsed {len(self.high_fanout_nets)} high fanout nets")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "fanout",
                extra_sections={
                    "high_fanout_nets": [
                        {"net_name": name, "fanout": fanout, "critical_paths": paths}
                        for name, fanout, paths in self.high_fanout_nets
                    ]
                },
            )

            if not self.high_fanout_nets:
                print("WARNING: No high fanout nets found to optimize!")
                logger.warning("No high fanout nets found to optimize")
            
            # Select top nets to optimize
            nets_to_optimize = self.high_fanout_nets[:max_nets_to_optimize]
            print(f"Will optimize {len(nets_to_optimize)} nets:")
            for net_name, fanout, path_count in nets_to_optimize:
                print(f"  - {net_name} (fanout={fanout}, paths={path_count})")
            
            # ================================================================
            # Step 4: Open the DCP in RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Open DCP in RapidWright")
            print("-"*60)
            
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")
            
            # ================================================================
            # Step 5: Apply fanout optimization for each high fanout net
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Apply fanout optimizations in RapidWright")
            print("-"*60)
            
            successful_optimizations = 0
            for i, (net_name, fanout, path_count) in enumerate(nets_to_optimize):
                print(f"\n[{i+1}/{len(nets_to_optimize)}] Optimizing net: {net_name}")
                print(f"    Fanout: {fanout}, Critical paths: {path_count}")
                
                # Calculate split factor: fanout/100, min 2, max 8
                split_factor = max(2, min(8, fanout // 100))
                print(f"    Split factor: {split_factor}")
                
                try:
                    result = await self.call_rapidwright_tool("optimize_fanout", {
                        "net_name": net_name,
                        "split_factor": split_factor
                    }, timeout=300.0)
                    print(f"    Result: {result[:500]}...")
                    logger.info(f"Optimize fanout {net_name}: {result}")
                    
                    # Check if successful
                    if "error" not in result.lower() or "success" in result.lower():
                        successful_optimizations += 1
                except Exception as e:
                    print(f"    FAILED: {e}")
                    logger.error(f"Failed to optimize {net_name}: {e}")
            
            print(f"\nSuccessfully optimized {successful_optimizations}/{len(nets_to_optimize)} nets")
            
            # ================================================================
            # Step 6: Write DCP from RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Write DCP from RapidWright")
            print("-"*60)
            
            rapidwright_dcp = Path(self.temp_dir) / "rapidwright_optimized.dcp"
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rapidwright_dcp),
                "overwrite": True
            }, timeout=600.0)
            print(f"Write checkpoint result:\n{result}")
            logger.info(f"RapidWright write checkpoint: {result}")
            
            # Check if the file was created
            if rapidwright_dcp.exists():
                print(f"DCP file created: {rapidwright_dcp} ({rapidwright_dcp.stat().st_size} bytes)")
            else:
                print("WARNING: DCP file was not created!")
                logger.warning("RapidWright DCP file not created")
            
            # ================================================================
            # Step 7: Read RapidWright DCP into Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Read RapidWright DCP into Vivado")
            print("-"*60)
            
            # Note: Opening a RapidWright-generated DCP takes MUCH longer than
            # opening the original DCP because:
            # 1. Vivado must reload encrypted IP blocks from disk
            # 2. Vivado must reconstruct internal data structures
            # For large designs, this can take 10-30 minutes
            RAPIDWRIGHT_DCP_TIMEOUT = 300.0  # 5 minutes
            
            # Check if there's a Tcl script we need to source first (for encrypted IP)
            tcl_script = rapidwright_dcp.with_suffix('.tcl')
            if tcl_script.exists():
                print(f"Found Tcl script for encrypted IP: {tcl_script}")
                print(f"Note: This may take 10-30 minutes for large designs...")
                # Source the Tcl script instead of directly opening the DCP
                result = await self.call_vivado_tool("run_tcl", {
                    "command": f"source {{{tcl_script}}}"
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Source Tcl script result:\n{result}")
            else:
                # Opening a RapidWright-generated DCP can take longer than original
                # because Vivado needs to reconstruct some internal data structures
                result = await self.call_vivado_tool("open_checkpoint", {
                    "dcp_path": str(rapidwright_dcp)
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Open RapidWright DCP result:\n{result}")
            logger.info(f"Open RapidWright DCP: {result}")
            
            # ================================================================
            # Step 8: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Route design in Vivado")
            print("-"*60)
            
            # First check route status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status before routing:\n{result[:1500]}...")
            logger.info(f"Route status before routing: {result}")
            
            # Route the design
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default",
            }, timeout=600.0)  # 2 hour timeout for routing
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")
            
            # Check route status again
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")
            
            # ================================================================
            # Step 9: Report final timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Report final timing")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")
            
            # Get final WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()
            
            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)
            
            # Always write the final checkpoint (regardless of improvement)
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            
            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Nets optimized: {successful_optimizations}/{len(nets_to_optimize)}"
            )
            
            return True
            
        except Exception as e:
            logger.exception(f"Test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False
    
    async def run_test_logicnets(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Run the pblock-based optimization flow for LogicNets designs.
        
        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado (Initialize WNS)
        3. Run the Vivado tool extract_critical_path_cells
        4. Run the RapidWright tool analyze_critical_path_spread
        5. Use known-optimal pblock range for LogicNets (SLICE_X55Y60:SLICE_X111Y254)
        6. Unplace the design in Vivado
        7. Create and apply pblock to entire design
        8. Place the design in Vivado
        9. Route the design in Vivado
        10. Report timing in Vivado (compare against initial WNS)
        """
        pblock_ranges = "SLICE_X55Y60:SLICE_X111Y254"
        
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - LOGICNETS PBLOCK FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")
        
        overall_start = time.time()
        
        try:
            # ================================================================
            # Step 0: Initialize RapidWright (Vivado starts automatically)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")
            
            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")
            
            # ================================================================
            # Step 2: Report timing in Vivado (Initialize WNS)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report initial timing in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]
            
            # Get clock period for fmax calculation (also detects target clock)
            self.clock_period = await self.fetch_clock_period()
            
            # Get WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()
            
            # ================================================================
            # Step 3: Extract critical path cells from Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Extract critical path cells")
            print("-"*60)
            
            # Write to a file for efficient data transfer
            critical_paths_file = Path(self.temp_dir) / "critical_paths.json"
            result = await self.call_vivado_tool("extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(critical_paths_file)
            }, timeout=600.0)
            print(f"Extract critical paths result:\n{result[:2000]}...")
            logger.info(f"Extract critical paths: {result}")
            
            # ================================================================
            # Step 4: Open DCP in RapidWright and analyze critical path spread
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Analyze critical path spread in RapidWright")
            print("-"*60)
            
            # First, open the DCP in RapidWright
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")
            
            # Analyze critical path spread
            result = await self.call_rapidwright_tool("analyze_critical_path_spread", {
                "input_file": str(critical_paths_file)
            }, timeout=300.0)
            print(f"Critical path spread analysis:\n{result[:3000] if isinstance(result, str) else str(result)[:3000]}...")
            logger.info(f"Critical path spread: {result}")
            
            # Parse the spread analysis result to check if pblock is recommended
            spread_result = result if isinstance(result, str) else str(result)
            pblock_recommended = "spread-out" in spread_result.lower() or "pblock" in spread_result.lower()
            print(f"\n*** Pblock optimization {'RECOMMENDED' if pblock_recommended else 'may not be needed'} ***")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "pblock",
                extra_sections={
                    "critical_path_spread": {
                        "pblock_recommended": pblock_recommended,
                    },
                },
            )

            # ================================================================
            # Step 5: Apply pblock constraint for LogicNets
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Apply pblock for LogicNets")
            print("-"*60)
            
            print(f"Using pblock range: {pblock_ranges}")
            
            # ================================================================
            # Step 6: Unplace the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Unplace the design in Vivado")
            print("-"*60)
            
            # Use place_design -unplace to remove all placement
            result = await self.call_vivado_tool("run_tcl", {
                "command": "place_design -unplace"
            }, timeout=300.0)
            print(f"Unplace result:\n{result}")
            logger.info(f"Unplace result: {result}")
            
            # ================================================================
            # Step 7: Create and apply pblock to entire design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Create and apply pblock to entire design")
            print("-"*60)
            
            result = await self.call_vivado_tool("create_and_apply_pblock", {
                "pblock_name": "pblock_opt",
                "ranges": pblock_ranges,
                "apply_to": "current_design",  # Apply to entire design
                "is_soft": False  # Hard constraint
            }, timeout=300.0)
            print(f"Create and apply pblock result:\n{result}")
            logger.info(f"Create pblock result: {result}")
            
            # ================================================================
            # Step 8: Place the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Place the design in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("place_design", {
                "directive": "Default"
            }, timeout=3600.0)  # 1 hour timeout for placement
            print(f"Place design result:\n{result}")
            logger.info(f"Place design: {result}")
            
            # ================================================================
            # Step 9: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Route the design in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)  # 1 hour timeout for routing
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")
            
            # Check route status
            result = await self.call_vivado_tool("report_route_status", {}, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")
            
            # ================================================================
            # Step 10: Report timing and compare WNS
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 10: Report final timing")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")
            
            # Get final WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()
            
            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)
            
            # Always write the final checkpoint
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            
            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - LOGICNETS PBLOCK OPTIMIZATION",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Pblock applied: {pblock_ranges}"
            )
            
            return True
            
        except Exception as e:
            logger.exception(f"LogicNets test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_vexriscv(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Cell re-placement optimization flow for VexRiscv.
        
        Mirrors the script in docs/optimization_example.md:
          Step 1 — Vivado baseline (open, get Fmax, extract critical path pins)
          Step 2 — RapidWright analysis (analyze_net_detour, filter candidates)
          Step 3 — RapidWright optimization (optimize_cell_placement, write DCP)
          Step 4 — Vivado verification (open optimized DCP, route, measure Fmax)
        """
        overall_start = time.time()
        
        try:
            # ==============================================================
            # Step 1: Vivado baseline
            # ==============================================================
            print("=" * 60)
            print("Step 1  Vivado baseline")
            print("=" * 60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            logger.info(f"Open checkpoint result: {result}")
            
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                ts = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                self.initial_wns = self.parse_wns_from_timing_report(ts)
            
            baseline_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            print(f"  Clock period:   {self.clock_period} ns")
            print(f"  Baseline WNS:   {self.initial_wns} ns")
            if baseline_fmax is not None:
                print(f"  Baseline Fmax:  {baseline_fmax:.2f} MHz")
            
            pins_file = Path(self.temp_dir) / "critical_path_pins.json"
            result = await self.call_vivado_tool("extract_critical_path_pins", {
                "num_paths": 10,
                "output_file": str(pins_file)
            }, timeout=600.0)
            
            critical_paths = json.loads(Path(pins_file).read_text()) if pins_file.exists() else json.loads(result)
            print(f"  Extracted {len(critical_paths)} critical path pin lists")
            
            # ==============================================================
            # Step 2: RapidWright analysis
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 2  RapidWright analysis")
            print("=" * 60)
            
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            logger.info(f"RapidWright init: {result}")
            
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            logger.info(f"RapidWright read checkpoint: {result}")
            
            result = await self.call_rapidwright_tool("analyze_net_detour", {
                "input_file": str(pins_file),
                "detour_threshold": 2.0
            }, timeout=300.0)
            logger.info(f"analyze_net_detour: {result}")
            
            analysis = json.loads(result) if isinstance(result, str) else result
            if "error" in analysis:
                raise RuntimeError(f"analyze_net_detour failed: {analysis['error']}")
            candidates = analysis.get("candidates", [])
            print(f"  Cells analyzed: {analysis.get('cells_analyzed', '?')}")
            print(f"  Candidates (detour > 2.0): {len(candidates)}")
            for c in candidates[:5]:
                print(f"    {str(c['cell']):55s}  ratio={c['max_detour_ratio']}")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "vexriscv",
                extra_sections={
                    "detour_analysis": {
                        "cells_analyzed": analysis.get("cells_analyzed"),
                        "candidate_count": len(candidates),
                        "top_candidates": [
                            {
                                "cell": str(c["cell"]),
                                "path": c.get("path"),
                                "max_detour_ratio": c.get("max_detour_ratio"),
                            }
                            for c in candidates[:10]
                        ],
                    },
                },
            )

            if not candidates:
                print("\n  No candidates found — nothing to optimize")
                self.final_wns = self.initial_wns
                return True
            
            worst_path_cells = list(set(
                str(c["cell"]) for c in candidates if c.get("path", 0) <= 2
            ))
            if not worst_path_cells:
                worst_path_cells = [str(candidates[0]["cell"])]
            
            print(f"\n  Targeting {len(worst_path_cells)} cells on paths 1-2:")
            for name in worst_path_cells:
                print(f"    {name}")
            
            # ==============================================================
            # Step 3: RapidWright optimization
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 3  RapidWright optimization")
            print("=" * 60)
            
            result = await self.call_rapidwright_tool("optimize_cell_placement", {
                "cell_names": worst_path_cells
            }, timeout=300.0)
            logger.info(f"optimize_cell_placement: {result}")
            
            opt_result = json.loads(result) if isinstance(result, str) else result
            for r in opt_result.get("results", []):
                print(f"  {r['cell']}: {r['status']} — {r['message']}")
            
            rw_output = Path(self.temp_dir) / "vexriscv_rw_optimized.dcp"
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rw_output)
            }, timeout=600.0)
            print(f"  Wrote {rw_output.name}")
            
            # ==============================================================
            # Step 4: Vivado verification
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 4  Vivado verification")
            print("=" * 60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(rw_output)
            }, timeout=600.0)
            logger.info(f"Open optimized checkpoint: {result}")
            
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            logger.info(f"Route design: {result}")
            
            route_result = await self.call_vivado_tool("report_route_status", {}, timeout=300.0)
            error_match = re.search(r"# of nets with routing errors.*?:\s+(\d+)", route_result)
            error_count = int(error_match.group(1)) if error_match else -1
            
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                ts = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                self.final_wns = self.parse_wns_from_timing_report(ts)
            
            new_fmax = self.calculate_fmax(self.final_wns, self.clock_period)
            
            print(f"  Routing errors:  {error_count}")
            if baseline_fmax is not None and new_fmax is not None:
                print(f"  Baseline WNS:    {self.initial_wns} ns  →  Fmax {baseline_fmax:.2f} MHz")
                print(f"  Optimized WNS:   {self.final_wns} ns  →  Fmax {new_fmax:.2f} MHz")
                delta = new_fmax - baseline_fmax
                print(f"  Fmax improvement: {delta:+.2f} MHz")
            else:
                print(f"  Baseline WNS:  {self.initial_wns} ns")
                print(f"  Optimized WNS: {self.final_wns} ns")
            
            # Write final DCP
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            
            # Summary
            elapsed = time.time() - overall_start
            cells_info = ", ".join(worst_path_cells)
            self.print_test_summary(
                title="TEST SUMMARY - VEXRISCV CELL RE-PLACEMENT",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Cells re-placed: {cells_info}"
            )
            
            return True
            
        except Exception as e:
            logger.exception(f"VexRiscv test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_phys_opt(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Physical optimization flow using Vivado's phys_opt_design command.

        Applies Vivado physical optimization in two passes:
          - Post-place: fanout replication, critical-cell opt, DSP/BRAM register moves, LUT restructure
          - Post-route: placement and routing optimization on negative-slack nets

        This flow is well-suited for designs that are already placed/routed but have not
        gone through phys_opt_design, and for DSP/BRAM-heavy benchmarks (finn, ispd16, boom_soc).

        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado (baseline WNS)
        3. Run phys_opt_design (Explore) - post-place pass
        4. Route the design in Vivado
        5. Run phys_opt_design (Explore) - post-route pass
        6. Report final timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - PHYS_OPT FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # ================================================================
            # Step 0: Initialize RapidWright (not strictly needed here but
            #         keeps the flow consistent with other test methods and
            #         ensures the MCP server is fully warmed up)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # ================================================================
            # Step 2: Report timing (baseline WNS)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            # Get clock period and baseline WNS
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(input_dcp, "phys_opt")

            # ================================================================
            # Step 3: Post-place physical optimization (Explore directive)
            #
            # Explore runs multiple passes including fanout replication,
            # critical-cell replication, DSP/BRAM register moves, and LUT
            # restructuring.  This pass runs before routing so the placer
            # can still move cells freely.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Post-place phys_opt_design (Explore)")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "directive": "Explore"
            }, timeout=3600.0)
            print(f"Post-place phys_opt_design result:\n{result[:2000]}...")
            logger.info(f"Post-place phys_opt_design: {result}")

            # ================================================================
            # Step 4: Route the design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Route design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            # Check route status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            # ================================================================
            # Step 5: Post-route physical optimization (Explore directive)
            #
            # After routing the physical optimizer can perform routing-aware
            # placement moves and optimize routing on critical nets.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Post-route phys_opt_design (Explore)")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "directive": "Explore"
            }, timeout=3600.0)
            print(f"Post-route phys_opt_design result:\n{result[:2000]}...")
            logger.info(f"Post-route phys_opt_design: {result}")

            # ================================================================
            # Step 6: Report final timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            # Get final WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            # Always write the final checkpoint (regardless of improvement)
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - PHYS_OPT FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="Directives: post-place Explore + route + post-route Explore"
            )

            return True

        except Exception as e:
            logger.exception(f"Phys-opt test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_place_route(self, input_dcp: Path, output_dcp: Path) -> bool:
        """Re-place and re-route a root without benchmark-specific policy.

        This is the production ``macro.place_route`` wrapper.  Preparation is
        deliberately explicit and bounded (unroute, unplace), followed by
        exactly one implementation-heavy placement and routing call.  It does
        not inspect filenames, families, historical winners or QoR to alter
        the sequence.
        """
        overall_start = time.time()
        self._wp013_call_accounting = {
            "schema_version": "dof.wp013-macro-call-accounting.v1",
            "macro": "place_route",
            "declared_light_mutations": 2,
            "declared_heavy_calls": 2,
            "started_light_mutations": 0,
            "started_heavy_calls": 0,
            "sequence": [],
        }

        async def accounted(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            timeout: float,
            event_class: str,
        ) -> str:
            key = (
                "started_heavy_calls"
                if event_class == "IMPLEMENTATION_HEAVY"
                else "started_light_mutations"
            )
            self._wp013_call_accounting[key] += 1
            self._wp013_call_accounting["sequence"].append(
                {"operation": tool_name, "event_class": event_class}
            )
            return await self.call_vivado_tool(tool_name, arguments, timeout=timeout)

        try:
            await self.call_vivado_tool(
                "open_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                timeout=600.0,
            )
            initial = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            timing = parse_timing_summary_static(initial)
            self.initial_tns = timing["tns"]
            self.initial_failing_endpoints = timing["failing_endpoints"]
            self.clock_period = await self.fetch_clock_period()
            self.initial_wns = await self.get_wns_for_target_clock(
                self._call_vivado_for_clock
            )
            if self.initial_wns is None:
                self.initial_wns = timing["wns"]

            await accounted(
                "run_tcl",
                {"command": "route_design -unroute"},
                timeout=300.0,
                event_class="MUTATION_LIGHT",
            )
            await accounted(
                "run_tcl",
                {"command": "place_design -unplace"},
                timeout=300.0,
                event_class="MUTATION_LIGHT",
            )
            await accounted(
                "place_design",
                {"directive": "Default"},
                timeout=3600.0,
                event_class="IMPLEMENTATION_HEAVY",
            )
            await accounted(
                "route_design",
                {"directive": "Default"},
                timeout=3600.0,
                event_class="IMPLEMENTATION_HEAVY",
            )
            route_status = await self.call_vivado_tool(
                "report_route_status",
                {"show_unrouted": True, "show_errors": True, "max_nets": 20},
                timeout=300.0,
            )
            final = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            self.final_wns = await self.get_wns_for_target_clock(
                self._call_vivado_for_clock
            )
            if self.final_wns is None:
                self.final_wns = parse_timing_summary_static(final)["wns"]
            await self.call_vivado_tool(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )
            self._wp013_call_accounting.update(
                {
                    "route_status_sha256": hashlib.sha256(
                        str(route_status).encode("utf-8")
                    ).hexdigest(),
                    "elapsed_seconds": time.time() - overall_start,
                    "success": output_dcp.exists(),
                }
            )
            return bool(output_dcp.exists())
        except Exception as exc:
            logger.exception("place_route flow failed: %s", exc)
            self._wp013_call_accounting.update(
                {
                    "elapsed_seconds": time.time() - overall_start,
                    "success": False,
                    "error_class": type(exc).__name__,
                }
            )
            return False

    async def run_test_rescue_place_reset_retime(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        No-improvement rescue flow using a larger placement-space reset.

        This is intentionally a fixed, allowlisted recipe.  It mirrors the
        manually validated VexRiscv-v2 sequence without allowing an LLM to emit
        arbitrary Tcl:

          route_design -unroute
          place_design -unplace
          place_design -directive Explore
          phys_opt_design -directive AddRetime
          route_design -directive Explore
          phys_opt_design -directive Explore
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - RESCUE PLACE RESET + RETIME FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            self.save_analysis_context(input_dcp, "rescue_place_reset_retime")

            print("\n" + "-"*60)
            print("STEP 3: Unroute design")
            print("-"*60)

            result = await self.call_vivado_tool("run_tcl", {
                "command": "route_design -unroute"
            }, timeout=600.0)
            print(f"Unroute result:\n{result[:1500]}...")
            logger.info(f"Rescue unroute: {result}")

            print("\n" + "-"*60)
            print("STEP 4: Unplace design")
            print("-"*60)

            result = await self.call_vivado_tool("run_tcl", {
                "command": "place_design -unplace"
            }, timeout=600.0)
            print(f"Unplace result:\n{result[:1500]}...")
            logger.info(f"Rescue unplace: {result}")

            print("\n" + "-"*60)
            print("STEP 5: Place design (Explore)")
            print("-"*60)

            result = await self.call_vivado_tool("place_design", {
                "directive": "Explore"
            }, timeout=3600.0)
            print(f"Place Explore result:\n{result[:2000]}...")
            logger.info(f"Rescue place Explore: {result}")

            print("\n" + "-"*60)
            print("STEP 6: Phys opt (AddRetime)")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "directive": "AddRetime"
            }, timeout=3600.0)
            print(f"Phys opt AddRetime result:\n{result[:2000]}...")
            logger.info(f"Rescue phys_opt AddRetime: {result}")

            print("\n" + "-"*60)
            print("STEP 7: Route design (Explore)")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Explore"
            }, timeout=3600.0)
            print(f"Route Explore result:\n{result[:2000]}...")
            logger.info(f"Rescue route Explore: {result}")

            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Rescue route status: {result}")

            print("\n" + "-"*60)
            print("STEP 8: Post-route phys opt (Explore)")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "directive": "Explore"
            }, timeout=3600.0)
            print(f"Post-route phys_opt Explore result:\n{result[:2000]}...")
            logger.info(f"Rescue post-route phys_opt Explore: {result}")

            print("\n" + "-"*60)
            print("STEP 9: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - RESCUE PLACE RESET + RETIME FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="route -unroute + place -unplace + Explore place + AddRetime + Explore route/phys_opt"
            )
            return True

        except Exception as e:
            logger.exception(f"Rescue place-reset-retime test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_synthesized_flow_primitive(self, flow_name: str, input_dcp: Path, output_dcp: Path) -> bool:
        """Dispatch a synthesized recipe flow primitive through existing named flows."""
        if flow_name == "phys_opt":
            return await self.run_test_phys_opt(input_dcp, output_dcp)
        if flow_name == "pblock":
            return await self.run_test_logicnets(input_dcp, output_dcp)
        if flow_name == "auto_pblock":
            return await self.run_test_auto_pblock(input_dcp, output_dcp)
        if flow_name == "lut_cone":
            return await self.run_test_lut_cone(input_dcp, output_dcp)
        if flow_name == "retime":
            return await self.run_test_retime(input_dcp, output_dcp)
        if flow_name == "fanout":
            return await self.run_test(input_dcp, output_dcp, max_nets_to_optimize=getattr(self, "max_nets", 5))
        if flow_name == "memory_path_opt":
            return await self.run_test_memory_path_opt(input_dcp, output_dcp)
        if flow_name == "hardblock_relocate":
            return await self.run_test_hardblock_relocate(input_dcp, output_dcp)
        if flow_name == "congestion_spread":
            return await self.run_test_congestion_spread(input_dcp, output_dcp)
        if flow_name == "net_swap":
            return await self.run_test_net_swap(input_dcp, output_dcp)
        raise ValueError(f"Unsupported synthesized flow primitive: {flow_name}")

    async def run_test_synthesized_recipe(self, input_dcp: Path, output_dcp: Path, recipe_spec: Dict[str, Any]) -> bool:
        """Execute a guarded recipe assembled from allowlisted primitives."""
        recipe = _recipe_spec_from_dict(recipe_spec)
        catalog = PrimitiveCatalog.default()
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - SYNTHESIZED GUARDED RECIPE")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Recipe ID:  {recipe.recipe_id}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()
        step_results: List[Dict[str, Any]] = []

        try:
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            result = await self.call_rapidwright_tool("initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"Synthesized recipe RapidWright init result: {result}")

            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("open_checkpoint", {"dcp_path": str(input_dcp.resolve())}, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Synthesized recipe open checkpoint result: {result}")

            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Synthesized recipe initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            self.initial_wns = target_wns if target_wns is not None else self.parse_wns_from_timing_report(result)
            recipe_initial_wns = self.initial_wns
            recipe_clock_period = self.clock_period
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Synthesized recipe initial WNS: {self.initial_wns} ns")
            self.save_analysis_context(input_dcp, "synthesized_recipe", extra_sections={"recipe_spec": recipe.to_dict()})

            route_clean = True
            current_checkpoint = Path(input_dcp)
            dirty_since_checkpoint = False
            for idx, step in enumerate(recipe.steps, start=1):
                primitive = catalog.get(step.primitive)
                if primitive is None:
                    raise ValueError(f"Unknown synthesized recipe primitive: {step.primitive}")
                print("\n" + "-"*60)
                print(f"STEP {idx + 2}: {step.primitive}")
                print("-"*60)
                started = time.time()
                if primitive.kind is PrimitiveKind.MACRO_TCL:
                    result = await self.call_vivado_tool("run_tcl", {"command": primitive.macro_command}, timeout=600.0)
                    dirty_since_checkpoint = True
                elif primitive.kind is PrimitiveKind.TYPED_VIVADO:
                    result = await self.call_vivado_tool(str(primitive.tool_name), dict(step.args), timeout=3600.0)
                    dirty_since_checkpoint = True
                elif primitive.kind is PrimitiveKind.OPTIMIZER_FLOW:
                    flow_name = str(primitive.flow_name or "")
                    if dirty_since_checkpoint:
                        current_checkpoint = Path(self.temp_dir) / f"synth_recipe_step_{idx:02d}_input.dcp"
                        checkpoint_result = await self.call_vivado_tool(
                            "write_checkpoint",
                            {"dcp_path": str(current_checkpoint.resolve()), "force": True},
                            timeout=600.0,
                        )
                        logger.info(f"Synthesized recipe checkpoint before flow {flow_name}: {checkpoint_result}")
                        dirty_since_checkpoint = False
                    step_output = Path(self.temp_dir) / f"synth_recipe_step_{idx:02d}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', flow_name)}.dcp"
                    flow_success = await self.run_test_synthesized_flow_primitive(flow_name, current_checkpoint, step_output)
                    elapsed = time.time() - started
                    row = {
                        "step_id": step.step_id,
                        "primitive": step.primitive,
                        "args": dict(step.args),
                        "flow_name": flow_name,
                        "elapsed_seconds": elapsed,
                        "flow_success": bool(flow_success),
                        "flow_output_dcp": str(step_output),
                    }
                    step_results.append(row)
                    if not flow_success or not step_output.exists():
                        print(f"\n*** SYNTHESIZED RECIPE FAILED: flow primitive {flow_name} failed ***")
                        route_clean = False
                        break
                    current_checkpoint = step_output
                    result = await self.call_vivado_tool("open_checkpoint", {"dcp_path": str(current_checkpoint.resolve())}, timeout=600.0)
                    dirty_since_checkpoint = False
                    logger.info(f"Synthesized recipe reopened flow output {flow_name}: {result}")
                    continue
                else:
                    raise ValueError(f"Primitive kind is disabled for synthesized recipes: {primitive.kind.value}")
                elapsed = time.time() - started
                print(f"Result:\n{result[:2000]}...")
                logger.info(f"Synthesized recipe step {idx} {step.primitive}: {result}")
                row = {
                    "step_id": step.step_id,
                    "primitive": step.primitive,
                    "args": dict(step.args),
                    "elapsed_seconds": elapsed,
                    "result_preview": result[:2000],
                }
                if primitive.tool_name == "route_design":
                    status = await self.call_vivado_tool(
                        "report_route_status",
                        {"show_unrouted": True, "show_errors": True, "max_nets": 20},
                        timeout=300.0,
                    )
                    print(f"Route status after routing:\n{status[:1500]}...")
                    logger.info(f"Synthesized recipe route status after step {idx}: {status}")
                    route_clean = _route_status_clean(status)
                    row["route_status_preview"] = status[:2000]
                    row["route_clean"] = route_clean
                step_results.append(row)
                if not route_clean:
                    print("\n*** SYNTHESIZED RECIPE FAILED: route status is not clean ***")
                    break
            if not route_clean:
                self.initial_wns = recipe_initial_wns
                self.clock_period = recipe_clock_period
                self.final_wns = self.initial_wns
                return False

            print("\n" + "-"*60)
            print("FINAL: Report final timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Synthesized recipe final timing summary: {result}")
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            self.final_wns = target_wns if target_wns is not None else self.parse_wns_from_timing_report(result)
            self.initial_wns = recipe_initial_wns
            self.clock_period = recipe_clock_period
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Synthesized recipe final WNS: {self.final_wns} ns")
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {"dcp_path": str(output_dcp.resolve()), "force": True}, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                "TEST SUMMARY - SYNTHESIZED GUARDED RECIPE",
                elapsed,
                self.initial_wns,
                self.final_wns,
                self.clock_period,
                extra_info=f"Recipe: {recipe.recipe_id} ({len(recipe.steps)} primitive steps)",
            )
            self.save_run_summary(
                input_dcp=input_dcp,
                output_dcp=output_dcp,
                mode="test",
                flow="synthesized_recipe",
                optimizers_tried=[{"flow": "synthesized_recipe", "recipe_id": recipe.recipe_id, "steps": [step.to_dict() for step in recipe.steps]}],
                extra={"recipe_spec": recipe.to_dict(), "recipe_step_results": step_results},
            )
            self._synthesized_recipe_step_results = list(step_results)
            self._synthesized_recipe_spec = recipe.to_dict()
            return True

        except Exception as e:
            logger.exception(f"Synthesized recipe test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_manual_action(self, input_dcp: Path, output_dcp: Path, manual_action_spec: Dict[str, Any]) -> bool:
        """Execute exactly one allowlisted manual rescue primitive."""
        validator = ManualActionValidator(ManualPrimitiveCatalog.default())
        validation = validator.validate_action(manual_action_spec, source="manual_action_runtime")
        if not validation.valid or validation.action is None:
            print(f"\n*** MANUAL ACTION VALIDATION FAILED: {validation.errors} ***")
            self._manual_action_result = {
                "manual_action_spec": manual_action_spec,
                "validation": validation.to_dict(),
                "success": False,
            }
            return False
        action = validation.action
        primitive = validator.catalog.get(action.primitive)
        if primitive is None:
            return False

        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - MANUAL RESCUE ACTION")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Action:     {action.primitive}")
        print(f"Args:       {action.args}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()
        route_status = None
        route_clean = None
        timing_summary = None
        action_result = None
        success = False
        try:
            result = await self.call_vivado_tool("open_checkpoint", {"dcp_path": str(input_dcp.resolve())}, timeout=600.0)
            logger.info(f"Manual action open checkpoint result: {result}")
            baseline = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            self.initial_wns = target_wns if target_wns is not None else self.parse_wns_from_timing_report(baseline)
            self.save_analysis_context(input_dcp, "manual_action", extra_sections={"manual_action_spec": action.to_dict()})

            started = time.time()
            if primitive.kind is ManualPrimitiveKind.MACRO_TCL:
                action_result = await self.call_vivado_tool("run_tcl", {"command": primitive.macro_command}, timeout=600.0)
            elif primitive.kind is ManualPrimitiveKind.TYPED_VIVADO:
                action_result = await self.call_vivado_tool(str(primitive.tool_name), dict(action.args), timeout=3600.0)
            else:
                raise ValueError(f"Unsupported manual primitive kind: {primitive.kind}")
            action_elapsed = time.time() - started
            print(f"Manual action result:\n{str(action_result)[:2000]}...")

            try:
                timing_summary = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
                self.final_wns = target_wns if target_wns is not None else self.parse_wns_from_timing_report(timing_summary)
            except Exception as exc:
                logger.warning(f"Manual action timing report failed: {exc}")
                self.final_wns = self.initial_wns

            try:
                route_status = await self.call_vivado_tool(
                    "report_route_status",
                    {"show_unrouted": True, "show_errors": True, "max_nets": 20},
                    timeout=300.0,
                )
                route_clean = _route_status_clean(route_status)
            except Exception as exc:
                logger.warning(f"Manual action route status failed: {exc}")

            result = await self.call_vivado_tool("write_checkpoint", {"dcp_path": str(output_dcp.resolve()), "force": True}, timeout=600.0)
            logger.info(f"Manual action write checkpoint result: {result}")
            success = output_dcp.exists()
            elapsed = time.time() - overall_start
            self._manual_action_result = {
                "manual_action_spec": action.to_dict(),
                "validation": validation.to_dict(),
                "action_elapsed_seconds": action_elapsed,
                "total_runtime_seconds": elapsed,
                "route_clean": route_clean,
                "route_status_preview": str(route_status or "")[:2000],
                "timing_summary_preview": str(timing_summary or "")[:2000],
                "result_preview": str(action_result or "")[:2000],
                "success": bool(success),
            }
            self.print_test_summary(
                "TEST SUMMARY - MANUAL RESCUE ACTION",
                elapsed,
                self.initial_wns,
                self.final_wns,
                self.clock_period,
                extra_info=f"Manual action: {action.primitive}",
            )
            self.save_run_summary(
                input_dcp=input_dcp,
                output_dcp=output_dcp,
                mode="test",
                flow="manual_action",
                optimizers_tried=[{"flow": "manual_action", "manual_action_spec": action.to_dict()}],
                extra={"manual_action": self._manual_action_result},
            )
            return bool(success)
        except Exception as e:
            logger.exception(f"Manual action test failed with exception: {e}")
            print(f"\n*** MANUAL ACTION FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            self.final_wns = getattr(self, "final_wns", None) or getattr(self, "initial_wns", None)
            self._manual_action_result = {
                "manual_action_spec": action.to_dict(),
                "validation": validation.to_dict(),
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "total_runtime_seconds": time.time() - overall_start,
            }
            return False

    async def run_test_retime(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Register retiming optimization flow.

        Moves flip-flops across combinational logic to balance pipeline stage delays.
        Uses Vivado's phys_opt_design with the AlternateFlowWithRetiming directive
        for aggressive register retiming and DSP/BRAM register optimization.

        Steps:
        1. Open the input DCP in Vivado
        2. Report timing (baseline WNS)
        3. Extract critical paths for optional analysis
        4. Run phys_opt_design with AlternateFlowWithRetiming
        5. Route the design in Vivado
        6. Report final timing and compare WNS
        7. Write optimized DCP
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - REGISTER RETIMING FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # ================================================================
            # Step 2: Report baseline timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()

            # ================================================================
            # Step 3: Extract critical paths for diagnostic purposes (optional)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Extract critical path information")
            print("-"*60)

            critical_paths_file = Path(self.temp_dir) / "critical_paths_retime.json"
            try:
                result = await self.call_vivado_tool("extract_critical_path_cells", {
                    "num_paths": 20,
                    "output_file": str(critical_paths_file)
                }, timeout=600.0)
                print(f"Extract critical paths result:\n{result[:1500]}...")
                logger.info(f"Extract critical paths: {result}")
            except Exception as e:
                logger.warning(f"Could not extract critical paths: {e}")
                print(f"[WARN] Could not extract critical paths: {e}")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "retime",
                extra_sections={
                    "critical_paths": {
                        "extracted": critical_paths_file.exists(),
                        "file": str(critical_paths_file.name),
                    },
                },
            )

            # ================================================================
            # Step 4: Place the design (if not already placed)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Place design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("place_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Place design result:\n{result}")
            logger.info(f"Place design: {result}")

            # ================================================================
            # Step 5: Physical optimization with register retiming
            # (AlternateFlowWithRetiming directive includes:
            #  - More aggressive replication
            #  - Register retiming (retime=true parameter)
            #  - DSP/BRAM register optimization)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: phys_opt_design with AlternateFlowWithRetiming")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "directive": "AlternateFlowWithRetiming"
            }, timeout=3600.0)
            print(f"phys_opt_design result:\n{result[:2000]}...")
            logger.info(f"phys_opt_design: {result}")

            # ================================================================
            # Step 6: Route the design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Route design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            # Check route status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            # ================================================================
            # Step 7: Report final timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            # Get final WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            # Always write the final checkpoint (regardless of improvement)
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - REGISTER RETIMING FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="Directive: AlternateFlowWithRetiming (aggressive register retiming + DSP/BRAM optimization)"
            )

            return True

        except Exception as e:
            logger.exception(f"Register retiming test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_net_swap(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Net swapping / equivalent-driver rewiring optimization flow.

        Uses Vivado phys_opt_design options focused on pin remapping and
        equivalent-driver rewiring:
          - critical_pin_opt: LUT pin swapping
          - equ_drivers_opt: rewire loads to equivalent drivers

        Steps:
        1. Open the input DCP in Vivado
        2. Report baseline timing
        3. Run targeted phys_opt_design (pin/driver rewiring)
        4. Route design
        5. Optionally run a second targeted pass
        6. Report final timing and write checkpoint
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - NET SWAP FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # ================================================================
            # Step 1: Open input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # ================================================================
            # Step 2: Report baseline timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(input_dcp, "net_swap")

            # ================================================================
            # Step 3: Targeted phys_opt for net/pin swapping
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Run targeted phys_opt_design for net swapping")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "critical_pin_opt": True,
                "equ_drivers_opt": True,
                "placement_opt": True
            }, timeout=3600.0)
            print(f"Targeted phys_opt result:\n{result[:2000]}...")
            logger.info(f"Targeted phys_opt pass 1: {result}")

            # ================================================================
            # Step 4: Route design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Route design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            # ================================================================
            # Step 5: Optional second focused pass (same transforms)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Second targeted phys_opt pass")
            print("-"*60)

            result = await self.call_vivado_tool("phys_opt_design", {
                "critical_pin_opt": True,
                "equ_drivers_opt": True,
                "routing_opt": True
            }, timeout=3600.0)
            print(f"Second targeted phys_opt result:\n{result[:2000]}...")
            logger.info(f"Targeted phys_opt pass 2: {result}")

            # ================================================================
            # Step 6: Final timing report
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()

            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - NET SWAP FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="Targeted phys_opt options: critical_pin_opt + equ_drivers_opt (+ routing/placement)"
            )

            return True

        except Exception as e:
            logger.exception(f"Net-swap test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_memory_path_opt(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Memory-path-focused optimization flow.

        Targets BRAM/DSP-heavy critical paths using phys_opt options tuned for
        memory-interface timing:
          - bram_register_opt / dsp_register_opt / uram_register_opt
          - bram_enable_opt
          - critical_pin_opt / equ_drivers_opt

        This flow is intended for designs where the worst path repeatedly crosses
        BRAM/DSP logic cones.
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - MEMORY PATH OPT FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            print("\n" + "-"*60)
            print("STEP 2: Baseline timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(input_dcp, "memory_path_opt")

            await self.ensure_placement_stage("memory_path_opt pre-route opts")

            print("\n" + "-"*60)
            print("STEP 3: Phys-opt pass 1 (memory-centric options)")
            print("-"*60)
            result = await self.call_vivado_tool("phys_opt_design", {
                "bram_register_opt": True,
                "dsp_register_opt": True,
                "uram_register_opt": True,
                "bram_enable_opt": True,
                "critical_pin_opt": True,
                "equ_drivers_opt": True,
                "placement_opt": True
            }, timeout=3600.0)
            print(f"Memory path phys_opt pass 1:\n{result[:2000]}...")
            logger.info(f"Memory path phys_opt pass 1: {result}")

            print("\n" + "-"*60)
            print("STEP 4: Route design")
            print("-"*60)
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            print("\n" + "-"*60)
            print("STEP 5: Phys-opt pass 2 (memory rewiring cleanup)")
            print("-"*60)
            result = await self.call_vivado_tool("phys_opt_design", {
                "critical_pin_opt": True,
                "equ_drivers_opt": True,
                "routing_opt": True
            }, timeout=3600.0)
            print(f"Memory path phys_opt pass 2:\n{result[:2000]}...")
            logger.info(f"Memory path phys_opt pass 2: {result}")

            print("\n" + "-"*60)
            print("STEP 6: Final timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")

            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - MEMORY PATH OPT FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="Phys-opt options: BRAM/DSP/URAM register opt + BRAM enable + pin/driver rewiring"
            )
            return True

        except Exception as e:
            logger.exception(f"Memory-path optimization failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_memory_path_opt_rw(self, input_dcp: Path, output_dcp: Path) -> bool:
        """RapidWright-assisted variant of memory_path_opt using detour pre-pass."""
        rw_input = await self.build_rw_detour_prepass_checkpoint(
            input_dcp=input_dcp,
            tag="memory_path_opt",
            num_paths=40,
            detour_threshold=1.7,
            max_candidates=30,
        )
        effective_input = rw_input if rw_input is not None else input_dcp
        return await self.run_test_memory_path_opt(effective_input, output_dcp)

    async def run_test_hardblock_relocate(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Hard-block relocation-oriented flow.

        Uses timing extraction plus hard-block-aware phys_opt to encourage
        BRAM/DSP register movement and critical-cell relocation near hard blocks.
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - HARDBLOCK RELOCATE FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            print("\n" + "-"*60)
            print("STEP 2: Baseline timing and hard-block path sampling")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")

            await self.ensure_placement_stage("hardblock_relocate pre-route opts")

            hardblock_cells_file = Path(self.temp_dir) / "hardblock_critical_cells.json"
            extract_result = await self.call_vivado_tool("extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(hardblock_cells_file)
            }, timeout=600.0)
            logger.info(f"Extract hard-block critical cells: {extract_result}")

            hardblock_count = 0
            if hardblock_cells_file.exists():
                try:
                    cells_data = json.loads(hardblock_cells_file.read_text())
                    for path_cells in cells_data:
                        for c in path_cells:
                            c_upper = str(c).upper()
                            if "RAMB" in c_upper or "DSP" in c_upper or "URAM" in c_upper:
                                hardblock_count += 1
                    print(f"Detected hard-block cells on critical paths: {hardblock_count}")
                except Exception:
                    print("Could not parse critical path cells JSON; continuing with generic hard-block flow")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "hardblock_relocate",
                extra_sections={
                    "hardblock_info": {
                        "cells_on_critical_paths": hardblock_count,
                    },
                },
            )

            print("\n" + "-"*60)
            print("STEP 3: Phys-opt pass 1 (hard-block relocation oriented)")
            print("-"*60)
            result = await self.call_vivado_tool("phys_opt_design", {
                "placement_opt": True,
                "critical_cell_opt": True,
                "dsp_register_opt": True,
                "bram_register_opt": True,
                "uram_register_opt": True
            }, timeout=3600.0)
            print(f"Hard-block phys_opt pass 1:\n{result[:2000]}...")
            logger.info(f"Hard-block phys_opt pass 1: {result}")

            print("\n" + "-"*60)
            print("STEP 4: Route design")
            print("-"*60)
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            print("\n" + "-"*60)
            print("STEP 5: Phys-opt pass 2 (final hard-block cleanup)")
            print("-"*60)
            result = await self.call_vivado_tool("phys_opt_design", {
                "placement_opt": True,
                "critical_cell_opt": True,
                "routing_opt": True
            }, timeout=3600.0)
            print(f"Hard-block phys_opt pass 2:\n{result[:2000]}...")
            logger.info(f"Hard-block phys_opt pass 2: {result}")

            print("\n" + "-"*60)
            print("STEP 6: Final timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")

            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - HARDBLOCK RELOCATE FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Hard-block cells sampled on critical paths: {hardblock_count}"
            )
            return True

        except Exception as e:
            logger.exception(f"Hard-block relocate flow failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_hardblock_relocate_rw(self, input_dcp: Path, output_dcp: Path) -> bool:
        """RapidWright-assisted variant of hardblock_relocate using detour pre-pass."""
        rw_input = await self.build_rw_detour_prepass_checkpoint(
            input_dcp=input_dcp,
            tag="hardblock_relocate",
            num_paths=50,
            detour_threshold=1.8,
            max_candidates=30,
        )
        effective_input = rw_input if rw_input is not None else input_dcp
        return await self.run_test_hardblock_relocate(effective_input, output_dcp)

    async def run_test_congestion_spread(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Congestion-aware spreading flow.

        Uses congestion analysis from Vivado and runs placement/routing-focused
        phys_opt passes to spread pressure from timing-critical congested regions.
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - CONGESTION SPREAD FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            print("\n" + "-"*60)
            print("STEP 2: Baseline timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")

            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")

            print("\n" + "-"*60)
            print("STEP 3: Baseline congestion analysis")
            print("-"*60)
            cong_before = await self.call_vivado_tool("run_tcl", {
                "command": "report_design_analysis -congestion -return_string"
            }, timeout=300.0)
            print(f"Congestion summary (first 1500 chars):\n{cong_before[:1500]}...")
            logger.info(f"Congestion before: {cong_before}")

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "congestion_spread",
                extra_sections={
                    "congestion_summary": cong_before[:1500],
                },
            )

            print("\n" + "-"*60)
            print("STEP 4: Phys-opt pass 1 (congestion-focused)")
            print("-"*60)
            result = await self.call_vivado_tool("phys_opt_design", {
                "placement_opt": True,
                "critical_cell_opt": True,
                "clock_opt": True,
                "slr_crossing_opt": True
            }, timeout=3600.0)
            print(f"Congestion phys_opt pass 1:\n{result[:2000]}...")
            logger.info(f"Congestion phys_opt pass 1: {result}")

            print("\n" + "-"*60)
            print("STEP 5: Route design")
            print("-"*60)
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            print("\n" + "-"*60)
            print("STEP 6: Post-route congestion analysis")
            print("-"*60)
            cong_after = await self.call_vivado_tool("run_tcl", {
                "command": "report_design_analysis -congestion -return_string"
            }, timeout=300.0)
            print(f"Post-route congestion summary (first 1500 chars):\n{cong_after[:1500]}...")
            logger.info(f"Congestion after: {cong_after}")

            print("\n" + "-"*60)
            print("STEP 7: Final timing")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")

            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - CONGESTION SPREAD FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info="Congestion analysis + placement/routing-focused phys_opt"
            )
            return True

        except Exception as e:
            logger.exception(f"Congestion-spread flow failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_congestion_spread_rw(self, input_dcp: Path, output_dcp: Path) -> bool:
        """RapidWright-assisted variant of congestion_spread using detour pre-pass."""
        rw_input = await self.build_rw_detour_prepass_checkpoint(
            input_dcp=input_dcp,
            tag="congestion_spread",
            num_paths=40,
            detour_threshold=1.9,
            max_candidates=25,
        )
        effective_input = rw_input if rw_input is not None else input_dcp
        return await self.run_test_congestion_spread(effective_input, output_dcp)

    async def run_test_auto_pblock(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Generalized pblock optimization flow driven by RapidWright fabric analysis.

        Unlike run_test_logicnets (which uses a hardcoded pblock range), this flow
        queries the FPGA fabric at runtime to find the optimal contiguous region for
        the current design's resource requirements.  It is intended for benchmarks
        with spread-out placement where no empirically-known range exists:
        boom_soc, corescore_500, rosetta, vtr_mcml.

        Steps:
        1. Open the input DCP in Vivado
        2. Report timing (baseline WNS)
        3. Extract critical path cells to file (Vivado)
        4. Open DCP in RapidWright; analyze critical-path spread
        5. Get design utilization for pblock sizing (Vivado)
        6. Analyze FPGA fabric for best contiguous region (RapidWright)
        7. Convert fabric region to Vivado pblock range string (RapidWright)
        8. Unplace the design in Vivado
        9. Create and apply pblock to entire design (Vivado)
        10. Place the design in Vivado
        11. Route the design in Vivado
        12. Report final timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - AUTO PBLOCK FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # ================================================================
            # Step 0: Initialize RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # ================================================================
            # Step 2: Report timing (baseline WNS)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()

            # ================================================================
            # Step 3: Extract critical path cells to JSON file
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Extract critical path cells")
            print("-"*60)

            critical_paths_file = Path(self.temp_dir) / "critical_paths.json"
            result = await self.call_vivado_tool("extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(critical_paths_file)
            }, timeout=600.0)
            print(f"Extract critical paths result:\n{result[:2000]}...")
            logger.info(f"Extract critical paths: {result}")

            # ================================================================
            # Step 4: Open DCP in RapidWright and analyze critical-path spread
            #
            # analyze_critical_path_spread computes per-path Manhattan distances
            # between consecutive cells.  avg_max_distance > 70 tiles indicates a
            # spread-out placement that benefits from a pblock constraint.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Analyze critical path spread in RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")

            spread_raw = await self.call_rapidwright_tool("analyze_critical_path_spread", {
                "input_file": str(critical_paths_file)
            }, timeout=300.0)
            print(f"Critical path spread analysis:\n{spread_raw[:3000]}...")
            logger.info(f"Critical path spread: {spread_raw}")

            try:
                spread_result = json.loads(spread_raw)
            except (json.JSONDecodeError, TypeError):
                spread_result = {}

            avg_dist = spread_result.get("avg_max_distance", 0)
            print(f"\nAverage max path distance: {avg_dist:.1f} tiles")
            pblock_recommended = avg_dist > 30  # conservative threshold
            print(f"Pblock optimization {'RECOMMENDED' if pblock_recommended else 'may not help'} "
                  f"(threshold: 30 tiles)")

            # ================================================================
            # Step 5: Get design utilization for pblock sizing
            #
            # report_utilization_for_pblock returns 1.5x resource counts,
            # which are used as targets so the chosen region has headroom.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Get design utilization for pblock sizing")
            print("-"*60)

            util_raw = await self.call_vivado_tool("report_utilization_for_pblock", {}, timeout=300.0)
            print(f"Utilization report:\n{util_raw[:2000]}...")
            logger.info(f"Utilization report: {util_raw}")

            # Parse 1.5x resource targets from the utilization report
            # Lines look like: "LUTs (1.5x): 12345" or JSON
            target_luts = 0
            target_ffs = 0
            target_dsps = 0
            target_brams = 0
            try:
                util_data = json.loads(util_raw)
                target_luts  = util_data.get("luts_1_5x",  util_data.get("luts",  0))
                target_ffs   = util_data.get("ffs_1_5x",   util_data.get("ffs",   0))
                target_dsps  = util_data.get("dsps_1_5x",  util_data.get("dsps",  0))
                target_brams = util_data.get("brams_1_5x", util_data.get("brams", 0))
            except (json.JSONDecodeError, TypeError):
                # Fall back to simple regex extraction for common report formats
                for label, key in [("LUTs", "target_luts"), ("FFs", "target_ffs"),
                                   ("DSPs", "target_dsps"), ("BRAMs", "target_brams")]:
                    m = re.search(rf"{label}.*?1\.5x.*?:\s*(\d+)", util_raw, re.IGNORECASE)
                    if m:
                        locals()[key]  # just touch to avoid unused warnings
                        if label == "LUTs":   target_luts  = int(m.group(1))
                        elif label == "FFs":  target_ffs   = int(m.group(1))
                        elif label == "DSPs": target_dsps  = int(m.group(1))
                        else:                 target_brams = int(m.group(1))

            # Use safe minimums so analyze_fabric_for_pblock always gets useful input
            target_luts  = max(target_luts,  1000)
            target_ffs   = max(target_ffs,   1000)
            print(f"Pblock targets  LUTs={target_luts}  FFs={target_ffs}  "
                  f"DSPs={target_dsps}  BRAMs={target_brams}")

            # ================================================================
            # Step 6: Analyze FPGA fabric for best contiguous region
            #
            # analyze_fabric_for_pblock walks the device tile map and finds the
            # largest contiguous column range that satisfies the resource targets
            # while avoiding delay-heavy columns (URAM, HPIO, etc.).
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Analyze FPGA fabric for best pblock region")
            print("-"*60)

            fabric_raw = await self.call_rapidwright_tool("analyze_fabric_for_pblock", {
                "target_lut_count":  target_luts,
                "target_ff_count":   target_ffs,
                "target_dsp_count":  target_dsps,
                "target_bram_count": target_brams
            }, timeout=300.0)
            print(f"Fabric analysis result:\n{fabric_raw[:3000]}...")
            logger.info(f"Fabric analysis: {fabric_raw}")

            try:
                fabric_result = json.loads(fabric_raw)
            except (json.JSONDecodeError, TypeError):
                fabric_result = {}

            if "error" in fabric_result:
                print(f"WARNING: Fabric analysis error: {fabric_result['error']}")
                print("Falling through to route-only with no pblock constraint")
                fabric_result = {}

            # Extract column/row bounds from the fabric analysis
            col_min = fabric_result.get("col_min", fabric_result.get("best_col_start", 0))
            col_max = fabric_result.get("col_max", fabric_result.get("best_col_end",   200))
            row_min = fabric_result.get("row_min", fabric_result.get("best_row_start", 0))
            row_max = fabric_result.get("row_max", fabric_result.get("best_row_end",   300))
            print(f"Fabric region: cols {col_min}-{col_max}, rows {row_min}-{row_max}")

            # ================================================================
            # Step 7: Convert fabric region to Vivado pblock range string
            #
            # use_clock_regions=False gives fine-grained SLICE_XaYb ranges
            # which are more precise than CLOCKREGION boundaries.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Convert fabric region to pblock range string")
            print("-"*60)

            pblock_raw = await self.call_rapidwright_tool("convert_fabric_region_to_pblock", {
                "col_min": col_min,
                "col_max": col_max,
                "row_min": row_min,
                "row_max": row_max,
                "use_clock_regions": False
            }, timeout=300.0)
            print(f"Pblock ranges result:\n{pblock_raw[:2000]}...")
            logger.info(f"Pblock ranges: {pblock_raw}")

            try:
                pblock_data = json.loads(pblock_raw)
                pblock_ranges = pblock_data.get("pblock_ranges", "")
            except (json.JSONDecodeError, TypeError):
                pblock_ranges = pblock_raw.strip()

            if not pblock_ranges:
                print("WARNING: Could not determine pblock ranges - skipping pblock constraint")
                pblock_ranges = None
            else:
                print(f"Using pblock range: {pblock_ranges}")

            # Save analysis context (end of analysis / planning phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "auto_pblock",
                extra_sections={
                    "critical_path_spread": {
                        "avg_max_distance": spread_result.get("avg_max_distance"),
                        "max_distance_found": spread_result.get("max_distance_found"),
                        "paths_analyzed": spread_result.get("paths_analyzed"),
                        "pblock_recommended": avg_dist > 30,
                    },
                    "fabric_region": {
                        "col_min": col_min,
                        "col_max": col_max,
                        "row_min": row_min,
                        "row_max": row_max,
                        "pblock_ranges": pblock_ranges,
                    },
                },
            )

            # ================================================================
            # Step 8: Unplace the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Unplace the design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("run_tcl", {
                "command": "place_design -unplace"
            }, timeout=300.0)
            print(f"Unplace result:\n{result}")
            logger.info(f"Unplace result: {result}")

            # ================================================================
            # Step 9: Create and apply pblock to entire design (if determined)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Create and apply pblock to entire design")
            print("-"*60)

            if pblock_ranges:
                result = await self.call_vivado_tool("create_and_apply_pblock", {
                    "pblock_name": "pblock_auto",
                    "ranges": pblock_ranges,
                    "apply_to": "current_design",  # Apply to entire design
                    "is_soft": False                # Hard constraint
                }, timeout=300.0)
                print(f"Create and apply pblock result:\n{result}")
                logger.info(f"Create pblock result: {result}")
            else:
                print("Skipping pblock creation (no valid range available)")

            # ================================================================
            # Step 10: Place the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 10: Place the design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("place_design", {
                "directive": "Default"
            }, timeout=3600.0)  # 1 hour timeout for placement
            print(f"Place design result:\n{result}")
            logger.info(f"Place design: {result}")

            # ================================================================
            # Step 11: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 11: Route the design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)  # 1 hour timeout for routing
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            # Check route completion status
            result = await self.call_vivado_tool("report_route_status", {}, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            # ================================================================
            # Step 12: Report final timing and compare WNS
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 12: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")

            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            pblock_info = pblock_ranges if pblock_ranges else "none (fallback: no pblock)"
            self.print_test_summary(
                title="TEST SUMMARY - AUTO PBLOCK OPTIMIZATION",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Pblock applied: {pblock_info}"
            )

            return True

        except Exception as e:
            logger.exception(f"Auto-pblock test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_test_lut_cone(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        LUT input cone optimization flow using RapidWright's LUTInputConeOpt.

        Chains of small LUTs on critical paths can be merged into a single larger
        LUT (up to 6 inputs), reducing logic depth and therefore path delay.
        This flow targets LUT-dense benchmarks: logicnets variants and any design
        where critical paths show many LUT-to-LUT connections.

        Steps:
        1. Open the input DCP in Vivado
        2. Report timing (baseline WNS)
        3. Extract critical path pin-level data to file (Vivado)
        4. Open DCP in RapidWright
        5. Parse LUT input pins from the critical path pin JSON
        6. Run optimize_lut_input_cone on identified pins (RapidWright)
        7. Write optimized DCP from RapidWright
        8. Open optimized DCP in Vivado
        9. Route the design in Vivado
        10. Report final timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - LUT CONE FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # ================================================================
            # Step 0: Initialize RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # ================================================================
            # Step 2: Report timing (baseline WNS)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report baseline timing in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            timing_info = parse_timing_summary_static(result)
            self.initial_tns = timing_info["tns"]
            self.initial_failing_endpoints = timing_info["failing_endpoints"]

            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()

            # ================================================================
            # Step 3: Extract pin-level critical path data to JSON file
            #
            # extract_critical_path_pins returns flat lists of pin references like
            # ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", ...].  Consecutive pins
            # from the same cell form the cell's data path; consecutive pins from
            # different cells identify a connecting net.  We parse LUT input pins
            # (pins whose name contains "I" and whose cell is an LUT) from this
            # data to feed into optimize_lut_input_cone.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Extract critical path pins")
            print("-"*60)

            pins_file = Path(self.temp_dir) / "critical_path_pins.json"
            result = await self.call_vivado_tool("extract_critical_path_pins", {
                "num_paths": 20,
                "output_file": str(pins_file)
            }, timeout=600.0)
            print(f"Extract critical path pins result:\n{result[:2000]}...")
            logger.info(f"Extract critical path pins: {result}")

            # Load the pin data - prefer the file, fall back to inline JSON
            critical_paths = []
            if pins_file.exists():
                critical_paths = json.loads(pins_file.read_text())
            else:
                try:
                    critical_paths = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    pass

            print(f"Loaded {len(critical_paths)} critical path pin lists")

            # ================================================================
            # Step 4: Open DCP in RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Open DCP in RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")

            # ================================================================
            # Step 5: Parse LUT input pins from the critical path pin data
            #
            # optimize_lut_input_cone takes hierarchical input pin names such as
            # "module/submodule/lut_inst/I0".  We extract pins whose suffix looks
            # like a LUT input (matches /I\d+$/) from all paths and deduplicate,
            # then limit to the top 30 to keep the optimization runtime bounded.
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Parse LUT input pins from critical path data")
            print("-"*60)

            lut_input_pins = []
            seen_pins = set()
            lut_input_pattern = re.compile(r'/[IA]\d+$', re.IGNORECASE)

            for path_pins in critical_paths:
                for pin in path_pins:
                    # Select only LUT input pins; skip duplicates
                    if lut_input_pattern.search(pin) and pin not in seen_pins:
                        lut_input_pins.append(pin)
                        seen_pins.add(pin)

            # Keep only the first 30 unique candidates to bound runtime
            lut_input_pins = lut_input_pins[:30]
            print(f"Identified {len(lut_input_pins)} unique LUT input pins to optimize:")
            for pin in lut_input_pins[:10]:
                print(f"  {pin}")
            if len(lut_input_pins) > 10:
                print(f"  ... ({len(lut_input_pins) - 10} more)")

            if not lut_input_pins:
                print("WARNING: No LUT input pins found on critical paths - nothing to optimize")
                logger.warning("No LUT input pins found on critical paths")
                # Still proceed: write through unchanged and measure timing
                lut_input_pins = []

            # Save analysis context (end of analysis phase, before optimization)
            self.save_analysis_context(
                input_dcp,
                "lut_cone",
                extra_sections={
                    "lut_analysis": {
                        "paths_analyzed": len(critical_paths),
                        "lut_input_pins_count": len(lut_input_pins),
                        "sample_pins": lut_input_pins[:10],
                    },
                },
            )

            # ================================================================
            # Step 6: Run optimize_lut_input_cone on identified pins
            #
            # LUTInputConeOpt combines chained small LUTs into a single larger
            # LUT (up to 6 inputs) where the input fan-in permits.  Cells that
            # cannot be merged are reported as "no_optimization".
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Run optimize_lut_input_cone in RapidWright")
            print("-"*60)

            optimized_count = 0
            if lut_input_pins:
                result = await self.call_rapidwright_tool("optimize_lut_input_cone", {
                    "hierarchical_input_pins": lut_input_pins
                }, timeout=300.0)
                print(f"optimize_lut_input_cone result:\n{result[:3000]}...")
                logger.info(f"optimize_lut_input_cone: {result}")

                try:
                    cone_result = json.loads(result)
                    optimized_count = cone_result.get("optimized_count", 0)
                    total_pins = cone_result.get("total_pins", len(lut_input_pins))
                    print(f"\nLUT cones optimized: {optimized_count}/{total_pins}")
                    for r in cone_result.get("results", [])[:10]:
                        print(f"  [{r['status']:16s}] {r['pin']}")
                except (json.JSONDecodeError, TypeError):
                    print("(Could not parse structured result)")
            else:
                print("Skipping optimization (no LUT input pins identified)")

            # ================================================================
            # Step 7: Write optimized DCP from RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Write optimized DCP from RapidWright")
            print("-"*60)

            rw_output = Path(self.temp_dir) / "lut_cone_optimized.dcp"
            rw_before_write = _checkpoint_signature(rw_output)
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rw_output),
                "overwrite": True
            }, timeout=600.0)
            print(f"Write checkpoint result:\n{result}")
            logger.info(f"RapidWright write checkpoint: {result}")

            _require_written_checkpoint(rw_output, rw_before_write, "RapidWright")
            print(f"DCP file created: {rw_output} ({rw_output.stat().st_size} bytes)")

            # ================================================================
            # Step 8: Open optimized DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Open optimized DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(rw_output)
            }, timeout=600.0)
            print(f"Open optimized checkpoint result:\n{result}")
            logger.info(f"Open optimized checkpoint: {result}")

            # ================================================================
            # Step 9: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Route design in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")

            # Check route completion status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            # ================================================================
            # Step 10: Report final timing and compare WNS
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 10: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)

            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)

            print(f"\nWriting final DCP to: {output_dcp}")
            final_before_write = _checkpoint_signature(output_dcp.resolve())
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            _require_written_checkpoint(output_dcp.resolve(), final_before_write, "Vivado")

            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - LUT CONE OPTIMIZATION",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"LUT input pins processed: {len(lut_input_pins)}, "
                           f"cones merged: {optimized_count}"
            )

            return True

        except Exception as e:
            logger.exception(f"LUT-cone test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def analyze_design_for_flow_selection(self, input_dcp: Path) -> Dict:
        """Return auto_top3-style deterministic analysis for constrained selection."""
        dcp_name = input_dcp.name.lower()

        print("\n" + "-"*60)
        print("AUTO-TOP3: Analyze design and rank candidate flows")
        print("-"*60)

        # Collect timing snapshot for WNS/failing-endpoint context.
        await self.call_vivado_tool("open_checkpoint", {
            "dcp_path": str(input_dcp.resolve())
        }, timeout=600.0)
        timing_report = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
        timing = parse_timing_summary_static(timing_report)

        # Keep class state consistent with other flows.
        self.initial_tns = timing.get("tns")
        self.initial_failing_endpoints = timing.get("failing_endpoints")
        self.clock_period = await self.fetch_clock_period()
        target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
        if target_wns is not None:
            self.initial_wns = target_wns
        else:
            self.initial_wns = timing.get("wns")

        utilization_raw = await self.call_vivado_tool("report_utilization_for_pblock", {}, timeout=300.0)
        luts = ffs = dsps = brams = 0

        try:
            util = json.loads(utilization_raw)
            luts = int(util.get("luts", util.get("luts_1_5x", 0)) or 0)
            ffs = int(util.get("ffs", util.get("ffs_1_5x", 0)) or 0)
            dsps = int(util.get("dsps", util.get("dsps_1_5x", 0)) or 0)
            brams = int(util.get("brams", util.get("brams_1_5x", 0)) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            lut_m = re.search(r"LUTs\D+(\d+)", utilization_raw, re.IGNORECASE)
            ff_m = re.search(r"FFs\D+(\d+)", utilization_raw, re.IGNORECASE)
            dsp_m = re.search(r"DSPs\D+(\d+)", utilization_raw, re.IGNORECASE)
            bram_m = re.search(r"BRAMs\D+(\d+)", utilization_raw, re.IGNORECASE)
            luts = int(lut_m.group(1)) if lut_m else 0
            ffs = int(ff_m.group(1)) if ff_m else 0
            dsps = int(dsp_m.group(1)) if dsp_m else 0
            brams = int(bram_m.group(1)) if bram_m else 0

        wns = self.initial_wns if self.initial_wns is not None else timing.get("wns")
        failing_eps = timing.get("failing_endpoints") or 0
        total_hardblocks = dsps + brams

        flow_heuristic_scores: Dict[str, float] = {
            "phys_opt": 3.0,
            "auto_pblock": 2.5,
            "congestion_spread": 2.0,
            "memory_path_opt": 1.8,
            "hardblock_relocate": 1.6,
            "retime": 1.2,
            "net_swap": 1.0,
            "lut_cone": 0.8,
            "vexriscv": 0.6,
            "pblock": 0.5,
            "fanout": 0.5,
        }

        # Benchmark-family priors from docs/benchmarks.md and docs guidance.
        if "logicnets" in dcp_name and "jscl" in dcp_name:
            flow_heuristic_scores["pblock"] += 9.0
            flow_heuristic_scores["lut_cone"] += 4.0
            flow_heuristic_scores["phys_opt"] += 2.0
        elif "logicnets" in dcp_name:
            flow_heuristic_scores["lut_cone"] += 8.0
            flow_heuristic_scores["net_swap"] += 3.0
            flow_heuristic_scores["phys_opt"] += 2.0

        if "vexriscv" in dcp_name:
            flow_heuristic_scores["vexriscv"] += 9.0
            flow_heuristic_scores["net_swap"] += 2.5
            flow_heuristic_scores["phys_opt"] += 2.0

        if any(tag in dcp_name for tag in ["boom", "corescore", "rosetta", "vtr_mcml"]):
            flow_heuristic_scores["auto_pblock"] += 8.0
            flow_heuristic_scores["congestion_spread"] += 4.0
            flow_heuristic_scores["phys_opt"] += 2.0

        if any(tag in dcp_name for tag in ["finn", "ispd", "isp", "mini-isp"]):
            flow_heuristic_scores["memory_path_opt"] += 6.0
            flow_heuristic_scores["hardblock_relocate"] += 4.0
            flow_heuristic_scores["phys_opt"] += 3.0

        # Report-derived nudges.
        if wns is not None and wns <= -5.0:
            flow_heuristic_scores["phys_opt"] += 2.0
            flow_heuristic_scores["auto_pblock"] += 1.0
            flow_heuristic_scores["congestion_spread"] += 1.0
        if failing_eps >= 1000:
            flow_heuristic_scores["congestion_spread"] += 2.0
            flow_heuristic_scores["phys_opt"] += 1.0
        if total_hardblocks >= 100:
            flow_heuristic_scores["memory_path_opt"] += 2.0
            flow_heuristic_scores["hardblock_relocate"] += 2.0
        if ffs > luts and ffs > 0:
            flow_heuristic_scores["retime"] += 1.5

        ranked = sorted(flow_heuristic_scores.items(), key=lambda kv: kv[1], reverse=True)
        top3 = [name for name, _ in ranked[:3]]
        initial_fmax = self.calculate_fmax(wns, self.clock_period)

        print(f"AUTO-TOP3 analysis: WNS={wns}, failing_endpoints={failing_eps}, "
              f"LUTs={luts}, FFs={ffs}, DSPs={dsps}, BRAMs={brams}")
        print("AUTO-TOP3 selected flows (ranked): " + ", ".join(top3))

        return {
            "benchmark_name": input_dcp.stem,
            "target_clock": getattr(self, "target_clock", None),
            "clock_period_ns": self.clock_period,
            "initial_wns_ns": wns,
            "initial_tns_ns": self.initial_tns,
            "failing_endpoints": self.initial_failing_endpoints,
            "initial_fmax_mhz": round(initial_fmax, 3) if initial_fmax is not None else None,
            "utilization_summary": {
                "luts": luts,
                "ffs": ffs,
                "dsps": dsps,
                "brams": brams,
            },
            "flow_scores": [
                {"flow": name, "flow_heuristic_score": round(flow_heuristic_score, 3)}
                for name, flow_heuristic_score in ranked
            ],
            "selected_top3": top3,
        }

    async def select_top3_optimizers_from_report(self, input_dcp: Path) -> List[str]:
        """
        Select the top-3 optimization flows for this design using:
          1) quick report analysis from the current checkpoint, and
          2) benchmark-family priors documented in docs/benchmarks.md.

        Returns a list of flow names ordered by descending heuristic priority.
        """
        analysis = await self.analyze_design_for_flow_selection(input_dcp)
        top3 = analysis.get("selected_top3", [])

        # Save analysis context so the auto_top3 selection rationale is persisted
        self.save_analysis_context(
            input_dcp,
            "auto_top3",
            extra_sections={
                "utilization_summary": analysis.get("utilization_summary"),
                "flow_scores": analysis.get("flow_scores", []),
                "selected_top3": top3,
            },
        )

        return top3

    async def run_test_auto_top3(
        self,
        input_dcp: Path,
        output_dcp: Path,
        max_nets_to_optimize: int = 5,
    ) -> bool:
        """
        Auto-select and execute the best three optimizers for the current design.

        Process:
        1) analyze reports and benchmark family,
        2) rank/select top-3 flows,
        3) run each flow independently from the same input DCP,
        4) keep the checkpoint with best final Fmax/WNS.
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - AUTO TOP-3 FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")

        overall_start = time.time()

        # Flow dispatch table for methods in this class.
        flow_runners = {
            "fanout": lambda inp, out: self.run_test(inp, out, max_nets_to_optimize=max_nets_to_optimize),
            "pblock": self.run_test_logicnets,
            "vexriscv": self.run_test_vexriscv,
            "phys_opt": self.run_test_phys_opt,
            "auto_pblock": self.run_test_auto_pblock,
            "lut_cone": self.run_test_lut_cone,
            "retime": self.run_test_retime,
            "net_swap": self.run_test_net_swap,
            "memory_path_opt": self.run_test_memory_path_opt,
            "hardblock_relocate": self.run_test_hardblock_relocate,
            "congestion_spread": self.run_test_congestion_spread,
        }

        try:
            top3 = await self.select_top3_optimizers_from_report(input_dcp)

            # Prefer higher final Fmax. If unavailable, prefer higher (less-negative) WNS.
            def candidate_key(item: Dict[str, object]) -> Tuple[float, float]:
                fmax = item.get("final_fmax")
                final_wns = item.get("final_wns")
                fmax_score = float(fmax) if fmax is not None else float("-inf")
                wns_score = float(final_wns) if final_wns is not None else float("-inf")
                return (fmax_score, wns_score)

            run_results: List[Dict[str, object]] = []
            best_so_far: Optional[Dict[str, object]] = None
            output_dcp.parent.mkdir(parents=True, exist_ok=True)
            for idx, flow_name in enumerate(top3, start=1):
                runner = flow_runners.get(flow_name)
                if runner is None:
                    logger.warning(f"AUTO-TOP3: flow '{flow_name}' has no runner; skipping")
                    continue

                candidate_out = Path(self.temp_dir) / f"auto_top3_{idx:02d}_{flow_name}.dcp"
                print("\n" + "-"*60)
                print(f"AUTO-TOP3 RUN {idx}/3: {flow_name}")
                print("-"*60)

                # Reset per-flow timing stats before each candidate run.
                self.initial_wns = None
                self.final_wns = None
                self.clock_period = None

                try:
                    success = await runner(input_dcp, candidate_out)
                except Exception as e:
                    logger.exception(f"AUTO-TOP3: flow '{flow_name}' failed: {e}")
                    success = False

                fmax = self.calculate_fmax(self.final_wns, self.clock_period)
                run_results.append({
                    "flow": flow_name,
                    "success": success,
                    "output": candidate_out,
                    "initial_wns": self.initial_wns,
                    "final_wns": self.final_wns,
                    "clock_period": self.clock_period,
                    "final_fmax": fmax,
                })

                if success:
                    current = run_results[-1]
                    if not candidate_out.exists():
                        logger.warning(f"AUTO-TOP3: successful flow '{flow_name}' produced no DCP at {candidate_out}")
                    elif best_so_far is None or candidate_key(current) > candidate_key(best_so_far):
                        best_so_far = current
                        shutil.copy2(candidate_out, output_dcp)
                        print(
                            f"AUTO-TOP3 checkpoint update: flow '{flow_name}' is current best; "
                            f"copied {candidate_out} -> {output_dcp}"
                        )

            successful = [r for r in run_results if r["success"]]
            if not successful:
                print("\nAUTO-TOP3: all candidate flows failed")
                return False

            best = best_so_far if best_so_far is not None else max(successful, key=candidate_key)
            best_out = Path(str(best["output"]))

            if not best_out.exists():
                print(f"\nAUTO-TOP3: best candidate output missing: {best_out}")
                return False

            # Keep final copy for robustness even if no intermediate candidate improved after first pass.
            shutil.copy2(best_out, output_dcp)

            print("\n" + "="*70)
            print("AUTO-TOP3 SUMMARY")
            print("="*70)
            for r in run_results:
                status = "PASS" if r["success"] else "FAIL"
                print(
                    f"- {r['flow']:20s} | {status:4s} | "
                    f"WNS={r['final_wns']} | Fmax={r['final_fmax']}"
                )
            print(f"Best flow: {best['flow']}")
            print(f"Copied best checkpoint: {best_out} -> {output_dcp}")

            # Preserve winning metrics on object state for consistency.
            self.initial_wns = best.get("initial_wns")
            self.final_wns = best.get("final_wns")
            self.clock_period = best.get("clock_period")
            # Store all per-flow results so run_summary can pick them up.
            self._auto_top3_results = run_results

            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - AUTO TOP-3 FLOW",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Top-3 tested: {', '.join(top3)} | Winner: {best['flow']}"
            )
            return True

        except Exception as e:
            logger.exception(f"AUTO-TOP3 flow failed with exception: {e}")
            print("\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def cleanup(self):
        """Clean up resources."""
        print("\n[TEST] Cleaning up...")
        await super().cleanup()
        print(f"[TEST] Run directory preserved at: {self.run_dir}")


async def run_test_mode(
    input_dcp: Path,
    output_dcp: Path,
    debug: bool = False,
    max_nets: int = 5,
    run_dir: Optional[Path] = None,
    flow: Optional[str] = None,
    optimizer_params: Optional[Dict[str, Any]] = None,
):
    """Run one explicitly named behavior-preserved Xilinx flow."""
    optimizer_params = dict(optimizer_params or {})

    # ------------------------------------------------------------------ #
    # Determine which flow to run                                          #
    # ------------------------------------------------------------------ #
    if flow is None:
        raise ValueError("an explicit Xilinx flow is required; filename inference is unsupported")
    valid_flows = set(XILINX_FLOW_NAMES)
    if flow not in valid_flows:
        print(f"\n[TEST] ERROR: Unknown flow '{flow}'. Valid options: {', '.join(sorted(valid_flows))}")
        return 1
    design_type = flow
    print(f"[TEST] Using explicitly requested flow: {design_type}")

    tester = FPGAOptimizerTest(debug=debug, run_dir=run_dir)
    tester._run_start_time = time.time()

    try:
        await tester.start_servers()
        await tester.capture_utilization_for_checkpoint(
            input_dcp,
            "initial_utilization",
            "Initial",
        )

        if design_type == "pblock":
            success = await tester.run_test_logicnets(input_dcp, output_dcp)
        elif design_type == "vexriscv":
            success = await tester.run_test_vexriscv(input_dcp, output_dcp)
        elif design_type == "phys_opt":
            success = await tester.run_test_phys_opt(input_dcp, output_dcp)
        elif design_type == "place_route":
            success = await tester.run_test_place_route(input_dcp, output_dcp)
        elif design_type == "rescue_place_reset_retime":
            success = await tester.run_test_rescue_place_reset_retime(input_dcp, output_dcp)
        elif design_type == "synthesized_recipe":
            recipe_spec = optimizer_params.get("recipe_spec")
            if not isinstance(recipe_spec, dict):
                print("\n[TEST] ERROR: synthesized_recipe requires optimizer_params.recipe_spec")
                success = False
            else:
                success = await tester.run_test_synthesized_recipe(input_dcp, output_dcp, recipe_spec)
        elif design_type == "manual_action":
            manual_action_spec = optimizer_params.get("manual_action_spec")
            if not isinstance(manual_action_spec, dict):
                print("\n[TEST] ERROR: manual_action requires optimizer_params.manual_action_spec")
                success = False
            else:
                success = await tester.run_test_manual_action(input_dcp, output_dcp, manual_action_spec)
        elif design_type == "auto_pblock":
            success = await tester.run_test_auto_pblock(input_dcp, output_dcp)
        elif design_type == "lut_cone":
            success = await tester.run_test_lut_cone(input_dcp, output_dcp)
        elif design_type == "retime":
            success = await tester.run_test_retime(input_dcp, output_dcp)
        elif design_type == "net_swap":
            success = await tester.run_test_net_swap(input_dcp, output_dcp)
        elif design_type == "memory_path_opt":
            success = await tester.run_test_memory_path_opt(input_dcp, output_dcp)
        elif design_type == "hardblock_relocate":
            success = await tester.run_test_hardblock_relocate(input_dcp, output_dcp)
        elif design_type == "congestion_spread":
            success = await tester.run_test_congestion_spread(input_dcp, output_dcp)
        elif design_type == "memory_path_opt_rw":
            success = await tester.run_test_memory_path_opt_rw(input_dcp, output_dcp)
        elif design_type == "hardblock_relocate_rw":
            success = await tester.run_test_hardblock_relocate_rw(input_dcp, output_dcp)
        elif design_type == "congestion_spread_rw":
            success = await tester.run_test_congestion_spread_rw(input_dcp, output_dcp)
        elif design_type == "auto_top3":
            success = await tester.run_test_auto_top3(input_dcp, output_dcp, max_nets_to_optimize=max_nets)
        else:  # "fanout" - original general fanout-split flow
            success = await tester.run_test(input_dcp, output_dcp, max_nets_to_optimize=max_nets)

        tester.end_time = time.time()

        if success and output_dcp.exists():
            await tester.capture_utilization_for_checkpoint(
                output_dcp,
                "final_utilization",
                "Final",
            )

        # Save run summary JSON
        try:
            auto_top3_results = getattr(tester, "_auto_top3_results", None)
            summary_extra = None
            if design_type == "synthesized_recipe":
                summary_extra = {
                    "recipe_spec": getattr(tester, "_synthesized_recipe_spec", optimizer_params.get("recipe_spec")),
                    "recipe_step_results": getattr(tester, "_synthesized_recipe_step_results", []),
                }
            elif design_type == "manual_action":
                summary_extra = {
                    "manual_action": getattr(tester, "_manual_action_result", {}),
                }
            elif design_type == "place_route":
                summary_extra = {
                    "wp013_call_accounting": getattr(
                        tester, "_wp013_call_accounting", {}
                    ),
                }
            tester.save_run_summary(
                input_dcp=input_dcp,
                output_dcp=output_dcp,
                mode="test",
                flow=design_type,
                optimizers_tried=auto_top3_results,
                extra=summary_extra,
            )
        except Exception as _e:
            logger.warning(f"Could not save run summary: {_e}")

        if success:
            print("\n[TEST] Test completed successfully")
            print(f"\n[TEST] Output files:")
            print(f"[TEST]   Optimized DCP: {output_dcp}")
            print(f"[TEST]   Run directory: {tester.run_dir}")
            return 0
        else:
            print("\n[TEST] Test failed")
            print(f"[TEST] Run directory: {tester.run_dir}")
            return 1

    except KeyboardInterrupt:
        print("\n[TEST] Interrupted by user")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 130
    except Exception as e:
        logger.exception(f"Test mode fatal error: {e}")
        print(f"\n[TEST] Fatal error: {e}")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 1
    finally:
        await tester.cleanup()


def _recipe_spec_from_dict(data: Dict[str, Any]) -> RecipeSpec:
    if not isinstance(data, dict):
        raise ValueError("recipe_spec must be a JSON object")
    steps = []
    for idx, item in enumerate(data.get("steps") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"recipe_spec step {idx} is not an object")
        from optimizer_pipeline.capabilities.intervention.recipe import RecipeStep

        steps.append(
            RecipeStep(
                step_id=str(item.get("step_id") or f"step_{idx:02d}"),
                primitive=str(item.get("primitive") or ""),
                args=dict(item.get("args") or {}) if isinstance(item.get("args"), dict) else {},
                rationale=str(item.get("rationale") or ""),
            )
        )
    if not steps:
        raise ValueError("recipe_spec must contain at least one step")
    return RecipeSpec(
        recipe_id=str(data.get("recipe_id") or "synthesized_recipe_v1"),
        summary=str(data.get("summary") or "Synthesized recipe"),
        steps=tuple(steps),
        source=str(data.get("source") or "runtime_recipe_spec"),
        risk_notes=tuple(str(x) for x in data.get("risk_notes", []) if isinstance(x, (str, int, float))),
    )


def _route_status_clean(status: str) -> bool:
    text = status or ""
    lower = text.lower()
    if "routing errors" in lower:
        match = re.search(r"#\s*of\s*nets\s*with\s*routing\s*errors[^:]*:\s*(\d+)", text, flags=re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            return False
    if re.search(r"#\s*of\s*unrouted\s*nets[^:]*:\s*[1-9]\d*", text, flags=re.IGNORECASE):
        return False
    return "fully routed" in lower or "routing errors" in lower
