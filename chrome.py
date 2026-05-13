from playwright.sync_api import sync_playwright
import argparse
import json
import os
import time
import re
from pathlib import Path
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from datetime import datetime
from decimal import Decimal
from itertools import product
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from bidi.algorithm import get_display

TRADINGVIEW_URL = "https://www.tradingview.com/"
pdfmetrics.registerFont(
    TTFont("DejaVuSans", "fonts/DejaVuSans.ttf")
)

hebrew_style = ParagraphStyle(
    "Hebrew",
    fontName="DejaVuSans",
    fontSize=10
)


def parse_numeric_or_string(raw):
    raw = raw.strip()
    if raw == "":
        raise ValueError("Empty value")

    try:
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def parse_params_arg(raw_params):
    params = []
    if not raw_params:
        return params

    entries = re.split(r",(?=[^,]+:(?:bool|range|list)=)", raw_params)
    for entry in entries:
        if ":" not in entry or "=" not in entry:
            raise ValueError(f"Invalid parameter format: {entry}")

        name, spec = entry.split(":", 1)
        param_type, raw_value = spec.split("=", 1)
        name = name.strip()
        param_type = param_type.strip().lower()
        raw_value = raw_value.strip()

        if param_type == "bool":
            if raw_value.lower() in {"both", "all", "true,false", "false,true"}:
                values = [True, False]
            else:
                values = [raw_value.lower() in {"true", "1", "yes", "y"}]
            params.append({"name": name, "values": values})
        elif param_type == "range":
            parts = [part.strip() for part in raw_value.split(",") if part.strip()]
            if len(parts) != 3:
                raise ValueError(f"Range parameter '{name}' must be start,end,step")
            params.append({
                "name": name,
                "start": parse_numeric_or_string(parts[0]),
                "end": parse_numeric_or_string(parts[1]),
                "step": parse_numeric_or_string(parts[2]),
            })
        elif param_type == "list":
            values = [parse_numeric_or_string(part) for part in raw_value.split(",") if part.strip()]
            params.append({"name": name, "values": values})
        else:
            raise ValueError(f"Unsupported parameter type '{param_type}' for '{name}'")

    return params


def parse_args():
    parser = argparse.ArgumentParser(description="TradingView strategy optimizer")
    parser.add_argument("--symbol", default="NQ")
    parser.add_argument("--timeframe", default="1")
    parser.add_argument("--strategy", default="NQ Stop Orders System")
    parser.add_argument("--params", default="")
    parser.add_argument("--backtest-range", default="365d")
    parser.add_argument(
        "--optimization-metric",
        choices=["pnl_dd", "win_rate"],
        default="pnl_dd",
    )
    parser.add_argument("--progress-file", default="optimizer_progress.json")
    parser.add_argument("--stop-file", default="optimizer_stop.flag")
    parser.add_argument("--scan-params", action="store_true")
    parser.add_argument("--param-catalog-file", default="strategy_parameters.json")
    return parser.parse_args()


def backtest_range_label(value):
    labels = {
        "7d": "Last 7 days",
        "30d": "Last 30 days",
        "90d": "Last 90 days",
        "365d": "Last 365 days",
        "1y": "Last year",
        "all": "Entire history",
    }
    return labels.get(value, value)


def write_progress(progress_file, **data):
    progress_path = Path(progress_file)
    progress_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_strategy_parameters(catalog_file, strategy, symbol, timeframe, parameter_names, parameter_options=None):
    catalog_path = Path(catalog_file)
    if catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            catalog = {}
    else:
        catalog = {}

    catalog.setdefault("strategies", {})
    catalog["strategies"][strategy] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "parameters": parameter_names,
        "parameter_options": parameter_options or {},
    }
    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def metric_label(metric):
    labels = {
        "pnl_dd": "Profit / Max Drawdown",
        "win_rate": "Win Rate",
    }
    return labels.get(metric, metric)


def calculate_score(pnl, dd, wr, metric):
    if metric == "win_rate":
        return wr
    return pnl / dd if dd != 0 else 0


def parse_tradingview_number(raw):
    no_data_values = {"", "-", "\u2013", "\u2014", "\u2212", "—", "–"}
    raw_text = str(raw).strip()
    if raw_text in no_data_values:
        return 0.0

    cleaned = (
        raw_text
        .replace("\u2212", "-")
        .replace("\xa0", " ")
        .replace(",", "")
        .replace("+", "")
        .replace("%", "")
        .replace("USD", "")
        .replace("$", "")
        .strip()
    )
    if cleaned in no_data_values:
        return 0.0
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if match:
        return float(match.group(0))
    return float(cleaned)


def main():
    args = parse_args()
    symbol = args.symbol
    bot_name = args.strategy
    timeframe = args.timeframe
    range_label = backtest_range_label(args.backtest_range)
    optimization_metric = args.optimization_metric
    stop_file = Path(args.stop_file)
    if stop_file.exists():
        stop_file.unlink()
    write_progress(args.progress_file, status="starting", current=0, total=0, message="Starting browser")

    with sync_playwright() as p:
        # תיקיית פרופיל מקומית (cookies/session נשמרים פה)
        user_data_dir = "tv_chrome_profile"

        running_on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "true" if running_on_railway else "false").lower() in {
            "1",
            "true",
            "yes",
        }
        launch_options = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            "viewport": None,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }

        if not headless:
            launch_options["channel"] = "chrome"          # מנסה להשתמש בכרום המותקן
            launch_options["args"].append("--start-maximized")

        context = p.chromium.launch_persistent_context(**launch_options)

        page = context.new_page()
        page.set_viewport_size({"width": 1500, "height": 1080})
        page.goto(TRADINGVIEW_URL, wait_until="domcontentloaded")
        # לחכות שהעמוד נטען
        page.wait_for_load_state("domcontentloaded")

        # ללחוץ על כפתור פתיחת תפריט משתמש
        # page.get_by_role("button", name="Open user menu").nth(0).click()

        # לחכות שה-dropdown יופיע
        page.wait_for_timeout(1000)
        page.get_by_role("button", name="Search").click()
        search_input = page.locator("input[name='query']")
        search_input.wait_for(state="visible")
        search_input.fill(symbol)
        page.locator("[data-role='list-item']").first.wait_for(state="visible")
        page.locator("[data-role='list-item']").first.click()
        page.wait_for_timeout(5000)
        def right_click_chart(page):

            canvas = page.locator("canvas[data-qa-id='pane-top-canvas']")

            canvas.wait_for()
            canvas.click(button="right")

            print("Right click on chart executed")
        def remove_all_indicators_from_menu(page):

            # מחפש אופציה שמכילה את המילה Remove
            menu_item = page.get_by_role("menuitem").filter(has_text="Remove")

            menu_item.first.click()

            print("Clicked remove from context menu")

        def clear_indicators_via_context_menu(page):
            page.wait_for_timeout(1000)

            # 1️⃣ קליק ימני על הגרף
            canvas = page.locator("canvas[data-qa-id='pane-top-canvas']")
            canvas.wait_for()

            # קבלת המידות האמיתיות של האלמנט
            box = canvas.bounding_box()

            if not box:
                raise Exception("Could not get canvas bounding box")

            # נלחץ 10 פיקסלים מהקצה הימני
            x_offset = box["width"] - 10
            y_offset = box["height"] / 2  # באמצע אנכית

            canvas.click(
                button="right",
                position={"x": x_offset, "y": y_offset}
            )

            print("Right-clicked on right side of canvas")
            page.wait_for_timeout(1000)

            print("Context menu opened")

            # 2️⃣ למצוא Remove X indicators (טקסט דינמי)
            remove_option = page.locator("span.label-GJX1EXhk").filter(
                has_text="indicator"
            )

            if remove_option.count() == 0:
                print("No indicators remove option found")
                return

            remove_option.first.click()

            print("Clicked remove indicators")

            page.wait_for_timeout(1000)
        clear_indicators_via_context_menu(page)
        page.wait_for_timeout(1000)

        page.locator("[data-name='open-indicators-dialog']:visible").click()

        search_input = page.locator("#indicators-dialog-search-input:visible")
        search_input.wait_for(state="visible")
        page.wait_for_timeout(2000)

        search_input.fill(bot_name)
        page.wait_for_timeout(2000)

        first_script = page.locator("[data-role='list-item']").first
        first_script.wait_for(state="visible")
        first_script.click()
        page.locator("button[data-qa-id='close']:visible").click()
        page.wait_for_timeout(2000)


        page.locator(
            f"button[data-strategy-title='{bot_name}']:visible"
        ).click()
        page.wait_for_timeout(1000)



        page.locator("div[class^='right-'] span[class^='contextActions-']:visible").first.click()
        page.wait_for_timeout(2000)
        def set_timeframe(page, value: str):

            # פתיחת תפריט
            page.locator("button[aria-label='Chart interval']:visible").first.click()

            # חכה ל-dropdown
            page.locator("div.dropdown-S_1OCXUK:visible").wait_for()

            # לחץ לפי data-value
            page.locator(f"[data-role='menuitem'][data-value='{value}']:visible").first.click()

        def set_backtest_range(page, range_label: str):

            # פותח את תפריט הטווח
            page.locator("button:has-text('—'):visible").first.click()

            # מחכה שהתפריט יופיע
            page.locator("div[role='menuitemcheckbox']").first.wait_for()

            # לוחץ לפי aria-label
            page.locator(
                f"[role='menuitemcheckbox'][aria-label='{range_label}']:visible"
            ).click()

        set_timeframe(page, timeframe) 

        page.wait_for_timeout(2000)

        # set_backtest_range(page, "Last 7 days")
        # set_backtest_range(page, "Last 30 days")
        # set_backtest_range(page, "Last 90 days")
        set_backtest_range(page, range_label)
        # set_backtest_range(page, "Entire history")
        # ללחוץ על Sign in לפי טקסט
        page.wait_for_timeout(1000)

        def rtl(text):
            return get_display(text)

        def float_range(start, end, step):
            values = []
            current = Decimal(str(start))
            end_value = Decimal(str(end))
            step_value = Decimal(str(step))
            if step_value == 0:
                raise ValueError("Step value must not be zero")
            if step_value > 0:
                while current <= end_value:
                    values.append(float(current))
                    current += step_value
            else:
                while current >= end_value:
                    values.append(float(current))
                    current += step_value
            return values

        def format_value(value):
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, float):
                return f"{value:.10f}".rstrip("0").rstrip(".")
            return str(value)

        def generate_param_combinations(params):

            value_lists = []

            for p in params:
                if "values" in p:
                    values = p["values"]
                else:
                    start = p["start"]
                    end = p["end"]
                    step = p["step"]

                    if isinstance(start, int) and isinstance(end, int) and isinstance(step, int):
                        values = list(range(start, end + 1, step))
                    else:
                        values = float_range(float(start), float(end), float(step))
                value_lists.append(values)

            return list(product(*value_lists))

        def wait_for_strategy_settings_modal(page, timeout=30000):
            submit_button = page.locator("button[data-qa-id='submit-button']:visible").first
            submit_button.wait_for(state="visible", timeout=timeout)

            content_selectors = [
                "div.cell-RLntasnw.first-RLntasnw:visible",
                "input[data-qa-id='ui-lib-Input-input']:visible",
                "button[role='combobox']:visible",
                "input[type='checkbox'][data-qa-id*='ui-lib-checkbox-input']:visible",
            ]

            deadline = time.monotonic() + (timeout / 1000)
            while time.monotonic() < deadline:
                for selector in content_selectors:
                    try:
                        locator = page.locator(selector)
                        if locator.count() > 0:
                            locator.first.wait_for(state="visible", timeout=1000)
                            return
                    except Exception:
                        pass
                page.wait_for_timeout(250)

            visible_names = get_visible_parameter_names(page)
            preview = ", ".join(visible_names[:10]) if visible_names else "none found"
            raise Exception(
                "Settings modal opened, but no editable strategy controls were found. "
                f"Visible parameter names: {preview}"
            )

        def open_last_strategy_settings(page):


            item = page.locator(
                "div[data-qa-id='legend-source-item']"
            ).filter(
                has=page.locator("div.title-l31H9iuA", has_text=bot_name)
            ).first

            item.wait_for()

            # מקבלים מיקום פיזי במסך
            box = item.bounding_box()
            if not box:
                raise Exception("No bounding box for legend item")

            # מזיזים עכבר פיזית למרכז האלמנט
            page.mouse.move(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2
            )

            # קליק פיזי אמיתי
            page.mouse.click(
                box["x"],
                box["y"] + box["height"] / 2
            )

            print("Clicked legend item via mouse")

            # עכשיו ללחוץ על כפתור ההגדרות
            settings_button = item.locator(
                "button[data-qa-id='legend-settings-action']"
            ).first

            settings_button.wait_for(state="attached", timeout=10000)
            last_error = None

            for attempt in range(1, 4):
                try:
                    settings_button.scroll_into_view_if_needed(timeout=5000)
                    settings_button.click(timeout=5000)
                    print(f"Clicked settings via locator (attempt {attempt})")
                except Exception as e:
                    last_error = e
                    settings_box = settings_button.bounding_box()
                    if not settings_box:
                        page.wait_for_timeout(500)
                        continue

                    page.mouse.click(
                        settings_box["x"] + settings_box["width"] / 2,
                        settings_box["y"] + settings_box["height"] / 2
                    )
                    print(f"Clicked settings via mouse (attempt {attempt})")

                try:
                    wait_for_strategy_settings_modal(page, timeout=15000)
                    print("Settings modal opened")
                    return
                except Exception as e:
                    last_error = e
                    page.wait_for_timeout(1000)

            raise Exception(f"Could not open settings modal after 3 attempts: {last_error}")

        def get_visible_parameter_names(page):
            try:
                names = []
                label_selectors = [
                    "div.cell-RLntasnw.first-RLntasnw",
                    "span.label-Lah5SRBd",
                ]
                for selector in label_selectors:
                    try:
                        names.extend(page.locator(selector).all_inner_texts())
                    except Exception:
                        pass

                unique_names = []
                seen = set()
                for name in names:
                    clean_name = name.strip()
                    if clean_name and clean_name not in seen:
                        unique_names.append(clean_name)
                        seen.add(clean_name)
                return unique_names
            except Exception:
                return []

        def collect_strategy_parameter_names(page):
            names = []
            seen = set()
            stable_rounds = 0

            for _ in range(30):
                before_count = len(seen)
                for name in get_visible_parameter_names(page):
                    if name not in seen:
                        names.append(name)
                        seen.add(name)

                if len(seen) == before_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                if stable_rounds >= 3:
                    break

                page.mouse.move(750, 540)
                page.mouse.wheel(0, 650)
                page.wait_for_timeout(250)

            return names

        def find_strategy_label_cell(page, param_name: str):
            escaped_name = re.escape(param_name.strip())
            candidates = [
                page.locator(f"div.cell-RLntasnw.first-RLntasnw:has-text('{param_name}')"),
                page.locator("div.cell-RLntasnw.first-RLntasnw").filter(
                    has_text=re.compile(escaped_name, re.IGNORECASE)
                ),
                page.locator(f"span.label-Lah5SRBd:has-text('{param_name}')").locator(
                    "xpath=ancestor::div[contains(@class, 'cell-RLntasnw')][1]"
                ),
                page.locator("span.label-Lah5SRBd").filter(
                    has_text=re.compile(escaped_name, re.IGNORECASE)
                ).locator("xpath=ancestor::div[contains(@class, 'cell-RLntasnw')][1]"),
            ]

            for candidate in candidates:
                if candidate.count() > 0:
                    return candidate

            visible_names = get_visible_parameter_names(page)
            preview = ", ".join(visible_names[:25]) if visible_names else "none found"
            raise Exception(
                f"Could not find strategy parameter '{param_name}'. "
                f"Visible parameter names: {preview}"
            )

        def collect_dropdown_options(page, param_name):
            try:
                label_cell = find_strategy_label_cell(page, param_name)
                dropdown_button = label_cell.locator(
                    "xpath=following-sibling::div[1]//button[@role='combobox']"
                ).first
                if dropdown_button.count() == 0:
                    return []

                dropdown_button.scroll_into_view_if_needed(timeout=3000)
                dropdown_button.click(timeout=3000)
                page.locator("div[role='option']").first.wait_for(state="visible", timeout=3000)

                options = []
                seen = set()
                for raw_option in page.locator("div[role='option']").all_inner_texts():
                    option = raw_option.strip()
                    if option and option not in seen:
                        options.append(option)
                        seen.add(option)

                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                return options
            except Exception as e:
                print(f"Could not scan list options for '{param_name}': {e}")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return []

        def collect_strategy_parameter_options(page, parameter_names):
            options_by_parameter = {}
            for index, param_name in enumerate(parameter_names, start=1):
                write_progress(
                    args.progress_file,
                    status="scanning",
                    current=index,
                    total=len(parameter_names),
                    message=f"Scanning options for {param_name}",
                )
                options = collect_dropdown_options(page, param_name)
                if options:
                    options_by_parameter[param_name] = options
                    print(f"Found {len(options)} options for '{param_name}'")
            return options_by_parameter

        if args.scan_params:
            write_progress(
                args.progress_file,
                status="scanning",
                current=0,
                total=0,
                message=f"Opening {bot_name} settings to scan variables",
            )
            open_last_strategy_settings(page)
            parameter_names = collect_strategy_parameter_names(page)
            if not parameter_names:
                raise Exception(f"No variables found for strategy '{bot_name}'")
            parameter_options = collect_strategy_parameter_options(page, parameter_names)
            save_strategy_parameters(
                args.param_catalog_file,
                bot_name,
                symbol,
                timeframe,
                parameter_names,
                parameter_options,
            )
            dropdown_count = len(parameter_options)
            write_progress(
                args.progress_file,
                status="scan_finished",
                current=len(parameter_names),
                total=len(parameter_names),
                message=f"Saved {len(parameter_names)} variables and {dropdown_count} option lists for {bot_name}",
                parameters=parameter_names,
                parameter_options=parameter_options,
            )
            print(f"Saved {len(parameter_names)} variables and {dropdown_count} option lists for {bot_name}")
            context.close()
            return

        def set_strategy_input(page, param_name: str, value: str):
            """
            משנה ערך של פרמטר טקסט/מספר לפי השם שמופיע בצד שמאל
            """

            # מאתר את התא עם שם הפרמטר
            label_cell = find_strategy_label_cell(page, param_name)

            # לוקח את האינפוט שנמצא לידו
            input_box = label_cell.locator(
                "xpath=following::input[@data-qa-id='ui-lib-Input-input'][1]"
            )

            # שינוי ערך בצורה שמפעילה את כל ה-events
            value_text = format_value(value)
            input_box = input_box.first
            if input_box.count() == 0:
                raise Exception(f"Could not find input box for parameter '{param_name}'")
            try:
                current_value = format_value(float(input_box.input_value(timeout=3000)))
            except Exception:
                try:
                    current_value = input_box.input_value(timeout=3000).strip()
                except Exception:
                    current_value = None

            if current_value == value_text:
                print(f"Input '{param_name}' already set to '{value_text}', skipping")
                return

            input_box.click(force=True, timeout=5000)
            input_box.press("Control+A")
            input_box.press("Backspace")
            input_box.type(value_text)
            input_box.press("Enter")

        def set_strategy_dropdown(page, param_name: str, option):
            """
            בוחר ערך מתוך dropdown לפי טקסט או מיקום ברשימה
            """

            label_cell = find_strategy_label_cell(page, param_name)

            dropdown_button = label_cell.locator(
                "xpath=following::button[@role='combobox'][1]"
            )

            dropdown_button.click()

            # Wait for options to appear
            page.locator("div[role='option']").first.wait_for()

            options = page.locator("div[role='option']")

            if isinstance(option, int):
                options.nth(option).click()
                print(f"Dropdown '{param_name}' set to option index {option}")
                return

            option_text = str(option)
            # Try exact match first
            matching = options.filter(has_text=option_text)
            if matching.count() == 0:
                # Try partial match
                matching = options.filter(has_text=option_text.split()[0])
            if matching.count() == 0:
                # Try case-insensitive match
                matching = options.filter(has_text=re.compile(f"(?i){re.escape(option_text)}"))

            if matching.count() == 0:
                print(f"Warning: Could not find option '{option_text}' in dropdown '{param_name}', selecting first option")
                options.first.click()
                print(f"Dropdown '{param_name}' set to first option (could not find '{option_text}')")
            else:
                matching.first.click()
                print(f"Dropdown '{param_name}' set to option '{option_text}'")

        def set_strategy_checkbox(page, param_name: str, checked: bool):
            """
            Set a checkbox parameter to checked or unchecked state by clicking the visible checkbox box.
            """
            print(f"Setting checkbox '{param_name}' to {checked}")

            # Try to find the cell containing the parameter name
            label_cell = find_strategy_label_cell(page, param_name)

            print(f"Found {label_cell.count()} label cells for '{param_name}'")

            # Find the clickable checkbox input (the actual interactive element)
            checkbox_input = label_cell.locator(
                "input[type='checkbox'][data-qa-id*='ui-lib-checkbox-input']"
            )

            print(f"Found {checkbox_input.count()} checkbox inputs")

            if checkbox_input.count() == 0:
                print(f"ERROR: Could not find checkbox input for '{param_name}'")
                return

            # Check current state using aria-checked attribute
            current_checked = checkbox_input.get_attribute("aria-checked") == "true"

            print(f"Current state: {'checked' if current_checked else 'unchecked'}, desired: {'checked' if checked else 'unchecked'}")

            if current_checked != checked:
                checkbox_input.click()
                print(f"Checkbox '{param_name}' clicked to {'check' if checked else 'uncheck'}")
            else:
                print(f"Checkbox '{param_name}' already {'checked' if checked else 'unchecked'}")

        def set_strategy_param(page, param_name: str, value):
            """
            Set a strategy parameter using checkbox, dropdown selection, or text input.
            """
            print(f"Setting parameter '{param_name}' to value: {value} (type: {type(value)})")

            label_cell = find_strategy_label_cell(page, param_name)

            print(f"Found {label_cell.count()} cells with parameter name '{param_name}'")

            # Try checkbox first (look for input element)
            checkbox = label_cell.locator(
                "input[type='checkbox'][data-qa-id*='ui-lib-checkbox-input']"
            )
            try:
                if checkbox.count() > 0:
                    print(f"Detected checkbox for '{param_name}', setting to {bool(value)}")
                    set_strategy_checkbox(page, param_name, bool(value))
                    return
            except Exception as e:
                print(f"Checkbox detection failed: {e}")

            # Try dropdown
            dropdown_button = label_cell.locator(
                "xpath=following::button[@role='combobox'][1]"
            )
            try:
                if dropdown_button.count() > 0:
                    print(f"Detected dropdown for '{param_name}', selecting '{value}'")
                    set_strategy_dropdown(page, param_name, value)
                    return
            except Exception as e:
                print(f"Dropdown detection failed: {e}")

            # Fallback to text input
            print(f"Using text input for '{param_name}' with value '{value}'")
            set_strategy_input(page, param_name, value)
        def apply_and_refresh_strategy(page):

            print("Clicking OK...")

            # 1️⃣ לחיצה על OK
            page.locator("button[data-qa-id='submit-button']").click()

            # 2️⃣ לחכות שהמודאל ייעלם
            page.locator("button[data-qa-id='submit-button']").wait_for(state="detached")

            print("Settings modal closed")

            # 3️⃣ למצוא שוב את ה-wrapper של ה-legend
            legend_wrapper = page.locator("div.sourcesWrapper-l31H9iuA")

            items = legend_wrapper.locator(
                "div[data-qa-id='legend-source-item']:visible"
            )

            count = items.count()
            if count == 0:
                raise Exception("No legend items found after closing modal")

            last_item = items.nth(count - 1)

            # 4️⃣ למצוא את כפתור העין של אותו בוט
            eye_button = last_item.locator(
                "button[data-qa-id='legend-show-hide-action']"
            )

            print("Clicking eye button twice...")

            # force בגלל בעיית canvas overlay
            eye_button.click(force=True)
            page.wait_for_timeout(500)
            eye_button.click(force=True)

            print("Eye toggled twice")

        def wait_for_report_update(page, previous_value=None, timeout=30):
            """
            Wait for the strategy report to be shown and ready.
            Return as soon as Total P&L and drawdown values are visible and not placeholders.
            """
            time.sleep(1)

            pnl_locator = page.locator(
                "div.containerCell-zres18Ue"
            ).filter(
                has=page.locator("div.title-nEWm7_ye", has_text="Total P&L")
            ).locator("div.value-DiHajR6I")

            dd_locator = page.locator(
                "div.containerCell-zres18Ue"
            ).filter(
                has=page.locator("div.title-nEWm7_ye", has_text="Max equity drawdown")
            ).locator("div.value-DiHajR6I")

            start_time = time.time()

            while True:
                try:
                    pnl_text = pnl_locator.inner_text().strip()
                    dd_text = dd_locator.inner_text().strip()

                    if (
                        pnl_text and pnl_text != "--" and
                        dd_text and dd_text != "--"
                    ):
                        print(f"Report ready: P&L {pnl_text}, DD {dd_text}")
                        return pnl_text
                except Exception:
                    pass

                if time.time() - start_time > timeout:
                    print(f"Timeout waiting for report update ({timeout}s), but proceeding with current values")
                    try:
                        return pnl_locator.inner_text().strip()
                    except Exception:
                        return previous_value or "0"

                time.sleep(0.5)

        def get_total_pnl(page):

            pnl = page.locator(
                "div.containerCell-zres18Ue"
            ).filter(
                has=page.locator("div.title-nEWm7_ye", has_text="Total P&L")
            ).locator("div.value-DiHajR6I")

            pnl.wait_for()
            return pnl.inner_text().strip()

        def get_report_values(page):

            def get_value_by_title(title_text, default="0"):
                try:
                    cell = page.locator(
                        "div.containerCell-zres18Ue"
                    ).filter(
                        has=page.locator("div.title-nEWm7_ye", has_text=title_text)
                    )

                    value_text = cell.locator("div.value-DiHajR6I").inner_text(timeout=5000).strip()
                    return value_text if value_text and value_text != "--" else default
                except Exception as e:
                    print(f"Warning: Could not find field '{title_text}', using default value '{default}'")
                    return default

            # Debug: Print available fields on first run
            if not hasattr(get_report_values, 'debug_printed'):
                try:
                    titles = page.locator("div.title-nEWm7_ye").all_inner_texts()
                    print("Available report fields:", titles[:10])  # Show first 10 fields
                    get_report_values.debug_printed = True
                except:
                    pass

            pnl_text = get_value_by_title("Total P&L")
            dd_text = get_value_by_title("Max equity drawdown")

            # Try different possible win rate field names
            wr_text = get_value_by_title("Profitable trades")
            trades_text = get_value_by_title("Total trades")


            # ניקוי טקסט → מספר
            pnl = parse_tradingview_number(pnl_text)
            dd = parse_tradingview_number(dd_text)
            wr = parse_tradingview_number(wr_text)
            trades = int(parse_tradingview_number(trades_text))

            return pnl, dd, wr, trades



        
                
        def generate_optimization_pdf(results, params, filename="Optimization_Report.pdf"):

            # ===== SORT RESULTS =====
            results_sorted = sorted(results, key=lambda x: x["score"], reverse=True)

            top10 = results_sorted[:10]
            top3 = results_sorted[:3]

            # ===== PDF SETUP =====
            doc = SimpleDocTemplate(filename)
            elements = []

            styles = getSampleStyleSheet()

            # ===== HEADER =====
            elements.append(Paragraph("Strategy Optimization Report", styles["Heading1"]))
            elements.append(Spacer(1, 20))

            elements.append(
                Paragraph(
                    rtl(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
                    hebrew_style,
                )
            )

            elements.append(
                Paragraph(
                    rtl(f"Total runs: {len(results)}"),
                    hebrew_style,
                )
            )

            elements.append(
                Paragraph(
                    rtl(f"Optimization Metric: {metric_label(optimization_metric)}"),
                    hebrew_style,
                )
            )
            elements.append(Spacer(1, 30))

            # ===== TOP 3 SECTION =====
            elements.append(Paragraph("Top 3 Configurations", styles["Heading2"]))
            elements.append(Spacer(1, 15))

            for result in top3:

                param_text = " | ".join(
                    f"{params[i]['name']} = {format_value(result['params'][i])}"
                    for i in range(len(params))
                )

                line = (
                    f"{param_text} | "
                    f"PnL: {result['pnl']:,.2f} USD | "
                    f"DD: {result['dd']:,.2f} USD | "
                    f"WR: {result['wr']:.2f}% | "
                    f"Trades: {result['trades']:,} | "
                    f"Score: {result['score']:.2f}"
                )

                elements.append(Paragraph(rtl(line), hebrew_style))
                elements.append(Spacer(1, 10))

            elements.append(Spacer(1, 25))

            # ===== TABLE DATA =====
            header = [rtl(p["name"]) for p in params] + ["PnL", "Drawdown", "Win Rate %", "Total Trades", "Score"]
            table_data = [header]

            for result in top10:

                row = [format_value(value) for value in result["params"]] + [
                    f"{result['pnl']:,.2f}",
                    f"{result['dd']:,.2f}",
                    f"{result['wr']:.2f}",
                    f"{result['trades']:,}",
                    f"{result['score']:.2f}",
                ]

                table_data.append(row)

            # ===== CREATE TABLE =====
            table = Table(table_data)

            table.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), "DejaVuSans"),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                    ]
                )
            )

            elements.append(Paragraph("Top 10 Results", styles["Heading2"]))
            elements.append(Spacer(1, 15))
            elements.append(table)

            elements.append(PageBreak())

            all_header = ["#"] + [rtl(p["name"]) for p in params] + ["PnL", "Drawdown", "Win Rate %", "Total Trades", "Score"]
            all_table_data = [all_header]

            for rank, result in enumerate(results_sorted, start=1):
                row = [rank] + [format_value(value) for value in result["params"]] + [
                    f"{result['pnl']:,.2f}",
                    f"{result['dd']:,.2f}",
                    f"{result['wr']:.2f}",
                    f"{result['trades']:,}",
                    f"{result['score']:.2f}",
                ]
                all_table_data.append(row)

            all_table = Table(all_table_data, repeatRows=1)
            all_table.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), "DejaVuSans"),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                    ]
                )
            )

            elements.append(Paragraph("All Results", styles["Heading2"]))
            elements.append(Spacer(1, 15))
            elements.append(all_table)

            # ===== BUILD PDF =====
            doc.build(elements)

            print("PDF generated:", filename)
        # ===== שימוש =====
        def run_grid_optimization(page, params):

            results = []

            combinations = generate_param_combinations(params)
            total_combinations = len(combinations)

            print("Total combinations:", total_combinations)
            write_progress(
                args.progress_file,
                status="running",
                current=0,
                total=total_combinations,
                message=f"Ready to test {total_combinations} combinations",
            )

            previous_pnl = get_total_pnl(page)

            for combo_index, combo in enumerate(combinations, start=1):
                if stop_file.exists():
                    print("Stop requested. Generating report from completed results.")
                    break

                write_progress(
                    args.progress_file,
                    status="running",
                    current=combo_index,
                    total=total_combinations,
                    message=f"Testing combination {combo_index}/{total_combinations}",
                    params=[format_value(value) for value in combo],
                )

                print("\n===== Testing combination =====")
                print(f"{combo_index}/{total_combinations}", combo)

                open_last_strategy_settings(page)

                # הגדרת כל הפרמטרים
                for i, param in enumerate(params):
                    set_strategy_param(page, param["name"], combo[i])

                apply_and_refresh_strategy(page)
                updated_pnl = wait_for_report_update(page, previous_pnl)

                # Always proceed since wait_for_report_update ensures report is ready
                previous_pnl = updated_pnl

                pnl, dd, wr, trades = get_report_values(page)

                score = calculate_score(pnl, dd, wr, optimization_metric)

                result = {
                    "params": combo,
                    "pnl": pnl,
                    "dd": dd,
                    "wr": wr,
                    "trades": trades,
                    "score": score
                }

                results.append(result)

                print(result)
                write_progress(
                    args.progress_file,
                    status="running",
                    current=combo_index,
                    total=total_combinations,
                    message=f"Completed combination {combo_index}/{total_combinations}",
                    latest_result=result,
                )

            report_name = f"Optimization_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            generate_optimization_pdf(results, params, report_name)
            final_status = "stopped" if stop_file.exists() else "finished"
            write_progress(
                args.progress_file,
                status=final_status,
                current=len(results),
                total=total_combinations,
                message=f"PDF generated: {report_name}",
                report_file=report_name,
            )

            print("Optimization finished")

            results_sorted = sorted(results, key=lambda x: x["score"], reverse=True)

            top10 = results_sorted[:10]
            top3 = results_sorted[:3]

            print("\n===== TOP 3 RESULTS =====")
            for r in top3:
                print(r)

            return top10, top3
        
        def parse_numeric_or_string(raw):
            raw = raw.strip()
            if raw == "":
                raise ValueError("Empty value")

            try:
                if "." in raw or "e" in raw.lower():
                    return float(raw)
                return int(raw)
            except ValueError:
                return raw

        def parse_param_values():
            while True:
                raw = input(
                    "Enter values for parameter (type 'range' for numeric range, 'list' for options, 'bool' for checkbox, or enter values directly): "
                ).strip()
                if not raw:
                    print("Please enter at least one value.")
                    continue

                if raw.lower() in {"list", "options", "values"}:
                    while True:
                        raw = input("Enter option values separated by commas (e.g., Basic Candle, Engulfing): ").strip()
                        if not raw:
                            print("Please enter at least one option.")
                            continue
                        if raw.lower() in {"list", "options", "values"}:
                            print("Please enter the actual option values, not the word 'list'.")
                            continue
                        break

                if raw.lower() == "range":
                    raw = input("Enter range values (start,end,step): ").strip()
                    if not raw:
                        print("Please enter range values.")
                        continue

                if raw.lower() == "bool":
                    return {"values": [True, False]}

                if "," in raw:
                    parts = [part.strip() for part in raw.split(",") if part.strip()]
                    if len(parts) == 3:
                        try:
                            start = parse_numeric_or_string(parts[0])
                            end = parse_numeric_or_string(parts[1])
                            step = parse_numeric_or_string(parts[2])
                            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and isinstance(step, (int, float)):
                                return {"start": start, "end": end, "step": step}
                        except ValueError:
                            pass

                    values = [parse_numeric_or_string(part) for part in parts]
                    return {"values": values}

                parsed = parse_numeric_or_string(raw)
                if isinstance(parsed, (int, float)):
                    end = parse_numeric_or_string(input("End value: "))
                    step = parse_numeric_or_string(input("Step: "))
                    return {"start": parsed, "end": end, "step": step}

                return {"values": [parsed]}

        def get_params_from_user():

            params = []

            print("\n=== Strategy Optimizer Setup ===")

            while True:

                name = input("\nEnter parameter name (or press Enter to finish): ")

                if name == "":
                    break

                values_spec = parse_param_values()
                param = {"name": name}
                param.update(values_spec)
                params.append(param)

            print("\nParameters configured:")
            for p in params:
                print(p)

            return params
        params = parse_params_arg(args.params) if args.params else get_params_from_user()

        run_grid_optimization(page, params)
        print("Optimization report generated. Closing Chrome.")
        context.close()
        return
        # open_last_strategy_settings(page)

        # set_strategy_input(page, "כמות נרות לאישור", 12)
        # apply_and_refresh_strategy(page)
        # מחכים שהתפריט יופיע
        # menu = page.locator("div[role='menu']:visible")
        # menu.wait_for(state="visible")

        # לחיצה על Sign in בתוך התפריט בלבד
        # menu.get_by_text("Sign in", exact=True).click()

        print("Clicked Sign In")
        print("Chrome opened on TradingView. You can log in manually if needed.")
        page.wait_for_timeout(60_0000)  # השאר חלון פתוח לדקה (שנה לפי צורך)

        context.close()

if __name__ == "__main__":
    main()
