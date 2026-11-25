import csv
import frappe


def run():
    csv_path = "/home/tyrescartaccounts/htdocs/accounts.tyrescart.ae/synced_orders 2.csv"
    with open(csv_path, encoding="utf-8-sig") as f:
        csv_by_order = {}
        for row in csv.DictReader(f):
            csv_by_order.setdefault(row["order_number"], row)

    erp_orders = {
        r.custom_website_order_number: r
        for r in frappe.db.get_all(
            "Sales Order",
            fields=["name", "customer", "custom_website_order_number", "transaction_date",
                    "total", "grand_total", "taxes_and_charges", "docstatus"],
        )
    }

    csv_set = set(csv_by_order.keys())
    erp_set = set(erp_orders.keys())

    print("CSV unique orders:", len(csv_set))
    print()
    print("=== In CSV but NOT in ERP ===")
    missing = csv_set - erp_set
    print(missing or "None")

    print()
    print("=== VAT check: any of these 49 orders charged VAT when they shouldn't be? ===")
    vat_issues = 0
    for order_num in csv_set & erp_set:
        so = erp_orders[order_num]
        if so.taxes_and_charges or (so.total and so.grand_total and float(so.total) != float(so.grand_total)):
            vat_issues += 1
            print(f"VAT APPLIED (shouldn't be): {so.name} ({order_num}) total={so.total} grand_total={so.grand_total} taxes_and_charges={so.taxes_and_charges}")
    print(f"VAT issues: {vat_issues}")

    print()
    print("=== Amount consistency check (payment_amount vs grand_total) ===")
    amount_mismatches = 0
    for order_num in csv_set & erp_set:
        so = erp_orders[order_num]
        csv_row = csv_by_order[order_num]
        csv_amount = float(csv_row["payment_amount"]) if csv_row["payment_amount"] else None
        if csv_amount is not None and so.grand_total is not None and abs(float(so.grand_total) - csv_amount) > 0.5:
            amount_mismatches += 1
            print(f"AMOUNT MISMATCH: {so.name} ({order_num}) ERP grand_total={so.grand_total} CSV payment_amount={csv_amount}")
    print(f"Amount mismatches: {amount_mismatches}")

    print()
    print("=== docstatus check (all should be submitted) ===")
    not_submitted = [so.name for so in erp_orders.values() if so.custom_website_order_number in csv_set and so.docstatus != 1]
    print(not_submitted or "All submitted")
