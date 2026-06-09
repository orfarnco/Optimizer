import streamlit as st
import subprocess
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import configparser

APP_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = APP_DIR / "optimizer_progress.json"
STOP_FILE = APP_DIR / "optimizer_stop.flag"
PARAM_CATALOG_FILE = APP_DIR / "strategy_parameters.json"

# Page configuration
st.set_page_config(
    page_title="TradingView Strategy Optimizer",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        text-align: center;
        margin-bottom: 2rem;
    }
    .section-header {
        font-size: 1.5rem;
        font-weight: bold;
        color: #2c3e50;
        margin-top: 1rem;
        margin-bottom: 1rem;
    }
    .parameter-card {
        background-color: #f8f9fa;
        padding: 1rem;
        border-radius: 0.5rem;
        border: 1px solid #e9ecef;
        margin-bottom: 1rem;
    }
    .result-card {
        background-color: #e8f5e8;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #28a745;
        margin-bottom: 1rem;
    }
    .stButton>button {
        width: 100%;
        height: 3rem;
        font-size: 1.1rem;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

def load_config():
    """Load configuration from config.ini file"""
    config = configparser.ConfigParser()
    config_path = Path("config.ini")

    if config_path.exists():
        config.read(config_path)
        return config
    else:
        # Create default config if it doesn't exist
        config.add_section('DEFAULTS')
        config.set('DEFAULTS', 'symbol', 'EURUSD')
        config.set('DEFAULTS', 'timeframe', '1D')
        config.set('DEFAULTS', 'strategy', 'Supertrend Strategy')
        config.set('DEFAULTS', 'backtest_range', '365d')
        with open(config_path, 'w') as f:
            config.write(f)

        return config


def read_progress():
    if not PROGRESS_FILE.exists():
        return {}
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_parameter_catalog():
    if not PARAM_CATALOG_FILE.exists():
        return {}
    try:
        return json.loads(PARAM_CATALOG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_log_tail(path, max_lines=40):
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def saved_strategy_parameters(strategy):
    catalog = load_parameter_catalog()
    strategy_data = catalog.get("strategies", {}).get(strategy, {})
    parameters = strategy_data.get("parameters", [])
    return [param for param in parameters if str(param).strip()]


def saved_strategy_updated_at(strategy):
    catalog = load_parameter_catalog()
    strategy_data = catalog.get("strategies", {}).get(strategy, {})
    return strategy_data.get("updated_at")


def saved_strategy_parameter_options(strategy, parameter_name):
    catalog = load_parameter_catalog()
    strategy_data = catalog.get("strategies", {}).get(strategy, {})
    options = strategy_data.get("parameter_options", {}).get(parameter_name, [])
    return [option for option in options if str(option).strip()]


def report_files():
    files = list(APP_DIR.glob("Optimization_Report*.pdf"))
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files


def parse_parameter_count(param):
    if ":" not in param or "=" not in param:
        return 1

    _, spec = param.split(":", 1)
    param_type, raw_value = spec.split("=", 1)
    param_type = param_type.strip().lower()
    raw_value = raw_value.strip()

    if param_type == "bool":
        return 2 if raw_value.lower() in {"both", "all", "true,false", "false,true"} else 1

    if param_type == "range":
        parts = [part.strip() for part in raw_value.split(",") if part.strip()]
        if len(parts) != 3:
            return 0
        start, end, step = (Decimal(part) for part in parts)
        if step == 0:
            return 0
        if step > 0 and start > end:
            return 0
        if step < 0 and start < end:
            return 0
        return int(abs((end - start) / step)) + 1

    if param_type == "list":
        return len([part for part in raw_value.split(",") if part.strip()])

    return 1


def total_parameter_combinations(parameters):
    total = 1
    for param in parameters:
        count = parse_parameter_count(param)
        if count == 0:
            return 0
        total *= count
    return total

def main():
    # Load configuration
    config = load_config()
    st.markdown('<div class="main-header">📈 TradingView Strategy Optimizer</div>', unsafe_allow_html=True)

    # Sidebar for configuration
    with st.sidebar:
        st.header("⚙️ Configuration")

        # Basic settings
        symbol = st.text_input("Symbol", value=config.get('DEFAULTS', 'symbol', fallback='EURUSD'), help="Trading symbol (e.g., EURUSD, BTCUSD)")
        timeframe_options = ["1", "5", "15", "30", "60", "240", "1D", "1W"]
        default_timeframe = config.get('DEFAULTS', 'timeframe', fallback='1D')
        timeframe_index = timeframe_options.index(default_timeframe) if default_timeframe in timeframe_options else 6
        timeframe = st.selectbox("Timeframe", timeframe_options, index=timeframe_index,
                                help="Chart timeframe")
        strategy = st.text_input("Strategy Name", value=config.get('DEFAULTS', 'strategy', fallback='Supertrend Strategy'),
                                help="Name of the strategy in TradingView")

        # Backtest range
        st.subheader("📅 Backtest Range")
        backtest_options = ["7d", "30d", "90d", "365d", "1y", "all"]
        backtest_labels = ["Last 7 days", "Last 30 days", "Last 90 days", "Last 365 days", "Last year", "Entire history"]
        default_backtest = config.get('DEFAULTS', 'backtest_range', fallback='365d')
        backtest_index = backtest_options.index(default_backtest) if default_backtest in backtest_options else 3
        backtest_range = st.selectbox("Range", backtest_options, index=backtest_index,
                                     format_func=lambda x: backtest_labels[backtest_options.index(x)])

        st.subheader("Optimization Metric")
        metric_options = {
            "Profit / Max Drawdown": "pnl_dd",
            "Win Rate": "win_rate",
        }
        optimization_metric_label = st.selectbox(
            "Metric",
            list(metric_options.keys()),
            help="Choose how results are sorted and scored in the PDF report",
        )
        optimization_metric = metric_options[optimization_metric_label]

        # Help section
        st.markdown("---")
        st.subheader("ℹ️ Help")
        with st.expander("How to use"):
            st.markdown("""
            **1. Configure Strategy:**
            - Enter symbol and timeframe
            - Strategy name must match TradingView exactly

            **2. Add Parameters:**
            - **Boolean**: Use for checkboxes (e.g., "Use Filter: bool=True")
            - **Range**: Use for numeric ranges (e.g., "Period: range=10,50,5")
            - **List**: Use for dropdowns (e.g., "Source: list=Close,Open,High,Low")

            **3. Run Optimization:**
            - Click "Start Optimization"
            - Monitor progress
            - Download PDF report
            """)

        with st.expander("Parameter Examples"):
            st.code("""
# Boolean parameter
Use Volatility Filter: bool=True

# Numeric range (start,end,step)
ATR Period: range=10,50,5

# List of options
Source: list=Close,Open,High,Low
            """)

    # Main content
    col1, col2 = st.columns([2, 1])

    with col1:
        st.markdown('<div class="section-header">🔧 Parameter Configuration</div>', unsafe_allow_html=True)

        # Parameter management
        if 'parameters' not in st.session_state:
            st.session_state.parameters = []

        saved_params = saved_strategy_parameters(strategy)
        scan_process = st.session_state.get("scan_process")
        scan_is_running = scan_process is not None and scan_process.poll() is None

        scan_col_a, scan_col_b = st.columns([1, 2])
        with scan_col_a:
            if st.button("Scan Bot Variables", disabled=scan_is_running):
                run_parameter_scan(symbol, timeframe, strategy, backtest_range)
        with scan_col_b:
            if scan_is_running:
                progress = read_progress()
                if progress.get("status") == "error":
                    st.error(progress.get("message", "Variable scan failed."))
                else:
                    st.info(progress.get("message", "Scanning bot variables..."))
                time.sleep(1)
                st.rerun()
            elif scan_process is not None:
                st.session_state.scan_process = None
                progress = read_progress()
                if progress.get("status") == "scan_finished" or scan_process.returncode == 0:
                    st.success(progress.get("message", "Bot variables saved."))
                    st.rerun()
                else:
                    st.error("Variable scan failed. Check optimizer_output.log.")
                    log_tail = read_log_tail(APP_DIR / "optimizer_output.log")
                    if log_tail:
                        with st.expander("Last log lines"):
                            st.code(log_tail, language="text")
            elif saved_params:
                updated_at = saved_strategy_updated_at(strategy)
                suffix = f" Last scan: {updated_at}" if updated_at else ""
                st.caption(f"{len(saved_params)} saved variables available.{suffix}")
            else:
                st.caption("No saved variables yet for this bot.")

        # Add parameter form
        with st.expander("➕ Add Parameter", expanded=len(st.session_state.parameters) == 0):
            param_type = st.selectbox(
                "Parameter Type",
                ["range", "list", "bool"],
                help="Choose the kind of TradingView setting you want to optimize",
                key="new_param_type",
            )

            if saved_params:
                selected_param = st.selectbox(
                    "Parameter Name",
                    saved_params + ["Type manually..."],
                    help="Choose a saved TradingView input name or type one manually",
                    key="parameter_name_select",
                )
                if selected_param == "Type manually...":
                    param_name = st.text_input("Manual Parameter Name", key="manual_parameter_name")
                else:
                    param_name = selected_param
            else:
                param_name = st.text_input(
                    "Parameter Name",
                    help="Exact name as shown in TradingView strategy settings",
                    key="manual_parameter_name_no_saved",
                )

            with st.form("add_param_form"):
                if param_type == "bool":
                    bool_value = st.selectbox(
                        "Values to test",
                        ["Both True and False", "True only", "False only"],
                        help="Choose whether to test both checkbox states or only one state",
                    )
                    bool_value = {
                        "Both True and False": "both",
                        "True only": "True",
                        "False only": "False",
                    }[bool_value]
                    param_value = f"bool={bool_value}"

                elif param_type == "range":
                    col_a, col_b, col_c = st.columns(3)
                    with col_a:
                        start_val = st.number_input("Start", value=1.0, step=0.0001, format="%.4f")
                    with col_b:
                        end_val = st.number_input("End", value=10.0, step=0.0001, format="%.4f")
                    with col_c:
                        step_val = st.number_input("Step", value=1.0, step=0.0001, format="%.4f")
                    param_value = f"range={start_val:.4f},{end_val:.4f},{step_val:.4f}"

                elif param_type == "list":
                    saved_options = saved_strategy_parameter_options(strategy, param_name)
                    if saved_options:
                        selected_options = st.multiselect(
                            "Values",
                            saved_options,
                            default=saved_options,
                            help="Scanned TradingView options for this variable",
                        )
                        list_values = ",".join(selected_options)
                    else:
                        list_values = st.text_input("Values (comma-separated)",
                                                  placeholder="option1,option2,option3",
                                                  help="Comma-separated list of values")
                    param_value = f"list={list_values}"

                submitted = st.form_submit_button("Add Parameter")
                if submitted and param_name:
                    if param_type == "list" and not list_values.strip():
                        st.error("Please enter at least one list value.")
                        st.stop()
                    param_str = f"{param_name}:{param_value}"
                    st.session_state.parameters.append(param_str)
                    st.success(f"Added parameter: {param_name}")
                    st.rerun()

        # Display current parameters
        if st.session_state.parameters:
            st.subheader("Current Parameters")

            for i, param in enumerate(st.session_state.parameters):
                with st.container():
                    col_a, col_b = st.columns([4, 1])
                    with col_a:
                        st.code(param, language="text")
                    with col_b:
                        if st.button("🗑️", key=f"delete_{i}", help="Remove parameter"):
                            st.session_state.parameters.pop(i)
                            st.rerun()

            # Clear all parameters
            if st.button("🗑️ Clear All Parameters"):
                st.session_state.parameters = []
                st.rerun()

    with col2:
        st.markdown('<div class="section-header">🚀 Run Optimization</div>', unsafe_allow_html=True)

        # Run button
        if st.session_state.parameters:
            params_str = ",".join(st.session_state.parameters)
            total_combinations = total_parameter_combinations(st.session_state.parameters)
            st.metric("Total Combinations", total_combinations)
            running_process = st.session_state.get("optimization_process")
            is_running = running_process is not None and running_process.poll() is None
            if total_combinations == 0:
                st.error("Range step cannot be 0 and must move from Start toward End.")

            if st.button("▶️ Start Optimization", type="primary"):
                if not is_running and total_combinations > 0:
                    run_optimization(symbol, timeframe, strategy, params_str, backtest_range, optimization_metric)
            if is_running and st.button("Stop and Generate PDF", type="secondary"):
                STOP_FILE.write_text("stop", encoding="utf-8")
                st.warning("Stop requested. The current combination will finish, then the PDF will be generated.")
        else:
            st.warning("Add at least one parameter to start optimization")

        # Status and results
        running_process = st.session_state.get("optimization_process")
        if running_process is not None:
            progress = read_progress()
            current = int(progress.get("current", 0) or 0)
            total = int(progress.get("total", 0) or 0)
            status = progress.get("status", "running")
            message = progress.get("message", "")

            if total:
                st.progress(min(current / total, 1.0))
                st.info(f"Combination {current}/{total} - {message}")
            else:
                st.info(message or "Optimization is starting...")

            if running_process.poll() is None:
                time.sleep(1)
                st.rerun()
            else:
                st.session_state.optimization_process = None
                if status == "stopped":
                    st.warning("Optimization stopped. PDF generated from completed combinations.")
                elif status == "finished" or progress.get("report_file") or running_process.returncode == 0:
                    st.success("Optimization completed successfully.")
                else:
                    st.error("Optimization stopped with an error. Check optimizer_output.log.")

        if False and running_process is not None:
            st.info("🔄 Optimization in progress...")

            # Progress bar
            progress_bar = st.progress(0)
            status_text = st.empty()

            # Simulate progress (in real implementation, this would come from the subprocess)
            for i in range(100):
                progress_bar.progress(i + 1)
                status_text.text(f"Processing combination {i+1}/{max_results}")
                time.sleep(0.1)

            st.session_state.optimization_running = False
            st.success("✅ Optimization completed!")

    # Results section
    st.markdown('<div class="section-header">📊 Recent Results</div>', unsafe_allow_html=True)

    # Look for recent PDF files
    pdf_files = report_files()
    if pdf_files:
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Reports", len(pdf_files))
        with col2:
            latest_pdf = pdf_files[0]
            latest_run = datetime.fromtimestamp(latest_pdf.stat().st_mtime)
            st.metric("Latest Run", latest_run.strftime("%Y-%m-%d %H:%M"))
        with col3:
            if st.button("📂 Open Reports Folder"):
                os.startfile(".") if os.name == 'nt' else subprocess.run(["xdg-open", "."])

        # Show recent reports
        st.subheader("Recent Optimization Reports")
        for i, pdf_file in enumerate(pdf_files[:5]):  # Show last 5 reports
            col_a, col_b, col_c = st.columns([3, 1, 1])
            with col_a:
                st.write(f"📄 {pdf_file.name}")
            with col_b:
                file_size = pdf_file.stat().st_size / 1024  # KB
                st.write(f"{file_size:.1f} KB")
            with col_c:
                with open(pdf_file, "rb") as f:
                    st.download_button(
                        label="⬇️",
                        data=f,
                        file_name=pdf_file.name,
                        mime="application/pdf",
                        key=f"download_{i}"
                    )
    else:
        st.info("No optimization reports found yet. Run your first optimization to generate reports!")


def run_parameter_scan(symbol, timeframe, strategy, backtest_range):
    """Scan the selected TradingView strategy and save its input names."""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
    log_file = APP_DIR / "optimizer_output.log"

    cmd = [
        sys.executable, "chrome.py",
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--strategy", strategy,
        "--backtest-range", backtest_range,
        "--scan-params",
        "--param-catalog-file", str(PARAM_CATALOG_FILE),
        "--progress-file", str(PROGRESS_FILE),
    ]

    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        child_env = os.environ.copy()
        if any(key.startswith("RAILWAY_") for key in child_env):
            child_env["PLAYWRIGHT_HEADLESS"] = "true"
        process = subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            text=True,
        )
        st.session_state.scan_process = process
        st.rerun()
    except Exception as e:
        st.error(f"Error scanning bot variables: {str(e)}")


def run_optimization(symbol, timeframe, strategy, params, backtest_range, optimization_metric):
    """Run the optimization script"""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    log_file = APP_DIR / "optimizer_output.log"

    # Prepare command
    cmd = [
        sys.executable, "chrome.py",
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--strategy", strategy,
        "--params", params,
        "--backtest-range", backtest_range,
        "--optimization-metric", optimization_metric,
        "--progress-file", str(PROGRESS_FILE),
        "--stop-file", str(STOP_FILE)
    ]

    # Run the command
    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        st.session_state.optimization_process = process
        st.rerun()
        return

        if result.returncode == 0:
            st.success("Optimization completed successfully!")

            # Look for PDF files
            pdf_files = list(Path(".").glob("Optimization_Report_*.pdf"))
            if pdf_files:
                latest_pdf = max(pdf_files, key=lambda x: x.stat().st_mtime)
                with open(latest_pdf, "rb") as f:
                    st.download_button(
                        label="📄 Download PDF Report",
                        data=f,
                        file_name=latest_pdf.name,
                        mime="application/pdf"
                    )
        else:
            st.error(f"Optimization failed: {result.stderr}")

    except Exception as e:
        st.error(f"Error running optimization: {str(e)}")

    st.session_state.optimization_running = False

if __name__ == "__main__":
    main()
