"""D-Invoice workspace. Run: streamlit run streamlit_app.py

Requires Streamlit >= 1.37, pandas, requests. Start the existing FastAPI
backend with `python main.py serve`. Optional environment: INVOICE_API_URL.
Payments are saved through the existing backend API. Read caches are private
to each browser session. Lists are cached for two minutes; Refresh fetches fresh
data. Backend cold starts and extraction time require backend-side improvements.
"""
from __future__ import annotations

import math
import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
import time
from datetime import date, datetime
from urllib.parse import quote, urlparse

import pandas as pd
import requests
import streamlit as st

from requests.adapters import HTTPAdapter


def _make_http_session():
    """Create a requests.Session with connection pooling and retry for speed."""
    session = requests.Session()
    # Pool TCP connections: 1 host, up to 4 concurrent, reuse warm connections
    adapter = HTTPAdapter(
        pool_connections=1,
        pool_maxsize=4,
        max_retries=0,  # Never replay payment, approval, upload or delete requests.
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


st.set_page_config(page_title="D-Invoice • Workspace", page_icon="🧾", layout="wide", initial_sidebar_state="collapsed")

PAGES = ["Dashboard", "Invoices", "Vendors", "Upload & Process", "Tools"]
API_DEFAULT = os.getenv("INVOICE_API_URL", "http://localhost:8000").rstrip("/")
st.session_state.setdefault("api_base", API_DEFAULT)
st.session_state.setdefault("read_cache", {})
if "http_session" not in st.session_state:
    st.session_state.http_session = _make_http_session()
if "secondary_session" not in st.session_state:
    st.session_state.secondary_session = _make_http_session()
st.session_state.setdefault("read_failures", {})

# Keep native widget colours under Streamlit's active theme. Custom surfaces
# inherit its text colour, so theme changes work without a rerun or OS detection.
_CSS = """
<style>
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none; }
.block-container { max-width:1320px; padding-top:2rem; padding-bottom:3rem; }
h1,h2,h3 { letter-spacing:-.025em; }
h1 { font-size:2rem !important; font-weight:700 !important; }
h2,h3 { line-height:1.35; }
.brand { display:flex; align-items:center; gap:12px; margin-bottom:16px; color:inherit; }
.brand-icon { display:grid; place-items:center; width:44px; height:44px; border-radius:12px;
    background:color-mix(in srgb, currentColor 7%, transparent); font-size:24px; }
.brand-name { font-size:23px; font-weight:700; letter-spacing:-.7px; }
.hero { color:inherit; background:color-mix(in srgb, currentColor 4%, transparent);
    border:1px solid color-mix(in srgb, currentColor 14%, transparent);
    padding:24px 28px; border-radius:16px; margin:8px 0 20px; }
.hero h1 { color:inherit; margin:0; padding:0; }
div[data-testid="stMetric"] {
    border:1px solid color-mix(in srgb, currentColor 16%, transparent);
    background:color-mix(in srgb, currentColor 3%, transparent);
    border-radius:14px; padding:18px 20px; }
[data-testid="stMetricValue"] { font-size:1.8rem; font-weight:650; }
[data-testid="stVerticalBlockBorderWrapper"]>div { border-radius:14px; }
.stButton>button, .stDownloadButton>button, [data-testid="stFormSubmitButton"] button {
    border-radius:10px; min-height:42px; font-weight:600;
    transition:border-color .15s ease, box-shadow .15s ease; }
.stButton>button:focus-visible, .stDownloadButton>button:focus-visible,
[data-testid="stFormSubmitButton"] button:focus-visible {
    outline:2px solid currentColor; outline-offset:3px; }
[data-testid="stRadio"] [role="radiogroup"] { gap:8px; flex-wrap:wrap; }
[data-testid="stRadio"] [role="radiogroup"]>label {
    border:1px solid color-mix(in srgb, currentColor 18%, transparent);
    border-radius:10px; padding:8px 12px; margin:0; }
[data-testid="stRadio"] [role="radiogroup"]>label:has(input:checked) {
    background:color-mix(in srgb, currentColor 9%, transparent);
    border-color:currentColor; box-shadow:inset 0 -2px 0 currentColor; }
[data-testid="stRadio"] [role="radiogroup"]>label:has(input:focus-visible) {
    outline:2px solid currentColor; outline-offset:3px; }
[data-testid="stFileUploader"], [data-testid="stDataFrame"] { border-radius:12px; }
@media (max-width:700px) {
    .block-container { padding:1.5rem 1rem 2rem; }
    .hero { padding:20px; border-radius:12px; }
    h1 { font-size:1.65rem !important; }
    div[data-testid="stMetric"] { padding:14px; }
    [data-testid="stRadio"] [role="radiogroup"] { gap:6px; }
    [data-testid="stRadio"] [role="radiogroup"]>label { padding:7px 9px; }
}
@media (prefers-reduced-motion:reduce) {
    .stButton>button, .stDownloadButton>button,
    [data-testid="stFormSubmitButton"] button { transition:none; }
}
.brand-icon { background:#2563eb; color:white; font-size:22px; }
.brand-name { font-size:22px; }
.hero { border:0; background:transparent; padding:8px 0 4px; margin:0; }
[data-testid="stMetric"] { border-top:3px solid #3b82f6 !important; }
[data-testid="stMetricLabel"] { opacity:.7; font-size:.85rem; }
[data-testid="stRadio"] [role="radiogroup"]>label:has(input:checked) {
    background:#2563eb; border-color:#2563eb; color:white; box-shadow:none;
}
[data-testid="stRadio"] [role="radiogroup"]>label:has(input:checked) p { color:white; }
[data-testid="stDataFrame"] { border:1px solid #94a3b833; overflow:hidden; }
.stButton>button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {
    background:#2563eb; border-color:#2563eb; color:white;
}
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


def number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def format_inr(value):
    value = number(value)
    if value is None:
        return "—"
    # Indian grouping: 12,34,567.89. Keep zero and negative values meaningful.
    whole, decimals = f"{abs(value):.2f}".split(".")
    tail, head = whole[-3:], whole[:-3]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ("−" if value < 0 else "") + "₹" + ",".join(groups + [tail]) + "." + decimals


def confidence_bar(value):
    value = number(value)
    return "—" if value is None else f"{max(0, min(1, value)):.0%}"


def records(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def invalidate():
    st.session_state.read_cache.clear()
    st.session_state.read_failures.clear()
    st.session_state.pop("vendor_index", None)


def cache_ttl(path):
    if path.startswith(("/api/stats", "/api/health", "/api/performance", "/api/logs")):
        return 30
    return 120 if "?" in path else 30


def _fetch(session, base, path, method="GET", **kwargs):
    """Transport only: safe to run in a worker; no Streamlit state or UI."""
    try:
        response = session.request(method, f"{base}{path}",
                                   timeout=(3.05, 15 if method == "GET" else 90),
                                   allow_redirects=(method == "GET"), **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            return None, "The server redirected this action. Check the API URL in Tools.", False
        value = ({"ok": True} if method != "GET" else None) if not response.content else response.json()
        if value is None:
            return None, "The server returned an empty response. Try Refresh.", False
        return value, None, False
    except (requests.Timeout, requests.ConnectionError):
        message = ("The server is unavailable or taking too long. Try Refresh." if method == "GET" else
                   "The result is uncertain. Refresh and check the record before trying again.")
        return None, message, True
    except requests.HTTPError as exc:
        status = exc.response.status_code
        messages = {401: "Authentication required.", 403: "Access denied.",
                    404: "Record or endpoint not found.",
                    409: "This record changed. Refresh before trying again.",
                    413: "The file exceeds the upload limit.", 422: "Check the submitted data."}
        return None, messages.get(status, f"Server error ({status}). Try Refresh."), status >= 500 or status == 429
    except (ValueError, requests.RequestException):
        return None, "Invalid server response. Refresh and check the result before retrying an action.", False


def _cached(path):
    key = (st.session_state.api_base, path)
    cached = st.session_state.read_cache.get(key)
    failure = st.session_state.read_failures.get(key)
    now = time.monotonic()
    if failure and now - failure[0] < 15:
        # Short cooldown prevents every widget interaction hammering a failing API.
        if failure[2] and cached and now - cached[0] < 900:
            return cached[1], None
        return None, failure[1]
    if cached and now - cached[0] < cache_ttl(path):
        return cached[1], None
    return None


def _accept(path, result):
    value, error, transient = result
    key = (st.session_state.api_base, path)
    cache = st.session_state.read_cache
    if error:
        failures = st.session_state.read_failures
        if len(failures) >= 128:
            failures.pop(next(iter(failures)))
        failures[key] = (time.monotonic(), error, transient)
        cached = cache.get(key)
        if transient and cached and time.monotonic() - cached[0] < 900:
            return cached[1], None
        return None, error
    st.session_state.read_failures.pop(key, None)
    if len(cache) >= 128:
        cache.pop(next(iter(cache)))
    cache[key] = (time.monotonic(), value)
    return value, None


def request_api(path, method="GET", **kwargs):
    method = method.upper()
    # GET paths contain their own query parameters; callers do not pass params.
    if method == "GET":
        cached = _cached(path)
        if cached is not None:
            return cached
    result = _fetch(st.session_state.http_session, st.session_state.api_base, path, method, **kwargs)
    if method == "GET":
        return _accept(path, result)
    # Invalidate on both success and uncertain outcomes; never replay writes.
    invalidate()
    return result[0], result[1]


def prefetch(paths):
    """At most two independent reads in parallel, with separate pooled sessions."""
    pending = [path for path in paths if _cached(path) is None]
    if not pending:
        return
    sessions = [st.session_state.http_session, st.session_state.secondary_session]
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Only the dashboard's two reads are scheduled here.
        jobs = [(path, pool.submit(_fetch, session, st.session_state.api_base, path))
                for path, session in zip(pending, sessions)]
        for path, future in jobs:
            _accept(path, future.result())


def load(path, expected=dict):
    value, error = request_api(path)
    if error:
        st.error(error)
        return None
    if not isinstance(value, expected):
        st.error("Unexpected server data. Try Refresh.")
        return None
    key = (st.session_state.api_base, path)
    if key in st.session_state.read_failures:
        st.warning("Showing previously loaded data. The server is temporarily unavailable; refresh before making changes.")
    return value


def mutation(path, method="POST", **kwargs):
    value, error = request_api(path, method, **kwargs)
    if error:
        st.error(error)
        return False
    if not isinstance(value, dict) or not value.get("ok"):
        st.warning(str(value.get("message", "The server did not confirm this action.")) if isinstance(value, dict) else "The server did not confirm this action.")
        return False
    return True


def flash(message):
    st.session_state.flash = message
    st.rerun()


def title(name):
    st.title(name)


def invoice_table(rows):
    data = records(rows)
    if not data:
        st.info("No invoices yet.")
        return
    df = pd.DataFrame(data)
    columns = {"id": "ID", "company_name": "Company", "vendor_name": "Vendor", "invoice_number": "Invoice", "invoice_date": "Date", "bill_amount": "Amount", "paid_amount": "Paid", "pending_amount": "Pending", "status": "Status"}
    view = df[[c for c in columns if c in df]].copy()
    if "bill_amount" in view:
        view["bill_amount"] = view["bill_amount"].map(format_inr)
    if "paid_amount" in view:
        view["paid_amount"] = view["paid_amount"].map(format_inr)
    if "pending_amount" in view:
        view["pending_amount"] = view["pending_amount"].map(format_inr)
    if "confidence" in view:
        view["confidence"] = view["confidence"].map(confidence_bar)
    if "status" in view:
        view["status"] = view["status"].fillna("Unknown").astype(str).str.replace("_", " ", regex=False).str.title()
    st.dataframe(view.rename(columns=columns), use_container_width=True, hide_index=True)


def metrics(stats):
    cols = st.columns(4)
    for col, label, key in zip(cols[:3], ["Invoices", "Needs review", "Quarantined"], ["stored", "needs_review", "quarantined"]):
        col.metric(label, stats.get(key) or 0)
    cols[3].metric("Invoice value", format_inr(stats.get("total_value", 0)))


def go(page):
    st.session_state.navigation = page
    st.query_params["page"] = page


def vendor_key(name):
    return " ".join(str(name or "").split()).casefold()


def payment_read(backend, company):
    path = f"/api/companys/{quote(company, safe='')}/balance"
    value, error = request_api(path)
    if error or value is None:
        return {"paid": None, "pending": None, "version": 0, "updated_at": None}
    return value


def paise(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < Decimal("-999999999999.99") or amount > Decimal("999999999999.99"):
            raise ValueError("Enter an amount between -₹9,99,99,99,99,999.99 and ₹9,99,99,99,99,999.99.")
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter valid amounts in both payment boxes.") from None


def payment_save(backend, company, paid, pending, version):
    paid, pending = paise(paid), paise(pending)
    path = f"/api/companys/{quote(company, safe='')}/balance"
    value, error = request_api(path, "POST", json={"paid": paid, "pending": pending, "version": version})
    if error:
        if "changed in another session" in str(error):
            raise ValueError("These balances changed in another session. Reload saved balances before saving again.")
        raise OSError(error)


def vendor_invoices():
    """Fetch all invoices for the company workspace with a single efficient request.

    Uses a generous initial limit to avoid multiple round-trips.  A capped
    response is surfaced explicitly; never label a knowingly incomplete list
    as complete.  The server must provide pagination to go beyond its own
    hard result limit.  Results are cached for 120 s because company index
    data changes infrequently (only on new invoice processing).
    """
    cache_key = st.session_state.api_base
    cached = st.session_state.get("vendor_index")
    if cached and cached[0] == cache_key and time.monotonic() - cached[1] < 120:
        return cached[2], cached[3]
    warning = "Company totals cover only the invoices returned by the server."
    # Single request with a generous limit covers most workloads.
    data, error = request_api(f"/api/invoices?limit=2000")
    if error or not isinstance(data, list):
        st.error(error or "The invoice server returned an unexpected data format.")
        return None, None
    rows = records(data)
    # Only escalate once if we hit the limit — avoids the old 4-step waterfall.
    if len(data) >= 2000:
        bigger, err2 = request_api(f"/api/invoices?limit=10000")
        if not err2 and isinstance(bigger, list):
            bigger_rows = records(bigger)
            if len(bigger_rows) > len(rows):
                rows = bigger_rows
            if len(bigger) >= 10000 or len(bigger_rows) <= len(records(data)):
                warning = "The server capped this list. Company totals may exclude older invoices."
        else:
            warning = "The server limits invoice results. Older invoices may be missing from this company view."
    st.session_state.vendor_index = (cache_key, time.monotonic(), rows, warning)
    return rows, warning


@st.fragment
def vendor_workspace():
    with st.container(border=True):
        st.subheader("Vendor account")
        with st.form("vendor_search_form"):
            cols = st.columns([4, 1])
            query = cols[0].text_input("Vendor account", key="vendor_name_input", placeholder="Enter vendor account, e.g. Nency Fashion")
            submitted = cols[1].form_submit_button("Find vendor", type="primary", use_container_width=True)
        if submitted:
            st.session_state.vendor_search = query.strip()
            st.session_state.pop("vendor_match", None)
            st.rerun()
        search = st.session_state.get("vendor_search", "")
        if not search:
            return
        if st.button("Close vendor view"):
            st.session_state.pop("vendor_search", None)
            st.rerun()
        data, warning = vendor_invoices()
        if data is None:
            return
        if warning:
            st.warning(warning)
        names = {}
        for row in data:
            name = str(row.get("vendor_name") or "").strip()
            if name:
                names.setdefault(vendor_key(name), name)
        matches = sorted((key for key in names if vendor_key(search) in key), key=lambda key: (key != vendor_key(search), key))
        if not matches:
            st.info("No matching vendor found in the invoices returned by the server. Check the name or refresh after processing new invoices.")
            return
        if st.session_state.get("vendor_match") not in matches:
            st.session_state.pop("vendor_match", None)
        selected = st.selectbox("Select company", matches, format_func=lambda key: names[key], key="vendor_match")
        vendor_rows = [row for row in data if vendor_key(row.get("company_name")) == selected]
        st.markdown("#### " + names[selected])
        amounts = [number(row.get("bill_amount")) for row in vendor_rows]
        cols = st.columns(4)
        cols[0].metric("Company invoices", len(vendor_rows))
        
        invoice_value = sum(amount for amount in amounts if amount is not None)
        paid_value = sum((number(row.get("paid_amount")) or 0.0) for row in vendor_rows if number(row.get("bill_amount")) is not None)
        pending_value = sum((number(row.get("pending_amount")) or 0.0) for row in vendor_rows if number(row.get("bill_amount")) is not None)
        
        cols[1].metric("Total invoice value", format_inr(invoice_value))
        cols[2].metric("Total payment paid", format_inr(paid_value))
        cols[3].metric("Total pending payment", format_inr(pending_value))
        
        if any(amount is None for amount in amounts):
            st.caption("Invoices with missing or invalid amounts are excluded from these totals.")
            
        st.markdown("#### Invoices for this vendor account")
        invoice_table(vendor_rows)
        choices = {str(row["id"]): row for row in vendor_rows if row.get("id") is not None}
        if choices:
            key = "vendor_invoice_" + hashlib.sha256((st.session_state.api_base + selected).encode()).hexdigest()[:16]
            options = [None] + list(choices)
            if st.session_state.get(key) not in options:
                st.session_state.pop(key, None)
            chosen = st.selectbox("Open a vendor invoice", options, key=key, format_func=lambda value: "Select an invoice…" if value is None else f"#{value} · {choices[value].get('invoice_number') or 'No number'}")
            if chosen is not None:
                invoice_detail(chosen)


def dashboard():
    heading, upload = st.columns([4, 1])
    heading.title("Overview")
    upload.button("＋ New invoice", on_click=go, args=("Upload & Process",), type="primary", use_container_width=True)
    with st.spinner("Loading overview…"):
        prefetch(["/api/stats", "/api/invoices?limit=10"])
    stats = load("/api/stats")
    if stats is not None:
        metrics(stats)
        if stats.get("processing"):
            st.info("Processing invoices. Refresh to see new results.")
    st.subheader("Recent invoices")
    invoices = load("/api/invoices?limit=10", list)
    if invoices is not None:
        invoice_table(invoices)
    left, right = st.columns(2)
    left.button("View all invoices →", on_click=go, args=("Invoices",), use_container_width=True)
    right.button("Vendor accounts →", on_click=go, args=("Vendors",), use_container_width=True)


def invoice_payment_editor(inv):
    st.markdown("#### Payments")
    inv_id = str(inv["id"])
    token = hashlib.sha256((st.session_state.api_base + inv_id).encode()).hexdigest()[:16]
    current_paid = number(inv.get("paid_amount")) or 0.0
    bill_amount = number(inv.get("bill_amount"))
    cols = st.columns(2)
    cols[0].metric("Paid", format_inr(current_paid))
    cols[1].metric("Pending", format_inr(inv.get("pending_amount")))
    path = f"/api/invoices/{quote(inv_id, safe='')}"
    unavailable = (st.session_state.api_base, path) in st.session_state.read_failures
    generation = st.session_state.get("payment_generation", 0)
    with st.form(f"payment_{token}_{generation}"):
        amount = st.number_input("Payment amount (₹)", min_value=0.01, max_value=999999999999.99,
                                 value=None, step=100.0, format="%.2f", placeholder="Enter amount")
        submitted = st.form_submit_button("Save payment", type="primary", disabled=unavailable or bill_amount is None)
    if submitted:
        if amount is None:
            st.warning("Enter a payment amount.")
            return
        # Force a fresh read before read-modify-write, avoiding stale cached balances.
        st.session_state.read_cache.pop((st.session_state.api_base, path), None)
        st.session_state.read_failures.pop((st.session_state.api_base, path), None)
        latest, error = request_api(path)
        if error or not isinstance(latest, dict):
            st.error(error or "Could not verify the current balance.")
            return
        latest_bill = number(latest.get("bill_amount"))
        latest_paid = number(latest.get("paid_amount"))
        if latest_bill is None or latest_paid is None:
            st.error("The invoice balance is incomplete. Refresh before saving a payment.")
            return
        if latest_paid != current_paid or latest_bill != bill_amount:
            st.warning("The balance changed. Refresh and review it before saving.")
            return
        # Decimal arithmetic prevents cumulative binary floating-point rounding errors.
        paid = paise(latest_paid) + paise(amount)
        pending = paise(latest_bill) - paid
        if pending < 0:
            st.warning("Payment exceeds the pending amount.")
            return
        value, error = request_api(path + "/payment", "POST", json={"paid": paid / 100, "pending": pending / 100})
        if error or not isinstance(value, dict) or value.get("ok") is False:
            st.error(error or "Payment was not confirmed. Refresh and check before retrying.")
            return
        st.session_state.payment_generation = generation + 1
        flash("Payment saved.")


def invoice_detail(inv_id):
    path = f"/api/invoices/{quote(str(inv_id), safe='')}"
    inv = load(path)
    if inv is None:
        return
    with st.container(border=True):
        st.subheader(f"Invoice {inv.get('invoice_number') or inv_id}")
        cols = st.columns(3)
        cols[0].write(f"**Company:** {inv.get('company_name') or '—'}")
        cols[1].write(f"**Invoice date:** {inv.get('invoice_date') or '—'}")
        cols[2].write(f"**Status:** {str(inv.get('status') or 'Unknown').replace('_', ' ').title()}")
        cols = st.columns(4)
        for col, label, key in zip(cols, ["Receiver", "State", "Broker"], ["receiver_details", "state", "broker"]):
            col.write(f"**{label}:** {inv.get(key) or '—'}")
        age = "—"
        try:
            age = f"{(date.today() - date.fromisoformat(str(inv.get('invoice_date'))[:10])).days} days"
        except (ValueError, TypeError):
            pass
        cols[3].write(f"**Invoice age:** {age}")
        cols = st.columns(3)
        cols[0].metric("Bill amount", format_inr(inv.get("bill_amount")))
        cols[1].metric("Average sale rate", format_inr(inv.get("sale_avg_rate")))
        cols[2].metric("Confidence", confidence_bar(inv.get("confidence")))
        st.write(f"**E-way bill:** {inv.get('e_way_bill_number') or '—'} · **Challan No:** {inv.get('challan_no') or '—'} · **Source:** {inv.get('source_file') or '—'}")
        
        invoice_payment_editor(inv)
        
        items = records(inv.get("line_items"))
        st.markdown("#### Line items")
        if items:
            df = pd.DataFrame(items)
            for col in ("rate", "amount"):
                if col in df:
                    df[col] = df[col].map(format_inr)
            st.dataframe(df, use_container_width=True, hide_index=True)
            amounts = [number(item.get("amount")) for item in items]
            st.write(f"**Known line-item total:** {format_inr(sum(x for x in amounts if x is not None))}")
            if any(x is None for x in amounts):
                st.caption("Some line-item amounts are missing or invalid and are excluded from this total.")
        else:
            st.info("No line items available for this invoice.")
        if inv.get("status") == "needs_review":
            if st.button("Approve invoice", type="primary", key=f"approve_{inv_id}"):
                if mutation(f"{path}/approve"):
                    flash("Invoice approved.")
        with st.expander("Delete this invoice"):
            confirmed = st.checkbox("I understand this permanently deletes the invoice.", key=f"confirm_{inv_id}")
            if st.button("Delete invoice", disabled=not confirmed, key=f"delete_{inv_id}"):
                if mutation(path, "DELETE"):
                    st.session_state.pop("invoice_choice", None)
                    flash("Invoice deleted.")


@st.fragment
def invoices_page():
    title("Invoices")
    with st.form("invoice_filters"):
        cols = st.columns([2, 1, 1])
        search = cols[0].text_input("Search", placeholder="Company, invoice number or source file")
        status = cols[1].selectbox("Status", ["All", "stored", "needs_review"])
        limit = cols[2].selectbox("Recent records", [50, 100, 200, 500], index=0)
        st.form_submit_button("Apply filters", type="primary")
    path = f"/api/invoices?limit={limit}" + (f"&status={status}" if status != "All" else "")
    data = load(path, list)
    if data is None:
        return
    rows = records(data)
    if search.strip():
        needle = search.strip().casefold()
        rows = [row for row in rows if any(needle in str(row.get(k) or "").casefold() for k in ("vendor_name", "invoice_number", "source_file", "id"))]
    st.caption(f"{len(rows)} matches · Latest {limit} records in selected status")
    if not rows:
        st.info("No matching invoices. Adjust the filters or upload files.")
        return
    invoice_table(rows)
    choices = {str(row["id"]): row for row in rows if row.get("id") is not None}
    if choices:
        options = [None] + list(choices)
        if st.session_state.get("invoice_choice") not in options:
            st.session_state.pop("invoice_choice", None)
        selected = st.selectbox("Open invoice details", options, key="invoice_choice", format_func=lambda x: "Select an invoice…" if x is None else f"#{x} · {choices[x].get('company_name') or 'Unknown company'} · {choices[x].get('invoice_number') or 'No number'}")
        if selected is not None:
            invoice_detail(selected)


def uploads_page():
    title("Upload & Process")
    st.session_state.setdefault("upload_generation", 0)
    with st.container(border=True):
        st.subheader("Add invoice files")
        files = st.file_uploader("Choose files or drag them here", type=["txt", "pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "md"], accept_multiple_files=True, key=f"files_{st.session_state.upload_generation}")
        immediate = st.checkbox("Start processing after successful uploads", value=True)
        if st.button("Upload files", type="primary", disabled=not files, use_container_width=True):
            succeeded, failed = [], []
            progress = st.progress(0, text="Uploading files…")
            for i, file in enumerate(files):
                # Upload first, trigger once afterwards—even if the final upload fails.
                if file.size == 0:
                    failed.append(file.name)
                    st.warning(f"{file.name}: this file is empty.")
                else:
                    result, error = request_api("/api/upload", "POST", files={"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}, data={"process": "false"})
                    if error or (isinstance(result, dict) and result.get("ok") is False):
                        failed.append(file.name)
                        st.error(f"{file.name}: {error or 'The server rejected this upload.'}")
                    else:
                        succeeded.append(file.name)
                progress.progress((i + 1) / len(files), text=f"Uploaded {i + 1} of {len(files)} files")
            progress.empty()
            if succeeded:
                st.success(f"Uploaded {len(succeeded)} file(s).")
                st.session_state.upload_generation += 1
                if immediate:
                    if mutation("/api/process"):
                        st.success("Processing started. Open Dashboard to track progress.")
            if failed:
                st.warning("Check failed or uncertain uploads in the backend before retrying: " + ", ".join(failed))
    st.subheader("Process inbox")
    st.write("Includes all pending files and earlier uploads.")
    if st.button("Start processing inbox", key="process_inbox"):
        if mutation("/api/process"):
            st.success("Processing started. Open Dashboard to follow the results.")


def quarantine_page():
    title("Quarantine")
    data = load("/api/quarantine", list)
    if data is None:
        return
    rows = records(data)
    if not rows:
        st.success("No quarantined files.")
        return
    st.caption(f"{len(rows)} files need attention")
    for row in rows:
        with st.expander(str(row.get("file") or "Unknown file")):
            st.warning(str(row.get("violations") or "No reason provided by the backend."))
            extracted = row.get("extracted")
            if isinstance(extracted, dict):
                st.write(f"**Company:** {extracted.get('company_name') or '—'}")
                st.write(f"**Invoice:** {extracted.get('invoice_number') or '—'}")
                st.write(f"**Amount:** {format_inr(extracted.get('bill_amount'))}")
                st.json(extracted)


def log_table(data):
    rows = records(data)
    if not rows:
        st.info("No activity recorded yet.")
        return
    df = pd.DataFrame(rows)
    columns = {"stage": "Stage", "source_file": "File", "outcome": "Outcome", "detail": "Detail", "created_at": "Timestamp"}
    available = [c for c in columns if c in df]
    st.dataframe(df[available].rename(columns=columns), use_container_width=True, hide_index=True)


def activity_page():
    title("Activity Log")
    limit = st.selectbox("Recent events", [25, 50, 100], index=1)
    data = load(f"/api/logs?limit={limit}", list)
    if data is None:
        return
    rows = records(data)
    cols = st.columns(4)
    for col, outcome in zip(cols, ["success", "error", "quarantined", "skipped"]):
        col.metric(outcome.title(), sum(row.get("outcome") == outcome for row in rows))
    log_table(rows)


def timestamp_label(value):
    try:
        if number(value) is not None:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        return str(value or "—")
    except (ValueError, OverflowError, OSError):
        return "—"


def performance_page():
    title("Performance")
    perf = load("/api/performance")
    if perf is None:
        return
    cols = st.columns(4)
    cols[0].metric("Total requests", perf.get("total_requests") or 0)
    cols[1].metric("Average response", f"{number(perf.get('avg_response_time_ms'), 0):.1f} ms")
    cols[2].metric("P95 response", f"{number(perf.get('p95_response_time_ms'), 0):.1f} ms")
    uptime = number(perf.get("uptime_seconds"), 0)
    cols[3].metric("Uptime", f"{uptime/3600:.1f} h" if uptime >= 3600 else f"{uptime/60:.1f} min" if uptime >= 60 else f"{uptime:.0f} s")
    st.subheader("Endpoint performance")
    endpoints = records(perf.get("endpoints"))
    if endpoints:
        df = pd.DataFrame(endpoints)
        st.dataframe(df, use_container_width=True, hide_index=True)
        if {"endpoint", "avg_ms"}.issubset(df.columns):
            chart = df[["endpoint", "avg_ms"]].copy()
            chart["avg_ms"] = pd.to_numeric(chart["avg_ms"], errors="coerce")
            chart = chart.dropna()
            if not chart.empty:
                st.bar_chart(chart.set_index("endpoint"))
    else:
        st.info("Endpoint metrics will appear after requests are recorded.")
    st.subheader("Recent requests")
    recent = records(perf.get("recent"))
    if recent:
        df = pd.DataFrame(recent)
        if "timestamp" in df:
            df["timestamp"] = df["timestamp"].map(timestamp_label)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption("Numeric timestamps are shown in the Streamlit server's local timezone.")
    else:
        st.info("No recent requests recorded.")
    cols = st.columns(2)
    cols[0].metric("Fastest response", f"{number(perf.get('min_response_time_ms'), 0):.2f} ms")
    cols[1].metric("Slowest response", f"{number(perf.get('max_response_time_ms'), 0):.2f} ms")


def settings_page():
    title("Settings")
    with st.form("connection_settings"):
        base = st.text_input("Backend API URL", value=st.session_state.api_base, help="localhost refers to the computer running Streamlit.")
        if st.form_submit_button("Save connection", type="primary"):
            parsed = urlparse(base.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
                st.error("Enter a valid HTTP or HTTPS base URL without credentials, query parameters or fragments.")
            else:
                st.session_state.api_base = base.strip().rstrip("/")
                invalidate()
                st.session_state.secondary_session.close()
                st.session_state.secondary_session = _make_http_session()
                st.session_state.http_session.close()
                st.session_state.http_session = _make_http_session()
                st.session_state.pop("invoice_choice", None)
                st.success("Connection saved.")
    st.subheader("System status")
    if not st.button("Check connection"):
        return
    health = load("/api/health")
    if health is not None:
        st.success("Connected to the invoice backend")
        cols = st.columns(3)
        cols[0].metric("AI extraction", "Ready" if health.get("openrouter") else "Unavailable")
        cols[1].metric("OCR", "Ready" if health.get("ocr") else "Unavailable")
        cols[2].metric("Pipeline", "Processing" if health.get("processing") else "Idle")
        st.caption(f"Model: {health.get('model') or 'Not specified'}")


def vendors_page():
    title("Vendors")
    vendor_workspace()


def tools_page():
    title("Tools")
    section = st.selectbox("Open tool", ["Backup", "Quarantine", "Activity Log", "Performance", "Settings"], label_visibility="collapsed")
    if section == "Backup":
        st.link_button("Download Excel backup", f"{st.session_state.api_base}/api/backup/download", use_container_width=True)
    else:
        {"Quarantine": quarantine_page, "Activity Log": activity_page,
         "Performance": performance_page, "Settings": settings_page}[section]()


st.markdown('<div class="brand"><div class="brand-icon">▤</div><div class="brand-name">D-Invoice</div></div>', unsafe_allow_html=True)
if "navigation" not in st.session_state or st.session_state.navigation not in PAGES:
    requested = st.query_params.get("page", "Dashboard")
    st.session_state.navigation = requested if requested in PAGES else "Dashboard"
nav, refresh = st.columns([6, 1])
with nav:
    page = st.radio("Navigation", PAGES, horizontal=True, key="navigation", label_visibility="collapsed")
st.query_params["page"] = page
if refresh.button("↻ Refresh", use_container_width=True):
    invalidate()
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

# No automatic polling: idle pages make zero API calls. Fragment interactions
# in the invoice/company workspaces do not rerender the surrounding dashboard.
{"Dashboard": dashboard, "Invoices": invoices_page, "Vendors": vendors_page,
 "Upload & Process": uploads_page, "Tools": tools_page}[page]()
