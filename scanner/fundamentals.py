import bs4
import requests

from resilience import call_with_resilience


FUNDAMENTAL_FIELDS = ("pe_ratio", "pb_ratio", "roce", "roe", "de_ratio")


def _latest_balance_sheet_value(soup, row_name: str):
    balance_sheet = soup.find("section", id="balance-sheet")
    if not balance_sheet:
        return None

    for row in balance_sheet.select("table.data-table tr"):
        cells = row.find_all("td")
        if not cells or not cells[0].get_text(" ", strip=True).startswith(row_name):
            continue
        if len(cells) < 2:
            return None
        value = cells[-1].get_text(strip=True).replace(",", "")
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _debt_to_equity_from_balance_sheet(soup):
    borrowings = _latest_balance_sheet_value(soup, "Borrowings")
    equity_capital = _latest_balance_sheet_value(soup, "Equity Capital")
    reserves = _latest_balance_sheet_value(soup, "Reserves")
    if borrowings is None or equity_capital is None or reserves is None:
        return None

    shareholder_equity = equity_capital + reserves
    if shareholder_equity <= 0:
        return None
    return round(borrowings / shareholder_equity, 2)


def fetch_screener_data(symbol: str) -> dict:
    """Scrapes financial metrics directly from Screener.in without login requirements."""
    clean_symbol = symbol.strip().upper()

    # Define User-Agent to mimic standard browser request
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # Screener URLs (tries consolidated first, falls back to standalone)
    urls = [
        f"https://www.screener.in/company/{clean_symbol}/consolidated/",
        f"https://www.screener.in/company/{clean_symbol}/",
    ]

    response = None
    for url in urls:
        def request_screener():
            result = requests.get(url, headers=headers, timeout=10)
            if result.status_code != 404:
                result.raise_for_status()
            return result

        res = call_with_resilience("Screener", request_screener)
        if res.status_code == 200:
            response = res
            break

    if not response or response.status_code != 200:
        raise ValueError(
            f"Could not find ticker symbol '{clean_symbol}' on Screener.in"
        )

    soup = bs4.BeautifulSoup(response.text, "html.parser")

    # Extract Company Name
    name_tag = soup.find("h1")
    company_name = name_tag.text.strip() if name_tag else clean_symbol

    # Dictionary to hold raw scraped values
    raw_ratios = {}

    # Top ratios grid parsing (<ul id="top-ratios">)
    top_ratios_ul = soup.find("ul", id="top-ratios")
    if top_ratios_ul:
        for li in top_ratios_ul.find_all("li"):
            name_span = li.find("span", class_="name")
            number_span = li.find("span", class_="number")
            if name_span and number_span:
                key = name_span.text.strip()
                val_text = (
                    number_span.text.strip().replace(",", "").replace("%", "")
                )
                try:
                    raw_ratios[key] = float(val_text)
                except ValueError:
                    raw_ratios[key] = None

    classification = {}
    for link in soup.select("#peers p.sub a[title]"):
        title = link.get("title")
        if title in {"Broad Sector", "Sector", "Broad Industry", "Industry"}:
            classification[title] = link.get_text(strip=True)

    de_ratio = raw_ratios.get("Debt to equity")
    de_ratio_source = "quick_ratio"
    if de_ratio is None:
        de_ratio = _debt_to_equity_from_balance_sheet(soup)
        de_ratio_source = "balance_sheet" if de_ratio is not None else None

    # Extract ratios from "Key Points" or text sections if available
    metrics = {
        "symbol": clean_symbol,
        "name": company_name,
        "broad_sector": classification.get("Broad Sector"),
        "sector": classification.get("Sector"),
        "broad_industry": classification.get("Broad Industry"),
        "industry": classification.get("Industry"),
        "market_cap": raw_ratios.get("Market Cap"),
        "current_price": raw_ratios.get("Current Price"),
        "pe_ratio": raw_ratios.get("Stock P/E"),
        "book_value": raw_ratios.get("Book Value"),
        "dividend_yield": raw_ratios.get("Dividend Yield"),
        "roce": raw_ratios.get("ROCE"),
        "roe": raw_ratios.get("ROE"),
        "face_value": raw_ratios.get("Face Value"),
        "de_ratio": de_ratio,
        "de_ratio_source": de_ratio_source,
    }

    # Calculate Price-to-Book Ratio manually if missing
    if (
        metrics["book_value"]
        and metrics["current_price"]
        and metrics["book_value"] > 0
    ):
        metrics["pb_ratio"] = round(
            metrics["current_price"] / metrics["book_value"], 2
        )
    else:
        metrics["pb_ratio"] = None

    return metrics


def calculate_fundamental_score(metrics: dict) -> dict:
    """Return a completeness-aware score using financial or industrial rules."""
    available_fields = [
        field for field in FUNDAMENTAL_FIELDS if metrics.get(field) is not None
    ]
    is_financial = metrics.get("broad_sector") == "Financial Services"
    scoring_fields = ("pe_ratio", "pb_ratio", "roe") if is_financial else FUNDAMENTAL_FIELDS
    available_scoring_fields = [
        field for field in scoring_fields if metrics.get(field) is not None
    ]
    minimum_fields = 2 if is_financial else 3
    profile = "financial_services" if is_financial else "industrial"
    completeness = {
        "available": len(available_fields),
        "total": len(FUNDAMENTAL_FIELDS),
        "scoring_available": len(available_scoring_fields),
        "scoring_total": len(scoring_fields),
        "minimum_required": minimum_fields,
    }
    if len(available_scoring_fields) < minimum_fields:
        return {
            "score": None,
            "tags": ["Insufficient fundamental data"],
            "completeness": completeness,
            "profile": profile,
        }

    score = 5.0  # Neutral baseline
    tags = []

    # 1. Valuation Checks
    pe = metrics.get("pe_ratio")
    pb = metrics.get("pb_ratio")

    if is_financial:
        if pe is not None:
            if pe < 15:
                score += 0.75
                tags.append("Moderate financial-sector earnings multiple")
            elif pe > 30:
                score -= 0.75
                tags.append("Expensive financial-sector earnings multiple")
        if pb is not None:
            if pb < 2:
                score += 1.0
                tags.append("Moderate financial-sector book multiple")
            elif pb > 5:
                score -= 1.0
                tags.append("Expensive financial-sector book multiple")
    elif pe is not None and pb is not None:
        if pe < 15 and pb < 1.5:
            score += 1.25
            tags.append("Deep Value / Fair Multiples")
        elif pe > 45 or pb > 12:
            score -= 1.0
            tags.append("Expensive Multiples")

    # 2. Capital Efficiency (ROCE & ROE)
    roce = metrics.get("roce")
    roe = metrics.get("roe")

    if not is_financial and roce is not None:
        if roce >= 20:
            score += 1.25
            tags.append("Exceptional ROCE (>20%)")
        elif roce >= 12:
            score += 0.5
        elif roce < 8:
            score -= 1.0
            tags.append("Poor Capital Efficiency")

    if roe is not None:
        if roe >= 18:
            score += 0.75
            tags.append("Strong ROE (>18%)")
        elif is_financial and roe < 10:
            score -= 0.75
            tags.append("Weak financial-sector ROE (<10%)")

    # 3. Debt & Financial Solvency
    de = metrics.get("de_ratio")
    if not is_financial and de is not None:
        if de < 0.1:
            score += 1.5
            tags.append("Virtually Debt-Free")
        elif de < 0.5:
            score += 0.75
            tags.append("Conservative Debt")
        elif de > 1.5:
            score -= 1.5
            tags.append("High Leverage Risk")

    # 4. Dividend Yield Bonus
    div_yield = metrics.get("dividend_yield")
    if div_yield is not None and div_yield >= 2.5:
        score += 0.5
        tags.append("Healthy Dividend Yield")

    # Clamp Score between 1.0 and 10.0
    final_score = round(max(1.0, min(10.0, score)), 1)
    return {
        "score": final_score,
        "tags": tags,
        "completeness": completeness,
        "profile": profile,
    }


def analyze_indian_stock(symbol: str):
    """Executes scraping, evaluation, and displays formatted classification report."""
    print(f"\nFetching live Screener.in fundamentals for '{symbol}'...")

    try:
        data = fetch_screener_data(symbol)
        result = calculate_fundamental_score(data)
        score = result["score"]
        tags = result["tags"]

        print("\n" + "=" * 55)
        print(f" SCREENER.IN ANALYSIS: {data['name']} ({data['symbol']})")
        print("=" * 55)

        print("\n--- Scraped Ratios ---")
        print(
            f"  Market Cap       : ₹{data['market_cap']} Cr"
            if data["market_cap"]
            else "  Market Cap       : N/A"
        )
        print(f"  Stock P/E        : {data['pe_ratio']}")
        print(f"  P/B Ratio        : {data['pb_ratio']}")
        print(
            f"  ROCE             : {data['roce']}%"
            if data["roce"]
            else "  ROCE             : N/A"
        )
        print(
            f"  ROE              : {data['roe']}%"
            if data["roe"]
            else "  ROE              : N/A"
        )
        print(
            f"  Debt to Equity   : {data['de_ratio']}"
            if data["de_ratio"] is not None
            else "  Debt to Equity   : N/A"
        )
        print(
            f"  Dividend Yield   : {data['dividend_yield']}%"
            if data["dividend_yield"]
            else "  Dividend Yield   : N/A"
        )

        print("\n--- Fundamental Classifications ---")
        if tags:
            for tag in tags:
                print(f"  • {tag}")
        else:
            print("  • Moderate / Mixed Fundamentals")

        print("\n--- Confidence Rating ---")
        completeness = result["completeness"]
        print(
            f"  DATA COMPLETENESS: {completeness['available']} / "
            f"{completeness['total']} metrics"
        )
        print(f"  SCORING PROFILE  : {result['profile']}")
        print(f"  CONFIDENCE SCORE : {score} / 10" if score is not None else "  CONFIDENCE SCORE : N/A")

        if score is None:
            assessment = "UNAVAILABLE (Insufficient fundamental data)"
        elif score >= 8.0:
            assessment = "HIGH CONFIDENCE (Strong Balance Sheet & Returns)"
        elif score >= 5.5:
            assessment = "MODERATE CONFIDENCE (Neutral / Fair Fundamentals)"
        else:
            assessment = "WEAK / HIGH RISK (Exercise Caution)"

        print(f"  ASSESSMENT       : {assessment}")
        print("=" * 55 + "\n")

    except Exception as e:
        print(f"Execution Error: {e}")


# --- Test Run ---
if __name__ == "__main__":
    # Input any NSE ticker symbol directly (e.g., HINDCOPPER, RELIANCE, TCS, INFYS)
    analyze_indian_stock("HINDCOPPER")