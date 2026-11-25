"""Magento -> ERPNext Purchase Order import endpoint.

The purchasing-side counterpart to sales_order_api.py's import_sales_order -
same overall shape (Magento's admin has a "Push to ERP" action that POSTs
one completed Purchase Order's full data here in one call), but deliberately
narrower in scope: this endpoint creates and submits ONLY the Purchase
Order itself - no Purchase Invoice or Purchase Receipt is auto-created.
There is no "auto-submit a draft financial document" step here at all, by
design, not something paused or removed.

Design notes:
- Items are created/updated INLINE via import_item from api.py, exactly
  like the Sales Order side, matched by sku. Also enriched via
  fetch_product_by_sku (the same Magento catalog lookup the Sales Order
  side uses) - added 2026-10-23, since a brand-new Item created from a
  Purchase Order is the same shared Item master a Sales Order would later
  reference, and leaving it unenriched just because a PO happened to be
  the first document to create it was an inconsistency, not a deliberate
  design choice. Same as the Sales side, a failed/empty lookup never
  blocks the order - it just falls back to the PO payload's own fields.
- Vendor (Supplier) matching is BY EXACT NAME first, not by email/phone
  Contact lookup (confirmed incident 2026-10-27: Magento sends a generic,
  shared "info@tyrescart.ae" placeholder email on many Fitment/Delivery POs
  whose vendor has no real email on file - 354 of 699 synced POs had
  silently piled onto one Supplier, "Lookin Tyres", because email-first
  matching treated them all as the same vendor). This site's Supplier
  naming rule is "Supplier Name" (confirmed via Supplier.autoname), so
  Supplier.name is always exactly the vendor name already - checking
  frappe.db.exists("Supplier", vendor_name) directly is both simpler and
  more reliable than Contact matching: vendor_name was present and
  character-for-character consistent on all 699 synced POs, unlike
  vendor_code (6 blanks) or email (the generic-placeholder problem).
  There is deliberately no email/phone fallback - see
  _get_or_create_supplier's own docstring for why that was tried and
  reverted. _link_doc and _get_or_create_address are reused directly from
  sales_order_api.py rather than duplicated, since they're already
  party-doctype-agnostic.
- The Contact's own name comes from vendor.contact_person (a real person,
  e.g. "Mr Arun") - not vendor.name, which is the vendor's company name,
  a different thing. supplier_type is always hardcoded to "Company"
  regardless of payload content (confirmed, not payload-driven).
- Idempotent on custom_website_po_number, NOT custom_website_order_number -
  one Magento customer order can split into several independent Purchase
  Orders (confirmed with the Magento dev 2026-10-27: MPO = buying the
  product, FPO = paying the installer, DPO = courier/delivery booking -
  PPO, internal payment tracking, is never sent to this endpoint at all).
  Each of those arrives as its own POST with a distinct po_number but the
  SAME order_number (the shared parent order), so order_number must stay a
  plain reference field, not the uniqueness key - keying on it would reject
  the 2nd/3rd PO for the same order as "already exists" and silently drop
  them.
"""

import json

import frappe
from frappe.utils import getdate, nowdate

from erpnext.controllers.accounts_controller import get_default_taxes_and_charges

from klever_integrations.api import import_item
from klever_integrations.magento_sync import fetch_product_by_sku
from klever_integrations.sales_order_api import VAT_EXEMPT_BEFORE_DATE, _get_or_create_address, _link_doc

DEFAULT_COMPANY = "DSP Trade Hub FZ-LLC"
DEFAULT_CURRENCY = "AED"
# Not hardcoded to a specific template name - always resolves to whichever
# Purchase Taxes and Charges Template is marked default for the order's
# company (currently "UAE VAT 5% - DSP"), mirroring the same mechanism used
# on the Sales Order side.
DEFAULT_TAX_MASTER_DOCTYPE = "Purchase Taxes and Charges Template"
DEFAULT_SUPPLIER_TYPE = "Company"
# Same gap hit on the Sales Order side: a stock Item with no warehouse set
# fails ERPNext's submission validation ("Warehouse is mandatory for stock
# Item"). Assigning this explicitly on every line sidesteps that regardless
# of whether the item is new or already exists - same single real warehouse
# this Company has for both sales and purchases.
DEFAULT_WAREHOUSE = "Main Warehouse - DSP"


# ---------------------------------------------------------------------------
# Supplier + Contact + Address
# ---------------------------------------------------------------------------

def _get_or_create_supplier(vendor_data, order_number=None):
    """vendor_data: {"name", "code", "contact_person", "email", "phone",
    "whatsapp_number", "address", "city", "vat_no", "vat_applicable"}

    Matched by EXACT Supplier name ONLY (see module docstring for the
    2026-10-27 "Lookin Tyres" incident this replaced email-first matching
    to prevent) - Supplier.name already equals vendor_data["name"] exactly
    on this site (Supplier Naming By = "Supplier Name"), so this is a
    direct, unambiguous lookup, not a heuristic.

    Deliberately NO email/phone Contact fallback here (tried first, then
    reverted - confirmed by testing: since the generic "info@tyrescart.ae"
    placeholder is shared by many different *brand-new* vendors, not just
    renamed existing ones, any such fallback reopens the exact same
    collision for every first-time vendor that happens to share it with an
    already-onboarded one). Email/phone is still recorded on the Contact
    for reference - it's just never used to decide Supplier identity.
    """
    email = vendor_data.get("email")
    phone = vendor_data.get("phone")
    supplier_name = vendor_data.get("name") or f"Vendor ({order_number or 'unknown order'})"
    # The Contact represents a person (contact_person, e.g. "Mr Arun"), not
    # the vendor's own company name - those are different things here,
    # unlike the Sales side where customer.name already is a person.
    contact_person_name = vendor_data.get("contact_person") or supplier_name

    def _backfill(name):
        # Backfill tax_id/vendor_code if this Supplier was created before
        # the Magento payload carried them (e.g. this vendor's first PO
        # predates that field being sent) - same reasoning as
        # _get_or_create_customer backfilling custom_is_installer on an
        # existing match rather than only at creation time.
        vat_no = vendor_data.get("vat_no")
        if vat_no and not frappe.db.get_value("Supplier", name, "tax_id"):
            frappe.db.set_value("Supplier", name, "tax_id", vat_no)
        code = vendor_data.get("code")
        if code and not frappe.db.get_value("Supplier", name, "custom_website_vendor_code"):
            frappe.db.set_value("Supplier", name, "custom_website_vendor_code", code)

    if frappe.db.exists("Supplier", supplier_name):
        _backfill(supplier_name)
        return supplier_name

    # Supplier is named directly from supplier_name here (this site's
    # "Supplier Naming By" default is "Supplier Name", confirmed via
    # Supplier.autoname), so the name is already known before insert even
    # completes. Two POs referencing the same not-yet-seen vendor (e.g. a
    # shared installer used across many different orders) can both pass the
    # "doesn't exist yet" checks above before either commits - same race
    # class already fixed for Make/Model/Year/Item (see api.py's
    # _ensure_link_value and sales_order_api._get_or_create_vehicle for the
    # original confirmed incidents, 2026-10-26). Confirmed here too:
    # "Duplicate entry 'Tire.ae'/'Al Sewar Tyres & Oil LLC' for
    # tabSupplier.PRIMARY", 2026-10-27, once multiple POs per order made
    # concurrent syncs of the same vendor common.
    supplier_existed_before = frappe.db.exists("Supplier", supplier_name)
    supplier = frappe.get_doc({
        "doctype": "Supplier",
        "supplier_name": supplier_name,
        "supplier_type": DEFAULT_SUPPLIER_TYPE,
        "tax_id": vendor_data.get("vat_no"),
        "custom_website_vendor_code": vendor_data.get("code"),
    })
    supplier.insert(ignore_permissions=True, ignore_if_duplicate=True)
    frappe.db.commit()

    if supplier_existed_before:
        # Genuinely pre-existing vendor that just didn't match via email/
        # phone this time (e.g. no contact info sent on this PO) - reuse its
        # existing Contact if it has one, rather than creating a duplicate.
        existing_contact = frappe.db.get_value(
            "Dynamic Link", {"link_doctype": "Supplier", "link_name": supplier.name, "parenttype": "Contact"},
            "parent",
        )
        if existing_contact:
            return supplier.name

    contact = frappe.get_doc({
        "doctype": "Contact",
        "first_name": contact_person_name,
    })
    if email:
        contact.append("email_ids", {"email_id": email, "is_primary": 1})
    if phone:
        contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
    whatsapp_number = vendor_data.get("whatsapp_number")
    if whatsapp_number:
        contact.append("phone_nos", {"phone": whatsapp_number, "is_primary_mobile_no": 0})
    _link_doc(contact, "Supplier", supplier.name)
    try:
        contact.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Lost the same race on the Contact's own name - a concurrent
        # request already created one for this supplier in the meantime.
        frappe.db.rollback()

    _get_or_create_address(vendor_data, "Supplier", supplier.name, "Billing")

    return supplier.name


# ---------------------------------------------------------------------------
# Item enrichment - same Magento catalog lookup the Sales Order side uses
# (see sales_order_api._normalize_order_item for the original reasoning).
# ---------------------------------------------------------------------------

def _normalize_po_item(item):
    item = dict(item)
    sku = item.pop("sku", None)
    if sku:
        item["item_code"] = sku

    real_sku = item.get("item_code")
    catalog = None
    if real_sku:
        try:
            catalog = fetch_product_by_sku(real_sku)
        except Exception as e:
            frappe.log_error(
                title="Magento product lookup failed (PO item)",
                message=f"sku={real_sku}: {e}",
            )

    if catalog:
        result = catalog
        for k, v in item.items():
            if v not in (None, "") and k not in result:
                result[k] = v
    else:
        result = item

    # Always force the item enabled for order-triggered creation/updates -
    # see sales_order_api._normalize_order_item for the full incident/
    # reasoning (2026-10-24) and why popping alone isn't enough for an
    # already-existing, already-disabled item. Disabling obsolete items is
    # legitimate for the nightly full catalog sync only, not for
    # order-import paths that must never block an already-placed real
    # order from being recorded.
    result.pop("product_online", None)
    result["disabled"] = 0

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def import_purchase_order(order=None, **kwargs):
    if order is None:
        order = kwargs
    if isinstance(order, str):
        order = json.loads(order)
    order = frappe._dict(order)

    order_number = order.get("order_number")
    if not order_number:
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": "Missing required field: order_number"}

    po_number = order.get("po_number")
    if not po_number:
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": "Missing required field: po_number"}

    if not order.get("items"):
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": "Missing required field: items (at least one line item is required)"}

    # Exclude cancelled Purchase Orders - same reasoning as the Sales Order
    # side's cancel+amend exclusion. Keyed on po_number, not order_number -
    # see the module docstring for why (one order can split into several
    # independent POs sharing the same order_number).
    existing_po = frappe.db.get_value(
        "Purchase Order", {"custom_website_po_number": po_number, "docstatus": ["!=", 2]}, "name"
    )
    if existing_po:
        return {
            "success": True,
            "already_exists": True,
            "purchase_order": existing_po,
            "note": "This po_number already has a Purchase Order. Resending does not "
                    "currently update an already-created order.",
        }

    try:
        po_items = [_normalize_po_item(item) for item in order.get("items", [])]

        item_results = []
        for item in po_items:
            result = import_item(item=item)
            item_results.append({
                "item_code": item.get("item_code"),
                "success": result.get("success"),
                "error": result.get("error"),
            })
            if not result.get("success"):
                frappe.db.rollback()
                frappe.local.response.http_status_code = 417
                return {"success": False, "error": f"Item import failed: {result.get('error')}", "item_results": item_results}

        supplier_name = _get_or_create_supplier(order.get("vendor", {}), order_number)

        # "Required By" (schedule_date) is po_date - a separate thing from
        # the supplier invoice's own invoice_date, per explicit instruction
        # (2026-10-24). The "Date" field (transaction_date) below instead
        # comes from supplier_invoice.invoice_date.
        #
        # ERPNext hard-validates schedule_date >= transaction_date on every
        # Purchase Order (buying_controller.py, "Required By cannot be
        # before Date") - and a real supplier invoice is routinely dated
        # AFTER the po_date (PO raised first, invoice follows), which trips
        # that check and blocks the sync entirely (confirmed incident
        # 2026-10-27). Rather than touching that core validation (it's a
        # legitimate, system-wide rule for every PO, not just Magento's),
        # Required By only shifts forward to match invoice_date when
        # invoice_date is later - otherwise it stays exactly po_date as
        # originally intended.
        po_date = order.get("po_date") or nowdate()
        supplier_invoice = order.get("supplier_invoice") or {}
        invoice_date = supplier_invoice.get("invoice_date") or po_date
        required_by_date = max(getdate(po_date), getdate(invoice_date))
        company = order.get("company", DEFAULT_COMPANY)
        # Same VAT_EXEMPT_BEFORE_DATE cutoff as the Sales Order side (shared
        # constant, not a duplicated date - one place to change if the lead
        # ever updates it). Compared against invoice_date since that's this
        # PO's transaction_date (see the "Date" field note above) - the same
        # semantic field the Sales Order side compares order_date against.
        # Confirmed gap fixed 2026-11-03: this previously applied VAT to
        # every Purchase Order unconditionally, even ones dated before UAE
        # VAT became mandatory for this business, unlike Sales Orders from
        # the same period which were already correctly exempt.
        tax_defaults = {}
        if getdate(invoice_date) >= VAT_EXEMPT_BEFORE_DATE:
            tax_defaults = get_default_taxes_and_charges(DEFAULT_TAX_MASTER_DOCTYPE, company=company) or {}

        po = frappe.get_doc({
            "doctype": "Purchase Order",
            "company": company,
            "supplier": supplier_name,
            "transaction_date": invoice_date,
            "schedule_date": required_by_date,
            "currency": order.get("currency", DEFAULT_CURRENCY),
            "custom_website_order_number": order_number,
            "custom_website_po_number": order.get("po_number"),
            "custom_invoice_number": supplier_invoice.get("invoice_number"),
            # The comment moved to a top-level "comment" field on the order
            # payload (confirmed 2026-10-27) - supplier_invoice.remarks is
            # kept as a fallback for any older-format payload that still
            # only sends it there.
            "custom_order_comment": order.get("comment") or supplier_invoice.get("remarks"),
            "taxes_and_charges": tax_defaults.get("taxes_and_charges"),
            "taxes": tax_defaults.get("taxes") or [],
            "items": [
                {
                    "item_code": item.get("item_code"),
                    "qty": item.get("qty", 1),
                    "rate": item.get("rate"),
                    "schedule_date": required_by_date,
                    "warehouse": DEFAULT_WAREHOUSE,
                }
                for item in po_items
            ],
        })
        po.insert(ignore_permissions=True)
        po.submit()

        # Supplier invoice PDF - Magento now sends a direct file_url instead
        # of base64 content (confirmed 2026-10-27), so this is stored as a
        # reference File doc (like the Item image URL), not downloaded/
        # re-hosted - same pattern, just linked to the PO's Attachments
        # instead of a single Attach Image field.
        invoice_file = supplier_invoice.get("file") or {}
        file_url = invoice_file.get("file_url")
        if file_url:
            frappe.get_doc({
                "doctype": "File",
                "file_url": file_url,
                "file_name": invoice_file.get("name") or file_url.rsplit("/", 1)[-1],
                "attached_to_doctype": "Purchase Order",
                "attached_to_name": po.name,
                "is_private": 0,
            }).insert(ignore_permissions=True)

        frappe.db.commit()

        return {
            "success": True,
            "purchase_order": po.name,
            "supplier": supplier_name,
            "item_results": item_results,
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.local.response.http_status_code = 417
        return {"success": False, "error": str(e)}
