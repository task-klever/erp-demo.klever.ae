"""Run on the NEW live site AFTER cleanup_test_data.run() has wiped
Make/Model/Year, to repopulate them from the original reference data.

Prerequisite: makes.sql and models.sql (the raw phpMyAdmin dumps this
project's Make/Model data was originally imported from - table schemas
`makes(id, make_slug, name, name_en, logo, logo_local)` and
`models(id, make_id, model_slug, name, name_en, year_ranges)`) must already
be loaded into this site's own database as plain (non-"tab"-prefixed)
tables, e.g.:

    mysql -h <host> -P <port> -u <user> -p'<password>' <dbname> < makes.sql
    mysql -h <host> -P <port> -u <user> -p'<password>' <dbname> < models.sql

This script reads those two plain tables, builds the correct Make/Model/
Year Frappe records from them (verified against the test site's exact
field mapping - confirmed by independently parsing models.sql's
year_ranges and getting an exact match: min 1983, max 2027, 43 distinct
years, identical to the test site's live Year table), then drops the two
scratch tables so they don't linger in the site's database afterward.

Field mapping (confirmed against the test site):
    Make.make_name    = makes.name
    Make.slug         = makes.make_slug
    Model.model_name  = models.name
    Model.make        = Make linked via models.make_id -> makes.id -> makes.name
    Model.year_ranges = models.year_ranges, stored as-is (raw JSON array string)
    Model.combined_slug = f"{make_slug}-{model_slug}"
    Model autoname    = f"{make}-{model_name}" (format:{make}-{model_name})
    Year.year_name    = every individual year covered by any model's
                        year_ranges (ranges like "2017-2026" expanded;
                        single years like "2020" taken as-is)

Usage:
    bench --site <your-new-site-name> execute klever_integrations.import_reference_data.run

(copy this file into apps/klever_integrations/klever_integrations/ first,
or run its contents via `bench console` instead - either works)
"""

import json

import frappe

FORBIDDEN_SITES = {"erp.klever.ae"}


def run():
    if frappe.local.site in FORBIDDEN_SITES:
        raise RuntimeError(
            f"Refusing to run: '{frappe.local.site}' is the source test site this script "
            "was written on. Run this only on the NEW live site, after loading "
            "makes.sql/models.sql into its database."
        )

    # frappe.db.table_exists() prepends "tab" (it's for doctypes, e.g. checks
    # for `tabmakes`) - these are plain scratch tables, so check raw table
    # names via get_tables() instead.
    existing_tables = frappe.db.get_tables(cached=False)
    if "makes" not in existing_tables or "models" not in existing_tables:
        raise RuntimeError(
            "The plain `makes`/`models` tables aren't present in this site's database yet. "
            "Load makes.sql and models.sql into it first (see this file's docstring), "
            "then re-run this script."
        )

    make_rows = frappe.db.sql(
        "SELECT id, make_slug, name FROM makes", as_dict=True
    )
    make_id_to_name = {}
    created_makes = 0
    for r in make_rows:
        if not frappe.db.exists("Make", r.name):
            frappe.get_doc({
                "doctype": "Make",
                "make_name": r.name,
                "slug": r.make_slug,
            }).insert(ignore_permissions=True)
            created_makes += 1
        make_id_to_name[r.id] = r.name

    model_rows = frappe.db.sql(
        "SELECT id, make_id, model_slug, name, year_ranges FROM models", as_dict=True
    )
    all_years = set()
    created_models = 0
    skipped_models = 0
    for r in model_rows:
        make_name = make_id_to_name.get(r.make_id)
        if not make_name:
            skipped_models += 1
            continue

        make_slug = frappe.db.get_value("Make", make_name, "slug")
        combined_slug = f"{make_slug}-{r.model_slug}"
        doc_name = f"{make_name}-{r.name}"

        if not frappe.db.exists("Model", doc_name):
            frappe.get_doc({
                "doctype": "Model",
                "model_name": r.name,
                "make": make_name,
                "year_ranges": r.year_ranges,
                "combined_slug": combined_slug,
            }).insert(ignore_permissions=True)
            created_models += 1

        try:
            ranges = json.loads(r.year_ranges) if r.year_ranges else []
        except (TypeError, ValueError):
            ranges = []
        for rng in ranges:
            rng = rng.strip()
            if "-" in rng:
                start, end = rng.split("-")
                all_years.update(range(int(start), int(end) + 1))
            elif rng:
                all_years.add(int(rng))

    created_years = 0
    for y in sorted(all_years):
        if not frappe.db.exists("Year", str(y)):
            frappe.get_doc({"doctype": "Year", "year_name": str(y)}).insert(ignore_permissions=True)
            created_years += 1

    frappe.db.commit()

    # Clean up the scratch tables now that the data is in Frappe's own
    # tabMake/tabModel/tabYear. `models` has a foreign key on `makes`
    # (models_ibfk_1), so it must be dropped first.
    frappe.db.sql_ddl("DROP TABLE IF EXISTS `models`")
    frappe.db.sql_ddl("DROP TABLE IF EXISTS `makes`")
    frappe.db.commit()

    print(
        f"Created {created_makes} Make, {created_models} Model "
        f"({skipped_models} skipped - no matching make_id), "
        f"{created_years} Year record(s). Scratch tables dropped."
    )
