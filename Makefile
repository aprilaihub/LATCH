# Makefile for the FPGA/EDA Experiment Platform

# Configuration
PYTHON ?= python3
PIP := $(PYTHON) -m pip

# Vivado executable - can be overridden with: make setup VIVADO_EXEC=<VIVADO_INSTALL>/bin/vivado
# The official validation AMI installs Vivado 2025.1 under /tools/Xilinx but
# does not necessarily put it on PATH for non-interactive make invocations.
VIVADO_EXEC ?= $(shell if command -v vivado >/dev/null 2>&1; then command -v vivado; elif [ -x /tools/Xilinx/2025.1/Vivado/bin/vivado ]; then echo /tools/Xilinx/2025.1/Vivado/bin/vivado; else echo vivado; fi)
export VIVADO_EXEC

# Set JAVA_HOME from PATH or Vivado if not already set
# Python RapidWright may need JAVA_HOME to be set, but often users only have `java` on PATH
# See: https://www.rapidwright.io/docs/Install.html#using-java-distributed-with-vivado
ifndef JAVA_HOME
  JAVA_PATH := $(shell command -v java 2>/dev/null)
  ifneq ($(JAVA_PATH),)
    # Found java on PATH - resolve symlinks and derive JAVA_HOME
    # Try readlink -f (Linux), fall back to direct path (macOS/others)
    REAL_JAVA_PATH := $(shell readlink -f "$(JAVA_PATH)" 2>/dev/null || readlink "$(JAVA_PATH)" 2>/dev/null || echo "$(JAVA_PATH)")
    # java is at $JAVA_HOME/bin/java, so go up two directories
    export JAVA_HOME := $(shell dirname $(shell dirname $(REAL_JAVA_PATH)))
  else
    # java not on PATH - try to find Java bundled with Vivado
    # Vivado includes Java at: <VIVADO_ROOT>/tps/lnx64/jre11*/bin/java
    VIVADO_PATH := $(shell command -v $(VIVADO_EXEC) 2>/dev/null)
    ifneq ($(VIVADO_PATH),)
      VIVADO_ROOT := $(shell dirname $(shell dirname $(VIVADO_PATH)))
      VIVADO_JAVA := $(shell ls $(VIVADO_ROOT)/tps/lnx64/jre11*/bin/java 2>/dev/null | head -n 1)
      ifneq ($(VIVADO_JAVA),)
        export JAVA_HOME := $(shell dirname $(shell dirname $(VIVADO_JAVA)))
        export PATH := $(JAVA_HOME)/bin:$(PATH)
      endif
    endif
  endif
endif

# RapidWright submodule path and classpath
# Points the Python rapidwright package to use the local RapidWright source
# See: https://www.rapidwright.io/docs/Install_RapidWright_as_a_Python_PIP_Package.html#java-development-and-python
RAPIDWRIGHT_PATH ?= $(CURDIR)/RapidWright
export RAPIDWRIGHT_PATH
RAPIDWRIGHT_JAR ?= $(RAPIDWRIGHT_PATH)/jars/rapidwright-2025.2.1-standalone-lin64.jar
export RAPIDWRIGHT_JAR
export CLASSPATH := $(RAPIDWRIGHT_PATH)/bin:$(RAPIDWRIGHT_PATH)/jars/*

# On hosts where Gradle cannot run (e.g. restricted IPv6/network environments),
# allow skipping source build and directly use the prebuilt standalone jar in
# RapidWright/jars/.
RAPIDWRIGHT_SKIP_BUILD ?= 0

# Benchmark archive from GitHub release
BENCHMARK_VERSION := v1.1.0
BENCHMARK_TARBALL := fpl26_contest_benchmarks_$(BENCHMARK_VERSION).tar.gz
BENCHMARK_DIR := fpl26_contest_benchmarks
BENCHMARK_URL := https://github.com/Xilinx/fpl26_optimization_contest/releases/download/$(BENCHMARK_VERSION)/$(BENCHMARK_TARBALL)
# Version marker written into the extracted benchmark directory so `make setup`
# can detect when an older archive was extracted previously and needs to be
# refreshed. Without this, `make setup` would silently keep a stale
# fpl26_contest_benchmarks/ from a previous BENCHMARK_VERSION.
BENCHMARK_VERSION_FILE := $(BENCHMARK_DIR)/.benchmark_version

# Example DCP paths (inside the extracted benchmark directory)
EXAMPLE_DCP_1 := $(BENCHMARK_DIR)/logicnets_jscl_2025.1.dcp
EXAMPLE_DCP_2 := $(BENCHMARK_DIR)/vexriscv_re-place_2025.1.dcp

# Contest application overrides. The parser in
# optimizer_pipeline/apps/fpl_contest/args.py is the sole code-level authority
# for defaults; unset Make variables deliberately do not override it.
RUN_MODE ?=
PIPELINE ?=
POLICY ?=
ANALYSIS ?=
LLM_PROVIDER ?=
BUDGET_RUNS ?=
SINGLE_PATH_DEPTH ?=
PARALLELISM ?=
RUN_DIR ?=
PLAN ?=
CUSTOM_PIPELINE ?=
MODEL ?=
API_KEY ?=
LLM_COST_BUDGET_USD ?=
KNOWLEDGE_MODE ?=
RESCUE_MODE ?=
RESCUE_TRIGGER ?=
RESCUE_MAX_RUNS ?=
RESCUE_MIN_MAIN_DELTA_MHZ ?=
RESCUE_SYNTHESIS_MAX_STEPS ?=
RESCUE_SYNTHESIS_FALLBACK ?=
MOCK ?= 0

# Colors for output
COLOR_GREEN := \033[0;32m
COLOR_YELLOW := \033[0;33m
COLOR_RED := \033[0;31m
COLOR_BLUE := \033[0;34m
COLOR_RESET := \033[0m

.PHONY: setup build-rapidwright run_optimizer run_optimizer_all run_action run_experiment validate validate_demo run-submission clean veryclean help

# Default target
help:
	@echo "FPGA/EDA Experiment Platform - Makefile"
	@echo ""
	@echo "Available targets:"
	@echo "  setup              - Install dependencies, build RapidWright, download example DCPs"
	@echo "  build-rapidwright  - Build RapidWright from source (git submodule)"
	@echo "  run_optimizer      - Run the contest-compatible profile shell on a DCP file"
	@echo "  run_optimizer_all  - Run the profile shell on every benchmark DCP (skips *_optimized-* files)"
	@echo "  run_action         - Run one explicitly named backend action"
	@echo "  run_experiment     - Run a custom managed experiment pipeline"
	@echo "  validate           - Validate functional equivalence between two DCPs"
	@echo "  validate_demo      - Run validation demo (self-check)"
	@echo "  clean              - Remove generated files (run directories, logs, Vivado outputs)"
	@echo "  veryclean          - Remove all generated files including example DCPs"
	@echo ""
	@echo "Usage examples:"
	@echo "  make setup"
	@echo "  make setup VIVADO_EXEC=<VIVADO_INSTALL>/bin/vivado"
	@echo "  make run_optimizer DCP=fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp"
	@echo "  make run_optimizer DCP=input.dcp PIPELINE=linear SINGLE_PATH_DEPTH=3 BUDGET_RUNS=3"
	@echo "  make run_optimizer DCP=input.dcp RUN_MODE=contest-strict PIPELINE=analysis_frontier POLICY=analysis_priority ANALYSIS=fast_llm LLM_PROVIDER=openrouter MODEL=openai/gpt-5.4-mini"
	@echo "  make run_optimizer DCP=input.dcp RUN_MODE=research-dev PIPELINE=analysis_single_path_frontier ANALYSIS=deep_llm LLM_PROVIDER=openai"
	@echo "  make run_optimizer_all"
	@echo "  make run_action DCP=fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp FLOW=phys_opt"
	@echo "  make run_experiment DCP=input.dcp CUSTOM_PIPELINE=my_package.my_module:run"
	@echo "  make validate GOLDEN=design.dcp REVISED=design_optimized.dcp"
	@echo "  make validate GOLDEN=design.dcp REVISED=design_optimized.dcp VECTORS=50000"
	@echo "  make validate_demo"
	@echo "  make clean"
	@echo ""
	@echo "Environment variables:"
	@echo "  VIVADO_EXEC     - Path to Vivado executable (default: vivado)"
	@echo "  JAVA_HOME       - Java installation directory (auto-detected from PATH if not set)"
	@echo "  DCP             - Input DCP file for optimizer, action, and experiment targets"
	@echo "  MAX_NETS        - Max high fanout nets for a direct action (default: 5)"
	@echo "  FLOW            - Required explicit action name for run_action"
	@echo "                    Valid: fanout, pblock, vexriscv, phys_opt, auto_pblock, lut_cone, retime, net_swap, memory_path_opt, hardblock_relocate, congestion_spread, memory_path_opt_rw, hardblock_relocate_rw, congestion_spread_rw, auto_top3, rescue_place_reset_retime, synthesized_recipe"
	@echo "  RUN_MODE        - contest-strict or research-dev (default: contest-strict)"
	@echo "  PIPELINE        - contest_default, fixed, linear, frontier, analysis_single_path, analysis_frontier, analysis_single_path_frontier, custom"
	@echo "  POLICY          - analysis_priority, deterministic, reliability, retrieval (CLI default: deterministic)"
	@echo "  ANALYSIS        - none, static, fast_llm, deep_llm (default: none)"
	@echo "  LLM_PROVIDER    - none, openrouter, openai, local (default: none)"
	@echo "  BUDGET_RUNS     - Maximum transition executions for run_optimizer (CLI default: 6)"
	@echo "  SINGLE_PATH_DEPTH - Depth for PIPELINE=linear single-path frontier preset (default: 3)"
	@echo "  PARALLELISM     - Concurrent transition executions for frontier phases (default: 1)"
	@echo "  RUN_DIR         - Optional run artifact directory"
	@echo "  PLAN            - Comma-separated fixed flow sequence for PIPELINE=fixed"
	@echo "  CUSTOM_PIPELINE - module:function for PIPELINE=custom"
	@echo "  MOCK            - Set to 1 to use mock analysis/execution without Vivado"
	@echo "  LLM_COST_BUDGET_USD - Optional per-benchmark LLM budget for OpenRouter opt-in runs (contest default: 0.80)"
	@echo "  KNOWLEDGE_MODE  - off or reference_manual for optional LLM analysis (default: off)"
	@echo "  RESCUE_MODE     - off, deterministic, llm, synthesis, synthesis_loop, or manual_loop (CLI default: off)"
	@echo "  RESCUE_TRIGGER  - no_improvement or always (default: no_improvement)"
	@echo "  RESCUE_MAX_RUNS - Maximum rescue transitions after the main profile (default: 1)"
	@echo "  RESCUE_MIN_MAIN_DELTA_MHZ - Main-profile Fmax delta threshold for no-improvement rescue (default: 0.1)"
	@echo "  RESCUE_SYNTHESIS_MAX_STEPS - Max primitive steps in synthesized recipe (default: 8)"
	@echo "  RESCUE_SYNTHESIS_FALLBACK - deterministic or fail when synthesis is invalid (default: deterministic)"
	@echo "  GOLDEN          - Golden (reference) DCP for validation"
	@echo "  REVISED         - Revised (optimized) DCP for validation"
	@echo "  VECTORS         - Number of test vectors for validation (default: 200)"
	@echo ""
	@echo "Output structure:"
	@echo "  - Optimized DCP: <input_name>_optimized-<timestamp>.dcp (next to input)"
	@echo "  - Run directory: dcp_optimizer_run-<timestamp>/ (contains all logs)"
	@echo "  - Pipeline:      dcp_optimizer_run-<timestamp>/final_output.dcp"
	@echo "  - Validation:    dcp_validation_*/ (contains simulation logs)"

# Setup target: Install dependencies, check Vivado, set up Java, build RapidWright, download DCPs
setup:
	@printf "$(COLOR_GREEN)===== FPGA Design Optimization Setup =====$(COLOR_RESET)\n"
	@echo ""
	
	@printf "$(COLOR_YELLOW)[1/5] Installing Python dependencies...$(COLOR_RESET)\n"
	$(PIP) install -e '.[tools]'
	@printf "$(COLOR_GREEN)✓ Python dependencies installed$(COLOR_RESET)\n"
	@echo ""
	
	@printf "$(COLOR_YELLOW)[2/5] Checking Vivado...$(COLOR_RESET)\n"
	@if command -v $(VIVADO_EXEC) >/dev/null 2>&1; then \
		printf "$(COLOR_GREEN)✓ Vivado found: %s$(COLOR_RESET)\n" "$$(command -v $(VIVADO_EXEC))"; \
		$(VIVADO_EXEC) -version | head -n 1; \
	else \
		printf "$(COLOR_RED)✗ Vivado not found on PATH$(COLOR_RESET)\n"; \
		echo ""; \
		echo "Please either:"; \
		echo "  1. Source Vivado settings: source <VIVADO_INSTALL>/settings64.sh"; \
		echo "  2. Specify Vivado path: make setup VIVADO_EXEC=<VIVADO_INSTALL>/bin/vivado"; \
		exit 1; \
	fi
	@echo ""
	
	@printf "$(COLOR_YELLOW)[3/5] Checking Java...$(COLOR_RESET)\n"
	@if command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_GREEN)✓ Java found: %s$(COLOR_RESET)\n" "$$(command -v java)"; \
		java -version 2>&1 | head -n 1; \
	else \
		printf "$(COLOR_YELLOW)⚠ Java not found on PATH$(COLOR_RESET)\n"; \
		echo "Attempting to locate Java from Vivado installation..."; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC)); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				printf "$(COLOR_GREEN)✓ Found Java in Vivado: %s$(COLOR_RESET)\n" "$$JAVA_FOUND"; \
				echo ""; \
				printf "$(COLOR_YELLOW)NOTE: Set JAVA_HOME before running optimizer:$(COLOR_RESET)\n"; \
				JAVA_HOME_DIR=$$(dirname $$(dirname $$JAVA_FOUND)); \
				echo "  export JAVA_HOME=$$JAVA_HOME_DIR"; \
				echo "  export PATH=\$$JAVA_HOME/bin:\$$PATH"; \
			else \
				printf "$(COLOR_RED)✗ Could not find Java in Vivado installation$(COLOR_RESET)\n"; \
				echo "Please install Java 11 or later"; \
				exit 1; \
			fi; \
		else \
			printf "$(COLOR_RED)✗ Cannot locate Java$(COLOR_RESET)\n"; \
			echo "Please install Java 11 or later"; \
			exit 1; \
		fi; \
	fi
	@echo ""
	
	@printf "$(COLOR_YELLOW)[4/5] Setting up RapidWright...( $(RAPIDWRIGHT_SKIP_BUILD) )$(COLOR_RESET)\n"
	@if [ "$(RAPIDWRIGHT_SKIP_BUILD)" = "1" ]; then \
		if [ -f "$(RAPIDWRIGHT_JAR)" ]; then \
			printf "$(COLOR_GREEN)✓ Skipping source build. Using jar: $(RAPIDWRIGHT_JAR)$(COLOR_RESET)\n"; \
		else \
			printf "$(COLOR_RED)✗ RAPIDWRIGHT_SKIP_BUILD=1 but $(RAPIDWRIGHT_JAR) not found$(COLOR_RESET)\n"; \
			echo "Please download the standalone RapidWright jar and place it at:"; \
			echo "  $(RAPIDWRIGHT_JAR)"; \
			exit 1; \
		fi; \
	else \
		$(MAKE) build-rapidwright; \
	fi
	@echo ""
	
	@printf "$(COLOR_YELLOW)[5/5] Downloading and extracting benchmark DCPs...$(COLOR_RESET)\n"
	@if [ -d "$(BENCHMARK_DIR)" ] && [ -f "$(EXAMPLE_DCP_1)" ] && \
	    [ -f "$(BENCHMARK_VERSION_FILE)" ] && \
	    [ "$$(cat $(BENCHMARK_VERSION_FILE))" = "$(BENCHMARK_VERSION)" ]; then \
		printf "$(COLOR_GREEN)✓ $(BENCHMARK_DIR)/ already exists for $(BENCHMARK_VERSION)$(COLOR_RESET)\n"; \
	else \
		if [ -d "$(BENCHMARK_DIR)" ]; then \
			if [ -f "$(BENCHMARK_VERSION_FILE)" ]; then \
				EXISTING_VERSION=$$(cat "$(BENCHMARK_VERSION_FILE)"); \
				printf "$(COLOR_YELLOW)Existing $(BENCHMARK_DIR)/ is version %s; refreshing to $(BENCHMARK_VERSION)...$(COLOR_RESET)\n" "$$EXISTING_VERSION"; \
			else \
				printf "$(COLOR_YELLOW)Existing $(BENCHMARK_DIR)/ has no version marker; refreshing to $(BENCHMARK_VERSION)...$(COLOR_RESET)\n"; \
			fi; \
			rm -rf "$(BENCHMARK_DIR)"; \
		fi; \
		if [ ! -f "$(BENCHMARK_TARBALL)" ]; then \
			printf "Downloading $(BENCHMARK_TARBALL)...\n"; \
			if command -v wget >/dev/null 2>&1; then \
				wget -q --show-progress $(BENCHMARK_URL); \
			elif command -v curl >/dev/null 2>&1; then \
				curl -# -L -O $(BENCHMARK_URL); \
			else \
				printf "$(COLOR_RED)✗ Neither wget nor curl found$(COLOR_RESET)\n"; \
				echo "Please install wget or curl, or manually download:"; \
				echo "  $(BENCHMARK_URL)"; \
				exit 1; \
			fi; \
		fi; \
		printf "Extracting $(BENCHMARK_TARBALL)...\n"; \
		tar xzf $(BENCHMARK_TARBALL); \
		echo "$(BENCHMARK_VERSION)" > "$(BENCHMARK_VERSION_FILE)"; \
		printf "$(COLOR_GREEN)✓ Benchmarks extracted to $(BENCHMARK_DIR)/ ($(BENCHMARK_VERSION))$(COLOR_RESET)\n"; \
	fi
	@echo ""
	
	@printf "$(COLOR_GREEN)===== Setup Complete! =====$(COLOR_RESET)\n"
	@echo ""
	@echo "Next steps - run the optimizer:"
	@echo ""
	@echo "  Explicit direct action (no API key required):"
	@echo "    make run_action DCP=$(EXAMPLE_DCP_1) FLOW=phys_opt"
	@echo "    make run_action DCP=$(EXAMPLE_DCP_2) FLOW=vexriscv"
	@echo ""
	@echo "  Contest-compatible optimizer profile shell:"
	@echo "    make run_optimizer DCP=$(EXAMPLE_DCP_1)"
	@echo ""
	@echo "Output will be in:"
	@echo "  - Optimized DCP: <input_name>_optimized-<timestamp>.dcp"
	@echo "  - Run logs: dcp_optimizer_run-<timestamp>/"
	@echo ""

# Build RapidWright from source (git submodule)
build-rapidwright:
	@printf "$(COLOR_YELLOW)Building RapidWright from source...$(COLOR_RESET)\n"
	@if [ ! -f "$(RAPIDWRIGHT_PATH)/gradlew" ]; then \
		printf "$(COLOR_YELLOW)Initializing RapidWright git submodule...$(COLOR_RESET)\n"; \
		git submodule update --init RapidWright; \
	fi
	@cd "$(RAPIDWRIGHT_PATH)" && ./gradlew compileJava -p "$(RAPIDWRIGHT_PATH)"
	@printf "$(COLOR_GREEN)✓ RapidWright built successfully$(COLOR_RESET)\n"
	@printf "$(COLOR_GREEN)  RAPIDWRIGHT_PATH=$(RAPIDWRIGHT_PATH)$(COLOR_RESET)\n"
	@printf "$(COLOR_GREEN)  CLASSPATH=$(CLASSPATH)$(COLOR_RESET)\n"

# Run optimizer target: contest-compatible expansion entry.
run_optimizer: 
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_optimizer DCP=input.dcp"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running contest-compatible optimizer on $(DCP)...$(COLOR_RESET)\n"
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	$(PYTHON) dcp_optimizer.py "$(DCP)" \
	  $(if $(RUN_MODE),--run-mode "$(RUN_MODE)",) \
	  $(if $(PIPELINE),--pipeline "$(PIPELINE)",) \
	  $(if $(POLICY),--policy "$(POLICY)",) \
	  $(if $(ANALYSIS),--analysis "$(ANALYSIS)",) \
	  $(if $(LLM_PROVIDER),--llm-provider "$(LLM_PROVIDER)",) \
	  $(if $(BUDGET_RUNS),--budget-runs "$(BUDGET_RUNS)",) \
	  $(if $(SINGLE_PATH_DEPTH),--single-path-depth "$(SINGLE_PATH_DEPTH)",) \
	  $(if $(PARALLELISM),--parallelism "$(PARALLELISM)",) \
	  $(if $(RESCUE_MODE),--rescue-mode "$(RESCUE_MODE)",) \
	  $(if $(RESCUE_TRIGGER),--rescue-trigger "$(RESCUE_TRIGGER)",) \
	  $(if $(RESCUE_MAX_RUNS),--rescue-max-runs "$(RESCUE_MAX_RUNS)",) \
	  $(if $(RESCUE_MIN_MAIN_DELTA_MHZ),--rescue-min-main-delta-mhz "$(RESCUE_MIN_MAIN_DELTA_MHZ)",) \
	  $(if $(RESCUE_SYNTHESIS_MAX_STEPS),--rescue-synthesis-max-steps "$(RESCUE_SYNTHESIS_MAX_STEPS)",) \
	  $(if $(RESCUE_SYNTHESIS_FALLBACK),--rescue-synthesis-fallback "$(RESCUE_SYNTHESIS_FALLBACK)",) \
	  $(if $(RUN_DIR),--run-dir "$(RUN_DIR)",) \
	  $(if $(PLAN),--plan "$(PLAN)",) \
	  $(if $(CUSTOM_PIPELINE),--custom-pipeline "$(CUSTOM_PIPELINE)",) \
	  $(if $(MODEL),--model "$(MODEL)",) \
	  $(if $(API_KEY),--api-key "$(API_KEY)",) \
	  $(if $(LLM_COST_BUDGET_USD),--llm-cost-budget-usd "$(LLM_COST_BUDGET_USD)",) \
	  $(if $(KNOWLEDGE_MODE),--knowledge-mode "$(KNOWLEDGE_MODE)",) \
	  $(if $(MAX_NETS),--max-nets "$(MAX_NETS)",) \
	  $(if $(filter 1 true yes,$(MOCK)),--mock,)

# Run profile shell on all benchmark DCPs inside BENCHMARK_DIR, skipping any *_optimized-* outputs.
# Runs sequentially. A single failed benchmark does not stop the remaining ones.
run_optimizer_all:
	@printf "$(COLOR_GREEN)Running contest-compatible optimizer profiles on all benchmarks in $(BENCHMARK_DIR)/...$(COLOR_RESET)\n"
	@echo ""
	@found=0; failed=0; passed=0; \
	for dcp in $(BENCHMARK_DIR)/*.dcp; do \
		case "$$dcp" in *_optimized-*.dcp) continue ;; esac; \
		[ -f "$$dcp" ] || continue; \
		found=$$((found+1)); \
		printf "$(COLOR_YELLOW)--- [%d] $$dcp ---$(COLOR_RESET)\n" "$$found"; \
		if $(MAKE) run_optimizer DCP="$$dcp" $(if $(MAX_NETS),MAX_NETS=$(MAX_NETS)); then \
			passed=$$((passed+1)); \
			printf "$(COLOR_GREEN)✓ $$dcp completed$(COLOR_RESET)\n\n"; \
		else \
			failed=$$((failed+1)); \
			printf "$(COLOR_RED)✗ $$dcp failed (continuing)$(COLOR_RESET)\n\n"; \
		fi; \
	done; \
	printf "$(COLOR_GREEN)===== run_optimizer_all complete: %d passed, %d failed (of %d) =====$(COLOR_RESET)\n" "$$passed" "$$failed" "$$found"

# Run exactly one explicitly named backend action (no filename inference).
run_action:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_action DCP=input.dcp FLOW=phys_opt"; \
		exit 1; \
	fi
	@if [ -z "$(FLOW)" ]; then \
		printf "$(COLOR_RED)Error: FLOW variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_action DCP=input.dcp FLOW=phys_opt"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running explicit action $(FLOW) on $(DCP)...$(COLOR_RESET)\n"
	$(PYTHON) -m optimizer_pipeline.apps.direct_action "$(DCP)" --action "$(FLOW)" $(if $(RUN_DIR),--run-dir "$(RUN_DIR)",) $(if $(MAX_NETS),--max-nets "$(MAX_NETS)",)

run_experiment:
	@if [ -z "$(DCP)" ] || [ -z "$(CUSTOM_PIPELINE)" ]; then \
		printf "$(COLOR_RED)Error: DCP and CUSTOM_PIPELINE are required$(COLOR_RESET)\n"; \
		echo "Usage: make run_experiment DCP=input.dcp CUSTOM_PIPELINE=package.module:run"; \
		exit 1; \
	fi
	$(PYTHON) -m optimizer_pipeline.apps.experiment "$(DCP)" --custom-pipeline "$(CUSTOM_PIPELINE)" $(if $(RUN_DIR),--run-dir "$(RUN_DIR)",) $(if $(filter 1 true yes,$(MOCK)),--mock,)

# Validation target: Validate functional equivalence between two DCPs
validate:
	@printf "$(COLOR_BLUE)╔══════════════════════════════════════════════════════════════════╗$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)║         DCP Equivalence Validation (2-Phase Approach)            ║$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)╚══════════════════════════════════════════════════════════════════╝$(COLOR_RESET)\n"
	@echo ""
	@# Check if GOLDEN and REVISED are provided
	@if [ -z "$(GOLDEN)" ]; then \
		printf "$(COLOR_RED)✗ Error: GOLDEN DCP not specified$(COLOR_RESET)\n"; \
		echo "Usage: make validate GOLDEN=<golden.dcp> REVISED=<revised.dcp> [VECTORS=200] [PRECHECK_VECTORS=100]"; \
		echo ""; \
		echo "Example:"; \
		echo "  make validate GOLDEN=logicnets_jscl.dcp REVISED=logicnets_jscl_optimized.dcp PRECHECK_VECTORS=50"; \
		exit 1; \
	fi
	@if [ -z "$(REVISED)" ]; then \
		printf "$(COLOR_RED)✗ Error: REVISED DCP not specified$(COLOR_RESET)\n"; \
		echo "Usage: make validate GOLDEN=<golden.dcp> REVISED=<revised.dcp> [VECTORS=200] [PRECHECK_VECTORS=100]"; \
		echo ""; \
		echo "Example:"; \
		echo "  make validate GOLDEN=logicnets_jscl.dcp REVISED=logicnets_jscl_optimized.dcp PRECHECK_VECTORS=50"; \
		exit 1; \
	fi
	@# Check if files exist
	@if [ ! -f "$(GOLDEN)" ]; then \
		printf "$(COLOR_RED)✗ Error: Golden DCP not found: $(GOLDEN)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@if [ ! -f "$(REVISED)" ]; then \
		printf "$(COLOR_RED)✗ Error: Revised DCP not found: $(REVISED)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@# Run validation
	@printf "$(COLOR_GREEN)Golden DCP:$(COLOR_RESET)  $(GOLDEN)\n"
	@printf "$(COLOR_GREEN)Revised DCP:$(COLOR_RESET) $(REVISED)\n"
	@printf "$(COLOR_GREEN)Test Vectors:$(COLOR_RESET) $(or $(VECTORS),200)\n"
	@printf "$(COLOR_GREEN)Precheck Vectors:$(COLOR_RESET) $(or $(PRECHECK_VECTORS),100)\n"
	@echo ""
	@$(PYTHON) validate_dcps.py "$(GOLDEN)" "$(REVISED)" --vectors "$(or $(VECTORS),200)" --precheck-vectors "$(or $(PRECHECK_VECTORS),100)"

# Quick validation example using demo DCPs
validate_demo:
	@printf "$(COLOR_BLUE)╔══════════════════════════════════════════════════════════════════╗$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)║                  Validation Demo (Self-check)                    ║$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)╚══════════════════════════════════════════════════════════════════╝$(COLOR_RESET)\n"
	@echo ""
	@echo "This self-check runs validate_dcps.py on a DCP against itself (should always PASS)."
	@echo "It may launch Vivado/xsim; prefer MOCK=1 or pre-generated artifacts for live demos."
	@echo "For real validation, first optimize a design, then validate:"
	@echo ""
	@echo "  1. python dcp_optimizer.py design.dcp --output design_optimized.dcp"
	@echo "  2. make validate GOLDEN=design.dcp REVISED=design_optimized.dcp"
	@echo ""
	@# Check if example DCP exists
	@if [ ! -f "$(EXAMPLE_DCP_2)" ]; then \
		printf "$(COLOR_RED)✗ Example DCP not found: $(EXAMPLE_DCP_2)$(COLOR_RESET)\n"; \
		echo "Run 'make setup' to download and extract benchmark $(BENCHMARK_VERSION), then retry make validate_demo."; \
		exit 1; \
	fi
	@# For demo, validate DCP against itself (should always pass)
	@printf "$(COLOR_GREEN)Running demo validation (self-check)...$(COLOR_RESET)\n"
	@echo ""
	$(PYTHON) validate_dcps.py "$(EXAMPLE_DCP_2)" "$(EXAMPLE_DCP_2)" --vectors 1000

run-submission:
	@echo "Running submission...[Will be implemented later]"
	
# Clean target: Remove run directories and Vivado-generated .Xil directories
clean:
	@printf "$(COLOR_YELLOW)Cleaning generated files...$(COLOR_RESET)\n"
	@# Remove run directories (contain all logs, journals, intermediate files)
	@if ls dcp_optimizer_run-* >/dev/null 2>&1; then \
		rm -rf dcp_optimizer_run-*; \
		echo "Removed dcp_optimizer_run-* directories"; \
	fi
	@# Remove .Xil directories (Vivado generates these outside run directories)
	@if [ -d ".Xil" ]; then \
		rm -rf .Xil; \
		echo "Removed .Xil/"; \
	fi
	@if [ -d "VivadoMCP/.Xil" ]; then \
		rm -rf VivadoMCP/.Xil; \
		echo "Removed VivadoMCP/.Xil/"; \
	fi
	@printf "$(COLOR_GREEN)✓ Clean complete$(COLOR_RESET)\n"
	@echo "Note: Optimized DCP files were preserved"

# Very clean target: Clean + remove __pycache__ and example DCPs
veryclean: clean
	@printf "$(COLOR_YELLOW)Performing deep clean...$(COLOR_RESET)\n"
	@# Remove Python cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "Removed __pycache__ directories"
	@# Remove benchmark directory and tarball
	@rm -rf $(BENCHMARK_DIR) $(BENCHMARK_TARBALL)
	@echo "Removed benchmark directory and tarball"
	@printf "$(COLOR_GREEN)✓ Deep clean complete$(COLOR_RESET)\n"
