# TradingView Strategy Optimizer

A comprehensive web interface for optimizing TradingView strategies with automated parameter testing and PDF report generation.

## Features

- 🎯 **Easy Parameter Configuration**: Support for boolean, numeric range, and list parameters
- 📊 **Real-time Optimization**: Run parameter combinations directly from the web interface
- 📄 **PDF Report Generation**: Automatic generation of detailed optimization reports
- 🎨 **Modern UI**: Clean, intuitive interface built with Streamlit
- ⚡ **Fast Execution**: Optimized for quick parameter testing

## Installation

1. **Install Python Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Playwright Browsers**:
   ```bash
   playwright install
   ```

3. **Set up TradingView Profile**:
   - The app uses a persistent Chrome profile stored in `tv_chrome_profile/`
   - First run will create the profile - log into TradingView manually
   - Subsequent runs will reuse your login session

## Usage

### Running the Interface

**Option 1: Python Launcher (Recommended)**
```bash
python run_optimizer.py
```

**Option 2: Direct Streamlit**
```bash
streamlit run streamlit_app.py
```

**Option 3: Windows Batch File**
```bash
run_optimizer.bat
```

This will open a web browser with the optimization interface at `http://localhost:8501`.

## Deploying to Railway

This project includes a `Dockerfile` for Railway. The Docker build installs Python dependencies, Playwright, Chromium, and the required Linux browser libraries.

### Deploy from GitHub

1. Push this project to a GitHub repository.
2. In Railway, create a new project and choose **Deploy from GitHub repo**.
3. Select this repository. Railway will detect the `Dockerfile` automatically.
4. After the first deploy, open **Settings > Networking** and generate a public domain.

### Deploy from the Railway CLI

```bash
railway login
railway init
railway up
```

The app listens on Railway's `PORT` environment variable via:

```bash
streamlit run streamlit_app.py --server.address=0.0.0.0 --server.port=$PORT
```

### TradingView login note

On Railway, Playwright runs headlessly using the bundled Chromium browser. The local `tv_chrome_profile/` folder is intentionally excluded from deployment because it can contain private browser session data. If the TradingView workflow requires an interactive login, you will need to adapt the app to authenticate through environment variables, cookies, or another non-interactive login/session flow.

### Basic Workflow

1. **Configure Strategy**:
   - Enter the trading symbol (e.g., EURUSD)
   - Select timeframe (1D, 1W, etc.)
   - Enter the exact strategy name as it appears in TradingView

2. **Set Backtest Range**:
   - Choose from predefined ranges (Last 7 days, Last 30 days, etc.)

3. **Add Parameters**:
   - Click **Scan Bot Variables** once for a strategy to save its TradingView input names
   - After scanning, choose parameter names from the dropdown instead of typing them manually
   - **Boolean Parameters**: For checkboxes like "Use Volatility Filter"
     - Type: `bool`
     - Value: `True` or `False`
   - **Numeric Ranges**: For parameters like "Period" from 10 to 50 with step 5
     - Type: `range`
     - Values: `10,50,5`
   - **List Parameters**: For dropdown options like "Source: Close,Open,High,Low"
     - Type: `list`
     - Values: `Close,Open,High,Low`

4. **Run Optimization**:
   - Set maximum combinations to test
   - Click "Start Optimization"
   - Monitor progress in real-time
   - Download the PDF report when complete

## Parameter Examples

### Boolean Parameter
```
Parameter Name: Use Volatility Ratio Filter
Type: bool
Value: True
```

### Numeric Range Parameter
```
Parameter Name: ATR Period
Type: range
Start: 10, End: 50, Step: 5
```

### List Parameter
```
Parameter Name: Source
Type: list
Values: Close,Open,High,Low
```

## Command Line Usage

You can also run optimizations directly from command line:

```bash
python chrome.py --symbol "EURUSD" --timeframe "1D" --strategy "Supertrend Strategy" --params "Use Volatility Ratio Filter:bool=True,ATR Period:range=10,50,5" --backtest-range "365d" --max-results 50
```

## Output

- **Console Output**: Real-time progress and results
- **Strategy Parameter Catalog**: Saved bot input names in `strategy_parameters.json`
- **PDF Report**: Comprehensive report with:
  - Top 3 parameter combinations
  - Top 10 results table
  - Profit/Loss, Drawdown, and Win Rate metrics
  - Optimization score (Profit/Drawdown ratio)

## Troubleshooting

### Common Issues

1. **Browser doesn't start**: Make sure Chrome is installed and Playwright browsers are installed
2. **Login issues**: Clear the `tv_chrome_profile/` folder and log in again
3. **Strategy not found**: Ensure the strategy name matches exactly in TradingView
4. **Parameters not set**: Verify parameter names match the strategy settings exactly

### Debug Mode

For debugging parameter detection, the script provides detailed console output showing:
- Parameter detection attempts
- Element location success/failure
- Current vs desired parameter values

## Architecture

- **streamlit_app.py**: Web interface and parameter management
- **chrome.py**: Core optimization logic using Playwright
- **fonts/**: Hebrew font support for PDF generation
- **tv_chrome_profile/**: Persistent browser session data

## Requirements

- Python 3.8+
- Google Chrome browser
- Internet connection for TradingView access
- Sufficient RAM for browser automation (4GB+ recommended)

## License

This project is for educational and personal use. Please respect TradingView's terms of service.
