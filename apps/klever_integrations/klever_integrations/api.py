"""Magento -> ERPNext product import endpoint.

Exposes one whitelisted method, import_item, that takes a raw product
payload and handles everything needed to create it: checking every Link
field it references against its target doctype, auto-creating any missing
master record, then creating (or updating) the Item itself.

Callers (e.g. a Magento cron job) only need to send the product data - they
never need to know which fields are Link fields or what doctype/field to
create a missing master record with. That knowledge lives in exactly one
place: LINK_FIELD_MAP below.
"""

import frappe


def save_with_retry(fn, max_attempts=5):
    """Call fn() - which must fetch a FRESH doc, apply updates, and save it,
    returning the saved doc - retrying on either of two concurrent-update
    conflicts: frappe.TimestampMismatchError or frappe.DoesNotExistError.

    Two requests updating the same existing record at nearly the same time
    (e.g. two orders for the same popular Item, or the same repeat
    customer's vehicle) can race against Frappe's own optimistic-lock
    version check: whichever saves second finds the doc it loaded is
    already stale and gets rejected outright, even though nothing about
    its own change was actually invalid (confirmed incident 2026-10-26,
    two concurrent orders for the same Item). Retrying with a fresh fetch
    resolves this without losing either request's changes.

    DoesNotExistError is the other half of that same race, for a doc that
    didn't exist yet when both requests started: the loser's create attempt
    hits DuplicateEntryError and falls back to calling this with an update
    fn - but under MySQL's REPEATABLE READ isolation, if this transaction's
    snapshot was taken before the winner committed, fn's own fetch can still
    raise "not found" even though the row now really exists (confirmed
    incident 2026-10-27, "Item TCKL-RACEITEM not found" on a concurrent PO
    sync). Rolling back ends the stale snapshot so the retry's fetch starts
    a fresh one that does see the winner's already-committed row.

    fn is called again from scratch on each retry - it must not rely on
    any doc loaded outside itself, since that doc is exactly what's stale.
    """
    for attempt in range(max_attempts):
        try:
            return fn()
        except (frappe.TimestampMismatchError, frappe.DoesNotExistError):
            frappe.db.rollback()
            if attempt == max_attempts - 1:
                raise

# ---------------------------------------------------------------------------
# ERPNext fieldname -> (target doctype, field used to create a missing record)
#
# Every one of these is a Link field on Item. Before creating/updating an
# Item that sets one of these fields, we confirm the referenced record
# already exists in its target doctype, creating it first if not.
#
# To support a newly added custom Link field later: add one line here.
# Nothing else in this file, and nothing on the calling side, needs to change.
# ---------------------------------------------------------------------------

LINK_FIELD_MAP = {
    "brand": ("Brand", "brand"),
    "item_group": ("Item Group", "item_group_name"),
    "stock_uom": ("UOM", "uom_name"),
    "custom_attribute_set_code": ("Attribute Set", "attribute_set_name"),
    "custom_size": ("Size", "size_name"),
    "custom_capacity_ah": ("Capacity AH", "capacity_ah_name"),
    "custom_offset": ("Offset", "offset_name"),
    "custom_oem_marking": ("OEM Marking", "oem_marking_name"),
    "custom_offers": ("Offers", "offers_name"),
    "custom_price_included_text": ("Price Included Text", "price_included_text_name"),
    "custom_pattern": ("Pattern", "pattern_name"),
    "custom_cold_test_current": ("Cold Test Current", "cold_test_current_name"),
    "custom_dimension": ("Dimension", "dimension_name"),
    "custom_hold_down_type": ("Hold Down Type", "hold_down_type_name"),
    "custom_post_positions": ("Post Positions", "post_positions_name"),
    "custom_tyre_marking": ("Tyre Marking", "tyre_marking_name"),
    "custom_oem_tyres": ("OEM Tyres", "oem_tyres_name"),
    "custom_terminal_type": ("Terminal Type", "terminal_type_name"),
    "custom_voltage": ("Voltage", "voltage_name"),
    "custom_ev_tyre": ("EV Tyre", "ev_tyre_name"),
    "custom_color": ("Color Option", "color_option_name"),
    "custom_tyre_size": ("Tyre Size", "tyre_size_name"),
    "custom_vehicle_compatible": ("Vehicle Compatible", "vehicle_compatible_name"),
    "custom_year": ("Year", "year_name"),
    "custom_country": ("Country", "country_name"),
    "custom_runflat": ("Runflat", "runflat_name"),
    "custom_tyre_type": ("Tyre Type", "tyre_type_name"),
    "custom_tyres_category": ("Tyres Category", "tyres_category_name"),
    "custom_width": ("Width", "width_name"),
    "custom_height": ("Height", "height_name"),
    "custom_rim": ("Rim", "rim_name"),
    "warranty_period": ("Warranty Period", "warranty_period_name"),
}

REQUIRED_FIELDS = ["item_code", "item_name", "item_group", "stock_uom"]

DEFAULT_STOCK_UOM = "Nos"

# ---------------------------------------------------------------------------
# Raw Magento field -> (real ERPNext fieldname, function to convert the value)
#
# Lets the caller send Magento's own raw field/value as-is, with no
# conversion logic on their side - the translation lives here instead.
#
# product_online: Magento uses 1 = enabled/online, 2 = disabled. ERPNext's
# `disabled` field is the opposite polarity and a plain 0/1 checkbox, so
# both the meaning and the numbering need converting, not just copying.
# ---------------------------------------------------------------------------

RAW_FIELD_TRANSLATIONS = {
    "product_online": ("disabled", lambda v: 0 if str(v).strip() == "1" else 1),
}


def _apply_raw_translations(item):
    for raw_field, (target_field, convert) in RAW_FIELD_TRANSLATIONS.items():
        if raw_field in item:
            value = item.pop(raw_field)
            item[target_field] = convert(value)
    return item


def _apply_defaults(item):
    if not item.get("stock_uom"):
        item["stock_uom"] = DEFAULT_STOCK_UOM
    if not item.get("item_group") and item.get("custom_attribute_set_code"):
        item["item_group"] = item["custom_attribute_set_code"]
    return item


# Known name mismatches for STANDARD Frappe doctypes with strict built-in
# validation (unlike our own custom lookup doctypes, which accept any
# value). Country is the main one: it only accepts real, ISO-recognized
# names. Add more doctype/alias entries here if the same problem shows up
# elsewhere later.
VALUE_ALIASES = {
    "Country": {
        "South Korea": "Korea, Republic of",
        "Turkey": "Türkiye",
        "Great Britain": "United Kingdom",
        "England": "United Kingdom",
    },
}


def _ensure_link_value(item, fieldname, created_log, skipped_log):
    """Make sure the record this Link field points to exists, creating it
    if not. No-op for fields not in LINK_FIELD_MAP (plain Data fields).

    Applies known aliases for stricter standard doctypes (e.g. Country).
    If the value still can't be created (genuinely invalid source data,
    like "0" or "Europe" for a country), removes it from `item` instead of
    failing the whole request - one bad field shouldn't block an otherwise
    valid item.
    """
    value = item.get(fieldname)
    if not value or fieldname not in LINK_FIELD_MAP:
        return

    doctype, naming_field = LINK_FIELD_MAP[fieldname]

    alias = VALUE_ALIASES.get(doctype, {}).get(value)
    if alias:
        value = alias
        item[fieldname] = alias

    if frappe.db.exists(doctype, value):
        return

    try:
        # ignore_if_duplicate=True: two orders/syncs referencing the same
        # not-yet-seen link value (e.g. a new Brand) can race between the
        # exists-check above and this insert - without it, the loser's
        # DuplicateEntryError would fall into the except below and get
        # treated as "invalid value, delete it", silently dropping a field
        # that actually exists and is valid (see sales_order_api's
        # _get_or_create_vehicle for the confirmed incident this mirrors,
        # 2026-10-26).
        frappe.get_doc({"doctype": doctype, naming_field: value}).insert(
            ignore_permissions=True, ignore_if_duplicate=True
        )
        created_log.append(f"{doctype}: {value}")
        # Commit before any further read of this record in this same
        # request. This isn't really about Frappe's app-level cache (an
        # earlier version of this comment assumed that, and a parallel fix
        # in sales_order_api.py confirmed it doesn't actually hold) - it's
        # MySQL's REPEATABLE READ isolation: if this insert lost a race to
        # a concurrent request creating the same value, this transaction's
        # snapshot was taken before that request committed, so a plain read
        # right after (e.g. the Item's own Link-field validation on save)
        # would still see "doesn't exist" even though the row is now really
        # there. Committing ends the stale snapshot.
        frappe.db.commit()
        frappe.db.get_value(doctype, value, "name", cache=True)
    except Exception as e:
        frappe.db.rollback()
        del item[fieldname]
        skipped_log.append(f"{fieldname} ({value}): {e}")


@frappe.whitelist(methods=["POST"])
def import_item(item=None, **kwargs):
    """Create or update an Item, auto-creating any missing Link field
    master records along the way.

    Accepts the item fields either as a nested `item` dict, or as top-level
    kwargs - whichever is more convenient for the caller.
    """
    if item is None:
        item = kwargs
    if isinstance(item, str):
        import json

        item = json.loads(item)

    item = dict(item)
    item.pop("doctype", None)
    item = _apply_raw_translations(item)
    item = _apply_defaults(item)

    missing = [f for f in REQUIRED_FIELDS if not item.get(f)]
    if missing:
        frappe.local.response.http_status_code = 417
        return {
            "success": False,
            "item_code": item.get("item_code"),
            "error": f"Missing required field(s): {', '.join(missing)}",
        }

    created_masters = []
    skipped_fields = []

    try:
        for fieldname in list(item.keys()):
            _ensure_link_value(item, fieldname, created_masters, skipped_fields)

        item_code = item["item_code"]
        exists = frappe.db.exists("Item", item_code)

        def _update_item():
            doc = frappe.get_doc("Item", item_code)
            doc.update(item)
            doc.save(ignore_permissions=True)
            return doc

        if exists:
            # save_with_retry: two orders for the same existing, popular
            # SKU can race on Frappe's own optimistic-lock version check
            # (confirmed incident 2026-10-26, "has been modified after you
            # have opened it") - retrying with a fresh fetch resolves it.
            doc = save_with_retry(_update_item)
            action = "updated"
        else:
            try:
                doc = frappe.get_doc({"doctype": "Item", **item})
                doc.insert(ignore_permissions=True)
                action = "created"
            except frappe.DuplicateEntryError:
                # Lost a race with a concurrent request creating the same
                # brand-new Item (two orders for the same not-yet-seen SKU
                # processed by different workers at once) - fall back to
                # updating the one that won instead of silently dropping
                # this request's field values. Same race class as
                # sales_order_api._get_or_create_vehicle's confirmed
                # Make/Model/Year incident (2026-10-26).
                doc = save_with_retry(_update_item)
                action = "updated"

        frappe.db.commit()

        return {
            "success": True,
            "item_code": doc.item_code,
            "action": action,
            "master_records_created": created_masters,
            "fields_skipped": skipped_fields,
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.local.response.http_status_code = 417
        return {
            "success": False,
            "item_code": item.get("item_code"),
            "error": str(e),
            "master_records_created": created_masters,
            "fields_skipped": skipped_fields,
        }


# ---------------------------------------------------------------------------
# Vehicle Make -> Model -> Year cascading filter
#
# Model.year_ranges stores a JSON list of "YYYY-YYYY" strings, e.g.
# ["2006-2009", "2011-2015"] - multiple ranges with real gaps between them
# (a model generation that wasn't sold for a stretch of years), not one
# continuous span. Expand each range separately so gaps are preserved.
# ---------------------------------------------------------------------------

import json
import re


def _parse_year_ranges(raw):
    if not raw:
        return []
    try:
        ranges = json.loads(raw)
    except (TypeError, ValueError):
        return []
    years = set()
    for r in ranges:
        m = re.match(r"^(\d{4})-(\d{4})$", str(r).strip())
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            years.update(range(start, end + 1))
        elif re.match(r"^\d{4}$", str(r).strip()):
            years.add(int(r.strip()))
    return sorted(years)


@frappe.whitelist()
def get_valid_years_for_model(model):
    """Years the given Model was actually produced in, for the Year field's
    dynamic filter on the Customer Vehicle form. Empty list (not an error)
    when the model has no year_ranges data - the Year field then falls back
    to showing every year, same as before this feature existed.
    """
    if not model:
        return []
    raw = frappe.db.get_value("Model", model, "year_ranges")
    return [str(y) for y in _parse_year_ranges(raw)]
