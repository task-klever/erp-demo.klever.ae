"""Scheduled nightly product sync from Magento's custom kleverErpProducts
GraphQL endpoint.

Replaces the earlier version of this module, which used the standard
storefront `products` query (enabled/visible products only, and could never
return `cost`). kleverErpProducts is a purpose-built endpoint the Magento
team wrote specifically for this ERP feed - it returns every catalog
product, enabled and disabled, with fields already using ERPNext's own
field names.

Runs in-process inside the Frappe scheduler (see hooks.py), so it calls
klever_integrations.api.import_item directly as a Python function instead of
over HTTP - no API key/secret needed for the ERPNext side (only the
Magento-side X-Klever-Api-Key, read from site_config.json).

Two things this must NOT do naively, both confirmed by testing:
1. Use `item.item_code` as ERPNext's item_code. Magento has a separate
   "Item Code" attribute that is NOT unique and NOT the same as sku
   (confirmed with the Magento dev - it can be duplicated across products),
   so it must never become ERPNext's item_code (the actual unique primary
   key). ERPNext's item_code always comes from the top-level `sku` field.
   Magento's own item_code attribute is preserved separately in
   custom_magento_item_code, purely for transparency.
2. Send `null` values through as real field values - a field absent from
   Magento means "no value", not "clear this field" - so null/None fields
   are dropped before calling import_item.
"""

import time

import frappe
import requests

MAGENTO_URL = "https://www.tyrescart.ae/graphql"
MAGENTO_STORE = "en"
PAGE_SIZE = 200
REQUEST_DELAY = 0.15

ITEM_FIELDS = """
      item_code
      stock_uom
      item_group
      custom_attribute_set_code
      item_name
      brand
      description
      warranty_period
      image
      custom_sku
      custom_cost
      custom_price
      custom_construction
      custom_load_index
      custom_model
      custom_wheel_type
      custom_gcc_code
      custom_dwb_code
      custom_lookin_code
      custom_url_key
      custom_price_included_text
      custom_color
      custom_hold_down_type
      custom_post_positions
      custom_country
      custom_terminal_type
      custom_voltage
      custom_size
      custom_capacity_ah
      custom_offset
      custom_oem_marking
      custom_offers
      custom_pattern
      custom_cold_test_current
      custom_dimension
      custom_tyre_marking
      custom_oem_tyres
      custom_ev_tyre
      custom_tyre_size
      custom_width
      custom_height
      custom_rim
      custom_vehicle_compatible
      custom_year
      custom_runflat
      custom_tyre_type
      custom_tyres_category
      tax_code
      is_zero_rated
      is_exempt
"""

GRAPHQL_QUERY = """
query ErpProducts($page: Int!, $size: Int!) {
  kleverErpProducts(pageSize: $size, currentPage: $page) {
    total_count
    page_info { current_page total_pages }
    items {
      sku
      status
      item {
""" + ITEM_FIELDS + """
      }
    }
  }
}
"""

PRODUCT_BY_SKU_QUERY = """
query ErpProductBySku($skus: [String]) {
  kleverErpProducts(pageSize: 1, currentPage: 1, filter: { sku: $skus }) {
    items {
      sku
      status
      item {
""" + ITEM_FIELDS + """
      }
    }
  }
}
"""

PASSTHROUGH_FIELDS = [
    "stock_uom", "item_group", "custom_attribute_set_code", "item_name",
    "brand", "description", "warranty_period", "image", "custom_sku",
    "custom_cost", "custom_price", "custom_construction", "custom_load_index",
    "custom_model", "custom_wheel_type", "custom_gcc_code", "custom_dwb_code",
    "custom_lookin_code", "custom_url_key", "custom_price_included_text",
    "custom_color", "custom_hold_down_type", "custom_post_positions",
    "custom_country", "custom_terminal_type", "custom_voltage", "custom_size",
    "custom_capacity_ah", "custom_offset", "custom_oem_marking",
    "custom_offers", "custom_pattern", "custom_cold_test_current",
    "custom_dimension", "custom_tyre_marking", "custom_oem_tyres",
    "custom_ev_tyre", "custom_tyre_size", "custom_width", "custom_height",
    "custom_rim", "custom_vehicle_compatible", "custom_year", "custom_runflat",
    "custom_tyre_type", "custom_tyres_category", "tax_code",
]

CHECK_FIELDS = ["is_zero_rated", "is_exempt"]


def _magento_session():
    api_key = frappe.conf.get("magento_erp_api_key")
    if not api_key:
        raise RuntimeError("magento_erp_api_key not set in site_config.json")
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Store": MAGENTO_STORE,
        "X-Klever-Api-Key": api_key,
    })
    return s


def fetch_products(session):
    page = 1
    while True:
        for attempt in range(4):
            try:
                resp = session.post(
                    MAGENTO_URL,
                    json={"query": GRAPHQL_QUERY, "variables": {"page": page, "size": PAGE_SIZE}},
                    timeout=60,
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("errors"):
                    raise RuntimeError(body["errors"])
                break
            except (requests.exceptions.RequestException, RuntimeError) as e:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
                session = _magento_session()

        data = body["data"]["kleverErpProducts"]
        for item in data["items"]:
            yield item

        if page >= data["page_info"]["total_pages"]:
            break
        page += 1


def translate_product(product):
    magento_item = product.get("item") or {}

    item = {
        "item_code": product["sku"],
        "custom_magento_item_code": magento_item.get("item_code"),
    }

    if product.get("status") is not None:
        item["product_online"] = product["status"]

    for field in PASSTHROUGH_FIELDS:
        value = magento_item.get(field)
        if value is not None and str(value).strip():
            item[field] = value

    for field in CHECK_FIELDS:
        value = magento_item.get(field)
        if value is not None:
            item[field] = value

    return item


def fetch_product_by_sku(sku):
    """Real-time single-product lookup, for enriching an Item created
    inline from a Sales Order with full catalog data instead of only the
    order's own limited fields (see sales_order_api._normalize_order_item).

    Uses the `filter: { sku: [...] }` argument on kleverErpProducts -
    confirmed by the Magento dev to exist for exactly this purpose, so this
    is a direct lookup, not a page-scan of the full catalog.

    Returns a translated item dict (same shape translate_product produces
    for the nightly sync) or None if Magento has no matching product (e.g.
    a non-catalog line like an installation fee, or a typo'd SKU) - the
    caller falls back to the order's own fields in that case, same as if
    this lookup were never attempted.
    """
    session = _magento_session()
    resp = session.post(
        MAGENTO_URL,
        json={"query": PRODUCT_BY_SKU_QUERY, "variables": {"skus": [sku]}},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])

    items = body["data"]["kleverErpProducts"]["items"]
    if not items:
        return None
    return translate_product(items[0])


def run_nightly_sync():
    """Entry point registered in hooks.py's scheduler_events (cron, 2 AM Gulf time).

    Only enqueues the real work onto the "long" RQ queue with an explicit,
    generous timeout - it must NOT do the actual sync itself. Frappe's cron
    dispatcher always runs a scheduled job on a queue with a hardcoded
    timeout (300s for "default"/"short", 1500s even for "long" by default -
    see frappe.utils.background_jobs.get_queue_timeout), and RQ forcibly
    kills anything that exceeds it, no matter what the code is doing.
    Confirmed directly: two nights running, this job was killed at exactly
    300.0 seconds - nowhere near enough for a multi-hour full-catalog sync.
    Re-enqueuing ourselves with an explicit timeout sidesteps that limit
    entirely, since frappe.enqueue's own `timeout` argument overrides it.
    """
    frappe.enqueue(
        "klever_integrations.magento_sync._run_nightly_sync_job",
        queue="long",
        timeout=21600,  # 6 hours - generous headroom over the ~2-3hr typical runtime
        now=False,
    )


def _run_nightly_sync_job():
    """The actual sync - runs as its own background job (see run_nightly_sync)."""
    from klever_integrations.api import import_item

    logger = frappe.logger("magento_sync", allow_site=True, file_count=14)
    # frappe.logger defaults to level ERROR in production (frappe._dev_server
    # is 0), which silently drops both .info() and .warning() calls - our
    # start/summary/failure lines would never actually reach the file
    # without this. Confirmed by direct testing: without this line, nothing
    # was written to either log file despite the calls succeeding.
    logger.setLevel("INFO")
    logger.info(f"Magento nightly sync starting ({MAGENTO_URL}, kleverErpProducts)")

    try:
        session = _magento_session()
    except RuntimeError as e:
        logger.error(str(e))
        return

    counts = {"success": 0, "failed": 0}
    failures = []
    reconnects = 0

    for count, product in enumerate(fetch_products(session), start=1):
        item = translate_product(product)
        try:
            result = import_item(item=item)
        except Exception as e:
            # A background-worker DB connection can desync mid-run (seen in
            # production: "Commands out of sync" from MySQLdb, mid multi-hour
            # run - the same script runs to 100% cleanly under `bench
            # execute`, a plain process, so this is specific to the RQ
            # worker's cooperative-concurrency environment, not our sync
            # logic). rollback() alone doesn't fix a protocol-level desync -
            # get a fresh connection so one bad item can't kill the rest of
            # an hours-long run.
            frappe.db.rollback()
            try:
                frappe.connect()
                reconnects += 1
            except Exception:
                pass
            result = {"success": False, "error": str(e)}

        if result.get("success"):
            counts["success"] += 1
        else:
            counts["failed"] += 1
            failures.append(f"{item.get('item_code')}: {result.get('error')}")

        # Proactively refresh the connection periodically too, not just
        # after a failure - cheaper than risking staleness over hours.
        if count % 500 == 0:
            try:
                frappe.connect()
            except Exception:
                pass
            logger.info(f"  {count} processed so far, summary so far: {counts}")

        time.sleep(REQUEST_DELAY)

    logger.info(f"Magento nightly sync finished. Summary: {counts}. Reconnects: {reconnects}")
    if failures:
        logger.warning("Failures:\n" + "\n".join(failures[:100]))
