"""Run ONCE on the NEW live site, after `bench restore` from the backup in
this same folder, and BEFORE going live / enabling the Magento order-push
endpoint.

Full scope (per explicit instruction): make the site look like a genuinely
fresh ERPNext install - zero business/reference records, just the schema
and custom functionality we built (Custom Fields, Property Setters, Print
Format, Client Script, the Make/Model/Year/Customer Vehicle doctypes
themselves, the Sales Order API, the Magento sync code - none of that is
touched, it all comes from the DB restore + app install, not from data).

Deletes, in dependency order: Payment Entry, Sales Invoice, Sales Order,
any remaining GL Entry / Stock Ledger Entry, Bin, Customer Vehicle, every
Address, every Contact, Customer, Item Price, Item, Payment Method,
Shipping Method, Make, Model, Year, Company (which cascades deleting its
own Account/Cost Center/Warehouse/Budget/Party Account automatically, once
GL Entry/Stock Ledger Entry are gone - see erpnext's own
Company.on_trash()), Letter Head, and every User except Administrator/Guest.

Left untouched: stock ERPNext framework defaults that ship with every
fresh install and aren't "records" in the client's sense - Currency, UOM,
the base Item Group tree, Mode of Payment, standard Price Lists, tax
templates, etc. Also left untouched: all schema/customization doctypes
(Custom Field, Property Setter, Print Format, Client Script) - these ARE
the delivered product, not data to clear.

After this runs, use import_reference_data.py (same folder) with
makes.sql/models.sql to repopulate Make/Model/Year with the real reference
data, minus the stray test entries that were mixed into it during testing.

Usage:
    bench --site <your-new-site-name> execute klever_integrations.cleanup_test_data.run

(copy this file into apps/klever_integrations/klever_integrations/ first,
or run its contents via `bench console` instead - either works)
"""

import frappe

# Hard safety guard: this must never run against the source test site this
# script was written on. Only ever run it against the new live site's own
# site name, after you've restored the backup there.
FORBIDDEN_SITES = {"erp.klever.ae"}

KEEP_USERS = {"Administrator", "Guest"}

# Frappe caps writes at ~200,000 per uncommitted transaction
# (MAX_WRITES_PER_TRANSACTION) - a single commit at the very end of this
# script isn't enough once the Item loop alone covers 17,000+ records
# (each touching several rows via its own child tables/hooks). Committing
# periodically resets that counter, same pattern already used in this
# project's own nightly Magento sync loop (magento_sync.py).
COMMIT_EVERY = 500


def _delete_docs(doctype, names):
    for i, name in enumerate(names, start=1):
        frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
        if i % COMMIT_EVERY == 0:
            frappe.db.commit()
    frappe.db.commit()


def _cancel_and_delete_docs(doctype, rows):
    for i, row in enumerate(rows, start=1):
        if row.docstatus == 1:
            frappe.get_doc(doctype, row.name).cancel()
        frappe.delete_doc(doctype, row.name, ignore_permissions=True, force=True)
        if i % COMMIT_EVERY == 0:
            frappe.db.commit()
    frappe.db.commit()


def _check_not_forbidden_site():
    if frappe.local.site in FORBIDDEN_SITES:
        raise RuntimeError(
            f"Refusing to run: '{frappe.local.site}' is the source test site this script "
            "was written on. This must only be run against the NEW live site, after "
            "restoring the backup there."
        )


def run():
    _check_not_forbidden_site()

    # 1. Payment Entries (must go before the Sales Invoices they reference)
    payment_entries = frappe.get_all("Payment Entry", fields=["name", "docstatus"])
    _cancel_and_delete_docs("Payment Entry", payment_entries)
    print(f"Deleted {len(payment_entries)} Payment Entry record(s)")

    # 2. Sales Invoices
    invoices = frappe.get_all("Sales Invoice", fields=["name", "docstatus"])
    _cancel_and_delete_docs("Sales Invoice", invoices)
    print(f"Deleted {len(invoices)} Sales Invoice record(s)")

    # 3. Sales Orders
    orders = frappe.get_all("Sales Order", fields=["name", "docstatus"])
    _cancel_and_delete_docs("Sales Order", orders)
    print(f"Deleted {len(orders)} Sales Order record(s)")

    # 4. Safety net - any GL Entry / Stock Ledger Entry rows that weren't
    # auto-cleared by cancelling the above. Company.on_trash() later in
    # this script refuses to cascade-delete Account/Warehouse while any of
    # these remain, so this must be genuinely empty before we get there.
    gl_count = frappe.db.count("GL Entry")
    frappe.db.delete("GL Entry")
    sle_count = frappe.db.count("Stock Ledger Entry")
    frappe.db.delete("Stock Ledger Entry")
    frappe.db.commit()
    print(f"Cleared {gl_count} leftover GL Entry row(s), {sle_count} leftover Stock Ledger Entry row(s)")

    # 5. Bin (per Item+Warehouse stock reservation)
    bin_count = frappe.db.count("Bin")
    frappe.db.delete("Bin")
    frappe.db.commit()
    print(f"Deleted {bin_count} Bin row(s)")

    # 6. Customer Vehicles (not submittable - plain delete)
    vehicles = frappe.get_all("Customer Vehicle", pluck="name")
    _delete_docs("Customer Vehicle", vehicles)
    print(f"Deleted {len(vehicles)} Customer Vehicle record(s)")

    # 7. Every Address (including the Company's own - Company is being
    # deleted too in this full wipe, unlike the earlier, narrower version
    # of this script)
    addresses = frappe.get_all("Address", pluck="name")
    _delete_docs("Address", addresses)
    print(f"Deleted {len(addresses)} Address record(s)")

    # 8. Every Contact
    contacts = frappe.get_all("Contact", pluck="name")
    _delete_docs("Contact", contacts)
    print(f"Deleted {len(contacts)} Contact record(s)")

    # 9. Customers
    customers = frappe.get_all("Customer", pluck="name")
    _delete_docs("Customer", customers)
    print(f"Deleted {len(customers)} Customer record(s)")

    # 10. Item Price, then Items themselves (by far the largest loop -
    # ~17,700 records - this is what actually hits the write-count limit)
    price_count = frappe.db.count("Item Price")
    frappe.db.delete("Item Price")
    frappe.db.commit()
    items = frappe.get_all("Item", pluck="name")
    _delete_docs("Item", items)
    print(f"Deleted {len(items)} Item record(s), {price_count} Item Price row(s)")

    # 11. Our own custom masters - Payment Method, Shipping Method
    for dt in ("Payment Method", "Shipping Method"):
        names = frappe.get_all(dt, pluck="name")
        _delete_docs(dt, names)
        print(f"Deleted {len(names)} {dt} record(s)")

    # 12. Make / Model / Year - re-import afterward with
    # import_reference_data.py using the original makes.sql/models.sql
    for dt in ("Model", "Make", "Year"):  # Model before Make (Model links to Make)
        names = frappe.get_all(dt, pluck="name")
        _delete_docs(dt, names)
        print(f"Deleted {len(names)} {dt} record(s)")

    # 13. Company - cascades deleting its own Account/Cost Center/Budget/
    # Party Account/Warehouse automatically (see Company.on_trash()), now
    # that no GL Entry/Stock Ledger Entry remain to block it.
    companies = frappe.get_all("Company", pluck="name")
    _delete_docs("Company", companies)
    print(f"Deleted {len(companies)} Company record(s) (Account/Warehouse/Cost Center cascade automatically)")

    # 14. Letter Head
    letterheads = frappe.get_all("Letter Head", pluck="name")
    _delete_docs("Letter Head", letterheads)
    print(f"Deleted {len(letterheads)} Letter Head record(s)")

    # 15. Users, except Administrator/Guest - the client adds their own
    users = frappe.get_all("User", pluck="name")
    users = [u for u in users if u not in KEEP_USERS]
    _delete_docs("User", users)
    print(f"Deleted {len(users)} User record(s) (kept: {sorted(KEEP_USERS)})")

    print(
        "Done. Framework defaults (Currency, UOM, Item Group tree, Mode of "
        "Payment, tax templates, etc.) and all schema/customizations "
        "(Custom Field, Property Setter, Print Format, Client Script) were "
        "left untouched. Make/Model/Year are now empty - run "
        "import_reference_data.py next to repopulate them."
    )
