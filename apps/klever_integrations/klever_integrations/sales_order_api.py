"""Magento -> ERPNext Sales Order import endpoint.

Companion to api.py (which handles product/Item sync). This is the other
direction of the Magento integration: instead of us pulling products, the
Magento admin has a "Push to ERP" button that POSTs one completed order's
full data here - customer, vehicle, address, items and payment all in one
payload - and we create everything it needs in ERPNext in a single call.

Design notes (see project conversation for the full reasoning):
- Items are created/updated INLINE from the order payload itself (reusing
  import_item from api.py), not from a separate product sync - so this
  endpoint has no dependency on the nightly sync at all.
- Customer matching follows ERPNext's own convention exactly as used by
  erpnext.crm.frappe_crm_api.create_customer/create_contacts/contact_exists:
  Customer itself has no email field - identity lives on a linked Contact
  record instead, matched via Contact Email/Contact Phone child tables.
- The Sales Order is created AND submitted immediately (orders are treated
  as final once placed).
- Sales Invoice auto-creation is PAUSED as of 2026-10-16 per the business
  lead - import_sales_order only creates the Sales Order now. The Sales
  Invoice has to be created manually from it in ERPNext (Create > Sales
  Invoice), then pushed to Magento via the "Sync Invoice" button
  (submit_and_push_invoice below), which still works unchanged. Previously
  (and the mechanism this can be switched back to) the Sales Invoice was
  inserted as a draft right after the Sales Order and a background job
  (_auto_submit_and_push_invoice) submitted it and pushed its PDF to
  Magento automatically, within moments, with no manual step.
- Idempotent on custom_website_order_number: re-sending the same order
  number is treated as a correction request, not a duplicate, since the
  Magento-side button can be clicked again for the same order.
"""

import base64
import json
import re

import frappe
import requests
from frappe.utils import getdate, nowdate

from erpnext.controllers.accounts_controller import get_default_taxes_and_charges

from klever_integrations.api import import_item, save_with_retry
from klever_integrations.magento_sync import fetch_product_by_sku

DEFAULT_COMPANY = "DSP Trade Hub FZ-LLC"
DEFAULT_CURRENCY = "AED"
DEFAULT_PRICE_LIST = "Standard Selling"
DEFAULT_CUSTOMER_GROUP = "Individual"
# A brand-new item (not yet in our Item master or Magento's catalog) is
# created stock-tracked with no warehouse set, which ERPNext rejects at
# Sales Order submission ("Source warehouse required"). Assigning this
# explicitly on every line item sidesteps that regardless of whether the
# item is new or already exists.
DEFAULT_WAREHOUSE = "Main Warehouse - DSP"
DEFAULT_TERRITORY = "United Arab Emirates"
# Magento sends an "installer" block even for these shipping methods (e.g.
# the customer's own city, not a real installer) - per business rule,
# there is no installer for these, so it must be ignored even if present.
SHIPPING_METHODS_WITHOUT_INSTALLER = {"Mobile Van Service"}
# Not hardcoded to a specific template name - always resolves to whichever
# Sales Taxes and Charges Template is marked default for the order's company
# (currently "UAE VAT 5% - DSP"), so this stays correct if that changes.
DEFAULT_TAX_MASTER_DOCTYPE = "Sales Taxes and Charges Template"
# Per explicit business/lead instruction (2026-10-26): orders dated before
# this cutoff must NOT have VAT applied at all, regardless of what the
# company's default tax template says - UAE VAT only became mandatory for
# this business from this date onward, so earlier orders are genuinely
# exempt, not just missing a default.
VAT_EXEMPT_BEFORE_DATE = getdate("2025-08-01")


# ---------------------------------------------------------------------------
# Customer + Contact + Address - same pattern ERPNext's own CRM integration
# uses (erpnext.crm.frappe_crm_api), reused here rather than reinvented.
# ---------------------------------------------------------------------------

def _contact_exists(email, mobile_no):
    email_exist = email and frappe.db.exists("Contact Email", {"email_id": email})
    mobile_exist = mobile_no and frappe.db.exists("Contact Phone", {"phone": mobile_no})
    doctype = "Contact Email" if email_exist else "Contact Phone"
    name = email_exist or mobile_exist
    if name:
        return frappe.db.get_value(doctype, name, "parent")
    return None


def _link_doc(doc, link_doctype, link_docname):
    already_linked = any(
        link.link_doctype == link_doctype and link.link_name == link_docname for link in doc.links
    )
    if not already_linked:
        doc.append("links", {"link_doctype": link_doctype, "link_name": link_docname})


def _get_or_create_customer(customer_data, order_number=None, is_installer=False):
    """customer_data: {"name": ..., "email": ..., "phone": ...}
    Matched by email (or phone as fallback) against Contact records, exactly
    like ERPNext's own CRM sync does it - Customer has no email field of its
    own, identity lives on a linked Contact.

    Installers are themselves ordinary Customer records with
    custom_is_installer checked (that's how the existing Sales Invoice
    client script's Installer field filters its dropdown) - reused here
    rather than building a separate installer-matching path.
    """
    email = customer_data.get("email")
    phone = customer_data.get("phone")
    # customer_name is a required Data field - never pass None through to it
    # (a real Magento order always has at least an email, but don't crash on
    # a malformed/incomplete one - fall back to something traceable instead).
    full_name = customer_data.get("name") or email or phone or f"Guest ({order_number or 'unknown order'})"

    existing_contact_name = _contact_exists(email, phone)
    if existing_contact_name:
        contact = frappe.get_doc("Contact", existing_contact_name)
        for link in contact.links:
            if link.link_doctype == "Customer":
                if is_installer and not frappe.db.get_value("Customer", link.link_name, "custom_is_installer"):
                    frappe.db.set_value("Customer", link.link_name, "custom_is_installer", 1)
                return link.link_name

    # No existing Customer found via contact - create both.
    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": full_name,
        "customer_type": "Individual",
        "customer_group": DEFAULT_CUSTOMER_GROUP,
        "territory": DEFAULT_TERRITORY,
        "custom_is_installer": 1 if is_installer else 0,
    })
    customer.insert(ignore_permissions=True)

    contact = frappe.get_doc({
        "doctype": "Contact",
        "first_name": full_name,
    })
    if email:
        contact.append("email_ids", {"email_id": email, "is_primary": 1})
    if phone:
        contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
    _link_doc(contact, "Customer", customer.name)
    contact.insert(ignore_permissions=True)

    return customer.name


def _get_or_create_address(address_data, party_doctype, party_name, address_type):
    """address_data: {"name": ..., "address_line1": ..., "city": ..., "country": ..., "phone": ...}
    Always creates a fresh Address for this party unless an identically-named
    one already exists (matching create_address's own dedup approach) -
    simplest correct behaviour given orders can have different addresses
    per purchase even for the same repeat customer.
    """
    if not address_data:
        return None

    existing = address_data.get("name") and frappe.db.exists("Address", address_data.get("name"))
    if existing:
        address = frappe.get_doc("Address", existing)
        _link_doc(address, party_doctype, party_name)
        address.save(ignore_permissions=True)
        return address.name

    address = frappe.get_doc({
        "doctype": "Address",
        "address_title": address_data.get("name") or party_name,
        "address_type": address_type,
        "address_line1": address_data.get("address_line1") or address_data.get("address") or "Not Provided",
        "city": address_data.get("city") or "Not Provided",
        "country": address_data.get("country") or "United Arab Emirates",
        "phone": address_data.get("phone"),
    })
    address.append("links", {"link_doctype": party_doctype, "link_name": party_name})
    address.insert(ignore_permissions=True, ignore_mandatory=True)
    return address.name


# ---------------------------------------------------------------------------
# Vehicle - Make/Model/Year/Customer Vehicle, matched by plate number.
# ---------------------------------------------------------------------------

# Known "no real plate on file" placeholders Magento sends in place of an
# actual license plate (confirmed seen: "000", "0000", "N/A") - anything
# that's just zeros, or one of these explicit "not applicable" tokens.
_PLACEHOLDER_PLATE_TOKENS = {"n/a", "na", "none", "nil", "null", "-"}
_ALL_ZEROS_RE = re.compile(r"^0+$")


def _is_placeholder_plate(plate_no):
    normalized = str(plate_no).strip().lower()
    return bool(_ALL_ZEROS_RE.match(normalized)) or normalized in _PLACEHOLDER_PLATE_TOKENS


def _get_or_create_vehicle(vehicle_data, customer_name, order_number=None):
    """vehicle_data: {"plate_no": ..., "make": ..., "model": ..., "year": ...}
    Returns (customer_vehicle_name, make_name, model_name, year_name) - the
    caller needs all four for the Sales Order/Invoice custom fields, not
    just the Customer Vehicle link.
    """
    if not vehicle_data or not vehicle_data.get("plate_no"):
        return None, None, None, None

    plate_no = vehicle_data["plate_no"]
    # Customer Vehicle is autonamed directly from plate_no (field:plate_no),
    # so a placeholder plate Magento sends for "no real plate on file" -
    # confirmed values seen: "000", "0000", "N/A" - isn't just cosmetically
    # wrong, it actively collides: every order sharing the same placeholder
    # becomes the SAME Customer Vehicle record, with each later order
    # silently overwriting the previous one's real make/model/year/customer
    # (confirmed incident 2026-10-26 - 10 real orders all collapsed onto one
    # "000" vehicle, 5 onto "0000", losing all but the last one's data).
    # Appending the order number keeps the placeholder visible (so it's
    # still obvious at a glance this isn't a real plate) while making every
    # such vehicle its own record, same as a real distinct plate would be.
    if _is_placeholder_plate(plate_no) and order_number:
        plate_no = f"{plate_no}-{order_number}"
    # Strip whitespace before anything else touches these. Confirmed
    # incident (2026-10-26): a trailing space on "Toyota " is insignificant
    # to MySQL's own string comparison (exists("Make", "Toyota ") matches
    # the existing clean "Toyota" record), so the make-creation check below
    # never normalizes it - but that same space, once baked into the middle
    # of model_name (f"{make_name}-{model_label}" -> "Toyota -Camry"), is
    # no longer trailing and MySQL treats it as a genuinely different
    # string. The Model document that actually gets saved ends up correctly
    # named "Toyota-Camry" (Frappe resolves the Link back to its clean
    # canonical form internally), but this function's own local
    # model_name variable never learns that - so every use of it afterward
    # (e.g. setting Customer Vehicle.model) references a name that was
    # never actually saved anywhere, failing Link validation with
    # "Could not find Model: Toyota -Camry". Stripping here prevents the
    # divergence from ever starting.
    make_name = (vehicle_data.get("make") or "").strip() or None
    model_label = (vehicle_data.get("model") or "").strip() or None
    year_name = (vehicle_data.get("year") or "")
    year_name = year_name.strip() if isinstance(year_name, str) else year_name

    # Some senders (confirmed: the real Magento integration) send Magento's
    # internal URL slugs instead of proper display names - e.g.
    # make="alfa-romeo", model="alfa-romeo-156" instead of "Alfa Romeo"/"156".
    # Translate via the slug/combined_slug fields (backfilled from the same
    # makes.sql/models.sql data the Make/Model masters were originally
    # imported from) before falling through to name-matching or
    # auto-creation, so slugs resolve to the real, already-imported records
    # (with correct year_ranges data) instead of creating disconnected
    # lowercase-slug duplicates.
    model_name = None
    if model_label:
        slug_match = frappe.db.get_value(
            "Model", {"combined_slug": model_label}, ["name", "make"], as_dict=True
        )
        if slug_match:
            model_name = slug_match.name
            make_name = slug_match.make

    # ignore_if_duplicate=True on every insert below: two orders for the
    # same new Make/Model/Year/vehicle can be processed by different worker
    # processes at nearly the same instant - both pass the "not exists"
    # check before either commits, then both try to insert, and the loser
    # hits a primary-key collision (confirmed incident 2026-10-26:
    # IntegrityError 1062 on Model "Toyota-Camry"). ignore_if_duplicate is
    # Frappe's own built-in handling for exactly this race - it swallows
    # the collision instead of raising, since losing the race just means
    # the record already exists with the name we wanted anyway.
    #
    # The frappe.db.commit() after every one of these is NOT about Frappe's
    # app-level cache (an earlier version of this fix wrongly assumed that
    # and didn't actually resolve the issue) - it's MySQL's REPEATABLE READ
    # transaction isolation. The race LOSER's transaction snapshot was taken
    # before the winner committed, so plain reads later in this SAME
    # request/transaction (e.g. Frappe's own Link-field validation when
    # saving the Model or Customer Vehicle doc, which reference this Make)
    # keep seeing the pre-commit "doesn't exist" state even though the row
    # is now really there - confirmed incident 2026-10-26, "Could not find
    # Make: X" immediately after losing this exact race. Committing here
    # ends the stale snapshot; the next statement starts a fresh one that
    # does see the winner's already-committed row.
    if make_name and not frappe.db.exists("Make", make_name):
        slug_make = frappe.db.get_value("Make", {"slug": make_name}, "name")
        make_name = slug_make or make_name
        if not frappe.db.exists("Make", make_name):
            frappe.get_doc({"doctype": "Make", "make_name": make_name}).insert(
                ignore_permissions=True, ignore_if_duplicate=True
            )
            frappe.db.commit()

    if not model_name and make_name and model_label:
        model_name = f"{make_name}-{model_label}"
        if not frappe.db.exists("Model", model_name):
            frappe.get_doc({
                "doctype": "Model", "model_name": model_label, "make": make_name,
            }).insert(ignore_permissions=True, ignore_if_duplicate=True)
            frappe.db.commit()

    if year_name and not frappe.db.exists("Year", year_name):
        frappe.get_doc({"doctype": "Year", "year_name": str(year_name)}).insert(
            ignore_permissions=True, ignore_if_duplicate=True
        )
        frappe.db.commit()

    def _update_vehicle():
        v = frappe.get_doc("Customer Vehicle", plate_no)
        v.update({
            "customer": customer_name, "make": make_name, "model": model_name, "year": year_name,
            "color": vehicle_data.get("color") or v.color,
            "chassis_no": vehicle_data.get("chassis_no") or v.chassis_no,
        })
        v.save(ignore_permissions=True)
        return v

    if frappe.db.exists("Customer Vehicle", plate_no):
        # save_with_retry: two orders for the same vehicle (same plate_no)
        # can race on Frappe's own optimistic-lock version check, same
        # class as the Item race above.
        vehicle = save_with_retry(_update_vehicle)
    else:
        vehicle = frappe.get_doc({
            "doctype": "Customer Vehicle",
            "plate_no": plate_no,
            "customer": customer_name,
            "make": make_name,
            "model": model_name,
            "year": year_name,
            "color": vehicle_data.get("color"),
            "chassis_no": vehicle_data.get("chassis_no"),
        })
        try:
            vehicle.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            # Lost the same kind of race as Make/Model/Year above, but
            # unlike those this request's own field values (customer, make,
            # model, year) still need to land somewhere - fall back to
            # updating the record the concurrent winner just created,
            # instead of silently dropping this request's data.
            vehicle = save_with_retry(_update_vehicle)

    _sync_customer_vehicle_table(customer_name, {
        "plate_no": plate_no, "make": make_name, "model": model_name, "year": year_name,
        "color": vehicle_data.get("color"), "chassis_no": vehicle_data.get("chassis_no"),
    })

    return vehicle.name, make_name, model_name, year_name


def _sync_customer_vehicle_table(customer_name, vehicle_vals):
    """Keep the Customer's own "Vehicle Information" section (the
    custom_vehicles child table shown on the Customer form) in sync with the
    standalone Customer Vehicle record above.

    These are two separate systems that happen to overlap - the existing
    Sales Invoice client script already does this same sync, but only
    client-side, triggered by a human picking a vehicle in the browser. An
    order arriving through this API never goes through a browser, so that
    JS never runs - this is the server-side equivalent, run unconditionally
    here so the two stay in sync no matter which path created the vehicle.
    """
    def _update_customer():
        customer = frappe.get_doc("Customer", customer_name)
        existing_row = next(
            (row for row in customer.get("custom_vehicles", []) if row.plate_no == vehicle_vals["plate_no"]),
            None,
        )
        if existing_row:
            existing_row.update({k: v for k, v in vehicle_vals.items() if v})
        else:
            customer.append("custom_vehicles", {k: v for k, v in vehicle_vals.items() if v})
        customer.save(ignore_permissions=True)
        return customer

    # save_with_retry: two orders for the same repeat customer can race on
    # Frappe's own optimistic-lock version check, same class as the Item
    # and Customer Vehicle races above.
    save_with_retry(_update_customer)


# ---------------------------------------------------------------------------
# Generic Link-field master auto-create, same philosophy as
# klever_integrations.api._ensure_link_value for Items: a payment method or
# shipping method Magento sends that we haven't seen before just becomes a
# new master record, rather than failing the whole order.
# ---------------------------------------------------------------------------

_LINK_MASTER_NAMING_FIELD = {
    "Payment Method": "payment_method_name",
    "Shipping Method": "shipping_method_name",
}


def _ensure_master(doctype, value):
    if not value:
        return None
    if not frappe.db.exists(doctype, value):
        # ignore_if_duplicate=True: same concurrent-order race as
        # Make/Model/Year in _get_or_create_vehicle. The commit after it
        # guards against the same stale-transaction-snapshot follow-on
        # failure found there too (see the comment on that block for the
        # full explanation) - not an app-cache issue, a MySQL REPEATABLE
        # READ one.
        frappe.get_doc({
            "doctype": doctype, _LINK_MASTER_NAMING_FIELD[doctype]: value,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)
        frappe.db.commit()
    return value


# ---------------------------------------------------------------------------
# Order line item identifiers - align with the nightly sync's own convention
# (see magento_sync.translate_product). Magento sends two different things
# under similar-looking names: the real, unique `sku` (what ERPNext's
# item_code must be), and a separate, NON-unique `item_code` attribute
# (confirmed: can repeat across different products - it must never become
# ERPNext's item_code). Confirmed on a real order (SAL-ORD-2026-00008,
# since corrected) that treating `item_code` as the identifier created a
# disconnected duplicate Item instead of matching the real, already-synced
# one.
# ---------------------------------------------------------------------------

def _normalize_order_item(item):
    """Resolve the real item_code, then enrich with full Magento catalog
    data via fetch_product_by_sku - so an item created fresh from an order
    gets the same cost/brand/description/etc. detail the nightly sync would
    have given it, instead of only the order payload's limited fields.

    Catalog data is treated as authoritative where both have the same
    field; the order's own fields (qty, rate, or anything catalog doesn't
    have) are layered on top. If the lookup fails or finds nothing (a
    non-catalog line like an installation fee, a typo'd SKU, or Magento
    being briefly unreachable), falls back to just the order's own fields -
    this must never block the order itself.

    A line flagged "is_service": true (shipping/handling, mobile van
    service, etc. - charges Magento sends as regular line items so their
    amount is included in the order total) skips the catalog lookup
    entirely (it was never going to be in Magento's product catalog) and
    is created as a non-stock Item - see the "_is_service" key on the
    returned dict, used by import_sales_order to skip assigning
    DEFAULT_WAREHOUSE on that line.
    """
    item = dict(item)
    sku = item.get("sku")
    magento_item_code = item.get("item_code")
    if sku:
        item["item_code"] = sku
    # else: sku not sent yet (older-format payload) - fall back to
    # item_code as-is, same as before this feature existed.
    item.pop("sku", None)

    is_service = bool(item.pop("is_service", False))
    if is_service:
        item["is_stock_item"] = 0

    real_sku = item.get("item_code")
    catalog = None
    if real_sku and not is_service:
        try:
            catalog = fetch_product_by_sku(real_sku)
        except Exception as e:
            frappe.log_error(
                title="Magento product lookup failed (order item)",
                message=f"sku={real_sku}: {e}",
            )

    if catalog:
        result = catalog
        for k, v in item.items():
            if v not in (None, "") and k not in result:
                result[k] = v
        if magento_item_code and not result.get("custom_magento_item_code"):
            result["custom_magento_item_code"] = magento_item_code
    else:
        result = item
        if magento_item_code and not result.get("custom_magento_item_code"):
            result["custom_magento_item_code"] = magento_item_code

    # Mirror item_code into the custom SKU field, and item_group into the
    # custom Attribute Set Code field - both would otherwise stay empty on
    # items created inline from an order (only the nightly catalog sync's
    # own payload happens to send these as separate fields). Forced last so
    # they're always set regardless of whether the catalog lookup above
    # found anything.
    if result.get("item_code"):
        result["custom_sku"] = result["item_code"]
    if result.get("item_group"):
        result["custom_attribute_set_code"] = result["item_group"]

    # Always force the item enabled for order-triggered creation/updates
    # (2026-10-24 incident: a real customer order referencing a since-
    # disabled Magento product got rejected outright - ERPNext's own
    # validate() chain calls validate_end_of_life() via
    # set_missing_item_details() for every Sales/Purchase Order line,
    # regardless of how the document was created, and throws "Item {0} is
    # disabled" - blocking an already-placed real order from even being
    # recorded). Per explicit business decision, catalog status is not
    # tracked through the order-import path at all - every item an order
    # touches becomes/stays enabled, whether newly created or already
    # existing (and already disabled) beforehand. Popping the raw field
    # first, then forcing "disabled" directly, so neither the catalog
    # lookup's value nor the item's own prior state can leave it disabled.
    # Disabling obsolete items is still a legitimate thing for the nightly
    # full catalog sync to do (see api.py's product_online -> disabled
    # translation, untouched here, still applied there).
    result.pop("product_online", None)
    result["disabled"] = 0

    result["_is_service"] = is_service
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def import_sales_order(order=None, **kwargs):
    if order is None:
        order = kwargs
    if isinstance(order, str):
        order = json.loads(order)
    order = frappe._dict(order)

    order_number = order.get("order_number")
    if not order_number:
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": "Missing required field: order_number"}

    if not order.get("items"):
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": "Missing required field: items (at least one line item is required)"}

    # Exclude cancelled Sales Orders - a cancel+amend cycle (e.g. a manual
    # data fix) leaves a cancelled original and a submitted amended order
    # both carrying the same custom_website_order_number. Matching the
    # cancelled one here would report "already exists" but fail to find the
    # real invoice, since that's linked to the amended order instead.
    existing_so = frappe.db.get_value(
        "Sales Order", {"custom_website_order_number": order_number, "docstatus": ["!=", 2]}, "name"
    )
    if existing_so:
        existing_si = frappe.db.get_value(
            "Sales Invoice", {"sales_order": existing_so, "docstatus": ["!=", 2]}, "name"
        )
        return {
            "success": True,
            "already_exists": True,
            "sales_order": existing_so,
            "sales_invoice": existing_si,
            "note": "This order_number already has a Sales Order. Edit the existing Sales "
                    "Order/Invoice directly in ERPNext rather than resending - resending "
                    "does not currently update an already-created order.",
        }

    try:
        order_items = [_normalize_order_item(item) for item in order.get("items", [])]

        item_results = []
        for item in order_items:
            # "_is_service" is this function's own bookkeeping (consumed
            # again below for the Sales Order line), not an Item field.
            item_for_import = {k: v for k, v in item.items() if k != "_is_service"}
            result = import_item(item=item_for_import)
            item_results.append({"item_code": item.get("item_code"), "success": result.get("success"), "error": result.get("error")})
            if not result.get("success"):
                frappe.db.rollback()
                frappe.local.response.http_status_code = 417
                return {"success": False, "error": f"Item import failed: {result.get('error')}", "item_results": item_results}

        customer_name = _get_or_create_customer(order.get("customer", {}), order_number)

        billing_address = _get_or_create_address(
            order.get("billing_address"), "Customer", customer_name, "Billing"
        )
        shipping_address = _get_or_create_address(
            order.get("fitment_details") or order.get("shipping_address"), "Customer", customer_name, "Shipping"
        )

        vehicle_name, make_name, model_name, year_name = _get_or_create_vehicle(
            order.get("vehicle"), customer_name, order_number
        )

        installer_name = None
        installer_data = order.get("installer")
        if (
            order.get("shipping_method") not in SHIPPING_METHODS_WITHOUT_INSTALLER
            and installer_data
            and (installer_data.get("name") or installer_data.get("email"))
        ):
            installer_name = _get_or_create_customer(installer_data, order_number, is_installer=True)
        else:
            installer_data = None

        payment = order.get("payment", {})
        payment_method = _ensure_master("Payment Method", payment.get("method"))
        shipping_method = _ensure_master("Shipping Method", order.get("shipping_method"))

        company = order.get("company", DEFAULT_COMPANY)
        order_date = order.get("order_date") or nowdate()
        tax_defaults = {}
        if getdate(order_date) >= VAT_EXEMPT_BEFORE_DATE:
            tax_defaults = get_default_taxes_and_charges(DEFAULT_TAX_MASTER_DOCTYPE, company=company) or {}

        so = frappe.get_doc({
            "doctype": "Sales Order",
            "company": company,
            "customer": customer_name,
            "order_type": "Shopping Cart",
            "transaction_date": order_date,
            "delivery_date": order_date,
            "currency": order.get("currency", DEFAULT_CURRENCY),
            "selling_price_list": DEFAULT_PRICE_LIST,
            "customer_address": billing_address,
            "shipping_address_name": shipping_address,
            "custom_website_order_number": order_number,
            "custom_vehicle": vehicle_name,
            "custom_make": make_name,
            "custom_model": model_name,
            "custom_year": year_name,
            "custom_payment_method": payment_method,
            "custom_transaction_id": payment.get("transaction_id"),
            "custom_shipping_method": shipping_method,
            "custom_installer": installer_name,
            "custom_installer_name": installer_data.get("name") if installer_data else None,
            "vat_emirate": order.get("vat_emirate"),
            "tourist_tax_return": order.get("tourist_tax_return"),
            "permit_no": order.get("permit_no"),
            "taxes_and_charges": tax_defaults.get("taxes_and_charges"),
            "taxes": tax_defaults.get("taxes") or [],
            "items": [
                {
                    "item_code": item.get("item_code"),
                    "qty": item.get("qty", 1),
                    "rate": item.get("rate"),
                    # Non-stock service/charge lines (shipping, handling,
                    # mobile van service, etc.) have no warehouse to assign.
                    **({} if item.get("_is_service") else {"warehouse": DEFAULT_WAREHOUSE}),
                }
                for item in order_items
            ],
        })
        so.insert(ignore_permissions=True)
        so.submit()

        # Sales Invoice auto-creation is PAUSED (per explicit request from
        # the business lead, 2026-10-16) - only the Sales Order is created
        # now. Previously this inserted a draft Sales Invoice via
        # make_sales_invoice(so.name) right here and enqueued
        # _auto_submit_and_push_invoice to submit it and push its PDF to
        # Magento automatically. That invoice step must now be done
        # manually from the Sales Order in ERPNext (Create > Sales
        # Invoice), then pushed via the "Sync Invoice" button
        # (submit_and_push_invoice below) - which still works unchanged.
        # To resume auto-creation, see _auto_submit_and_push_invoice further
        # down in this file for the submit+push logic to re-wire back in
        # here via a make_sales_invoice(so.name) + si.insert() + enqueue.

        frappe.db.commit()

        return {
            "success": True,
            "sales_order": so.name,
            "sales_invoice": None,
            "customer": customer_name,
            "customer_vehicle": vehicle_name,
            "item_results": item_results,
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# "Push to ERP Order Sync" button - server-side half. Submits the draft
# invoice, renders it to PDF, and uploads it onto the original Magento order
# via the setOrderUploadAttachment mutation the Magento dev built for this
# (see Tyrescart.postman_collection.json). Auth for that mutation is an
# api_key embedded in the GraphQL variables themselves, not a header - a
# different credential from magento_erp_api_key (which is for calls Magento
# sends TO us), stored separately as magento_push_api_key.
# ---------------------------------------------------------------------------

MAGENTO_GRAPHQL_URL = "https://www.tyrescart.ae/graphql"
INVOICE_PRINT_FORMAT = "Sales Invoice with Item Image - Klever"

SET_ORDER_UPLOAD_ATTACHMENT_MUTATION = """
mutation SetOrderUploadAttachment($input: OrderUploadAttachmentInput!) {
  setOrderUploadAttachment(input: $input) {
    status
    message
    result
  }
}
"""


def _push_invoice_to_magento(sales_invoice_doc):
    api_key = frappe.conf.get("magento_push_api_key")
    if not api_key:
        return {"pushed": False, "reason": "magento_push_api_key not set in site_config.json"}

    sales_order_no = sales_invoice_doc.custom_website_order_number
    if not sales_order_no:
        return {"pushed": False, "reason": "No custom_website_order_number on this invoice - not a Magento order"}

    sales_order_name = sales_invoice_doc.items[0].sales_order if sales_invoice_doc.items else None
    sales_order_date = (
        frappe.db.get_value("Sales Order", sales_order_name, "transaction_date") if sales_order_name else None
    )
    sales_order_date = str(sales_order_date or sales_invoice_doc.posting_date)

    pdf_bytes = frappe.get_print(
        "Sales Invoice", sales_invoice_doc.name,
        print_format=INVOICE_PRINT_FORMAT, as_pdf=True,
    )

    variables = {
        "input": {
            "api_key": api_key,
            "sales_order_no": sales_order_no,
            "sales_order_date": sales_order_date,
            "document_name": f"{sales_invoice_doc.name}.pdf",
            "erp_order_no": sales_order_name or sales_invoice_doc.name,
            "pdf": base64.b64encode(pdf_bytes).decode(),
        }
    }

    try:
        resp = requests.post(
            MAGENTO_GRAPHQL_URL,
            json={"query": SET_ORDER_UPLOAD_ATTACHMENT_MUTATION, "variables": variables},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        return {"pushed": False, "reason": f"Request to Magento failed: {e}"}

    if body.get("errors"):
        return {"pushed": False, "reason": str(body["errors"])}

    result = (body.get("data") or {}).get("setOrderUploadAttachment") or {}
    return {
        "pushed": True,
        "status": result.get("status"),
        "message": result.get("message"),
        "result": result.get("result"),
    }


def _submit_and_push(sales_invoice_name):
    doc = frappe.get_doc("Sales Invoice", sales_invoice_name)
    if doc.docstatus == 0:
        doc.submit()
        frappe.db.commit()
    elif doc.docstatus == 2:
        frappe.throw("This invoice is cancelled - amend it first, then push the amended version.")

    push_result = _push_invoice_to_magento(doc)
    return {
        "success": True,
        "sales_invoice": doc.name,
        "docstatus": doc.docstatus,
        "magento_push": push_result,
    }


@frappe.whitelist()
def submit_and_push_invoice(sales_invoice):
    """Server-side half of the "Sync Invoice" button - now mainly a manual
    retry/correction path, since new orders are auto-submitted-and-pushed by
    _auto_submit_and_push_invoice below. Still useful if that background job
    failed (e.g. Magento was briefly down), or after a cancel+amend
    correction, where the button reappears on the new draft.
    """
    return _submit_and_push(sales_invoice)


def _auto_submit_and_push_invoice(si_name):
    """Background job enqueued by import_sales_order right after creating
    the draft invoice - submits it and pushes the PDF to Magento
    automatically, with no manual button click needed. Runs as its own job
    (not inline in the request) so a slow PDF render or Magento call never
    delays the response Magento gets back for the order push itself.

    Failures here are logged, not raised - the order/invoice were already
    created successfully by the time this runs, so a push failure shouldn't
    look like the order itself failed. Use the "Sync Invoice" button to
    retry manually once the underlying issue (e.g. Magento downtime) is
    resolved.
    """
    logger = frappe.logger("sales_order_api", allow_site=True, file_count=14)
    logger.setLevel("INFO")
    try:
        result = _submit_and_push(si_name)
        logger.info(f"Auto-submit-and-push succeeded for {si_name}: {result}")
    except Exception as e:
        frappe.db.rollback()
        logger.error(f"Auto-submit-and-push failed for {si_name}: {e}")
        frappe.log_error(title="Auto push to Magento failed", message=f"{si_name}: {e}")
