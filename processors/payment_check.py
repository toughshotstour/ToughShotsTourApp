#!/usr/bin/env python3
"""
Compare a tournament registration CSV with a Square transactions CSV.

Outputs:
  1. payment_status.csv
     One row per bowler with PAID/UNPAID status.
     Square customer names are NOT included here.

  2. duplicate_review.csv
     Only bowlers with multiple form submissions and/or multiple completed
     Square payments. This file DOES include Square_Customer_Names.

Usage:
    python payment_check_with_duplicates.py registrations.csv transactions.csv

Optional:
    python payment_check_with_duplicates.py registrations.csv transactions.csv --output payment_status.csv
"""

import argparse
from pathlib import Path
import pandas as pd


def clean_text(series):
    return series.fillna("").astype(str).str.strip()


def join_unique(values):
    seen = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.append(value)
    return "; ".join(seen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("registrations", help="Google Form registration CSV")
    parser.add_argument("transactions", help="Square transactions CSV")
    parser.add_argument(
        "--output",
        default="payment_status.csv",
        help="Output CSV filename (default: payment_status.csv)",
    )
    args = parser.parse_args()

    registrations = pd.read_csv(args.registrations)
    transactions = pd.read_csv(args.transactions)

    registration_required = {
        "Bowlers First Name",
        "Bowlers Last Name",
        "Bowlers Date of Birth",
        "Payable Order ID",
    }
    transaction_required = {
        "Order Reference ID",
        "Transaction Status",
        "Event Type",
    }

    missing_reg = registration_required - set(registrations.columns)
    missing_txn = transaction_required - set(transactions.columns)

    if missing_reg:
        raise ValueError(
            f"Registration file is missing columns: {sorted(missing_reg)}"
        )
    if missing_txn:
        raise ValueError(
            f"Transaction file is missing columns: {sorted(missing_txn)}"
        )

    # ------------------------------------------------------------
    # 1. Completed Square payments
    # ------------------------------------------------------------
    transaction_status = clean_text(
        transactions["Transaction Status"]
    ).str.casefold()

    event_type = clean_text(
        transactions["Event Type"]
    ).str.casefold()

    completed_payments = transactions[
        transaction_status.eq("complete")
        & event_type.eq("payment")
    ].copy()

    completed_payments["_order_id"] = clean_text(
        completed_payments["Order Reference ID"]
    )

    # Use a unique Square payment identifier.
    if "Payment ID" in completed_payments.columns:
        completed_payments["_payment_id"] = clean_text(
            completed_payments["Payment ID"]
        )
    elif "Transaction ID" in completed_payments.columns:
        completed_payments["_payment_id"] = clean_text(
            completed_payments["Transaction ID"]
        )
    else:
        completed_payments["_payment_id"] = (
            completed_payments.index.astype(str)
        )

    # Pull Square customer names.
    if "Customer Name" in completed_payments.columns:
        completed_payments["_customer_name"] = clean_text(
            completed_payments["Customer Name"]
        )
    else:
        completed_payments["_customer_name"] = ""

    # Map each order ID to its actual completed Square payments
    # and customer names.
    order_to_payment_ids = {}
    order_to_customer_names = {}

    for order_id, group in completed_payments[
        completed_payments["_order_id"].ne("")
    ].groupby("_order_id"):

        payment_ids = set(
            group.loc[
                group["_payment_id"].ne(""),
                "_payment_id",
            ]
        )

        customer_names = set(
            group.loc[
                group["_customer_name"].ne(""),
                "_customer_name",
            ]
        )

        order_to_payment_ids[order_id] = payment_ids
        order_to_customer_names[order_id] = customer_names

    paid_order_ids = set(order_to_payment_ids.keys())

    # ------------------------------------------------------------
    # 2. Registration rows
    # ------------------------------------------------------------
    registrations["_order_id"] = clean_text(
        registrations["Payable Order ID"]
    )

    registrations["Square Paid"] = registrations["_order_id"].isin(
        paid_order_ids
    )

    # Honor manual Paid entries from the form if present.
    if "Column 13" in registrations.columns:
        registrations["Manual Paid"] = (
            clean_text(registrations["Column 13"])
            .str.casefold()
            .eq("paid")
        )
    else:
        registrations["Manual Paid"] = False

    registrations["Row Paid"] = (
        registrations["Square Paid"]
        | registrations["Manual Paid"]
    )

    # Build a person key from bowler name + DOB.
    first = clean_text(registrations["Bowlers First Name"])
    last = clean_text(registrations["Bowlers Last Name"])

    dob_parsed = pd.to_datetime(
        registrations["Bowlers Date of Birth"],
        errors="coerce",
    )

    dob_key = dob_parsed.dt.strftime("%Y-%m-%d").fillna(
        clean_text(registrations["Bowlers Date of Birth"])
    )

    registrations["_person_key"] = (
        first.str.casefold()
        + "|"
        + last.str.casefold()
        + "|"
        + dob_key
    )

    if "Email Address" in registrations.columns:
        registrations["_email"] = clean_text(
            registrations["Email Address"]
        )
    else:
        registrations["_email"] = ""

    # ------------------------------------------------------------
    # 3. One summary row per bowler
    # ------------------------------------------------------------
    output_rows = []

    for person_key, group in registrations.groupby("_person_key"):
        order_ids = set(
            group.loc[
                group["_order_id"].ne(""),
                "_order_id",
            ]
        )

        square_payment_ids = set()
        square_customer_names = set()
        paid_orders = set()

        max_payments_on_one_order = 0

        for order_id in order_ids:
            payment_ids = order_to_payment_ids.get(order_id, set())
            customer_names = order_to_customer_names.get(order_id, set())

            if payment_ids:
                paid_orders.add(order_id)
                square_payment_ids.update(payment_ids)
                square_customer_names.update(customer_names)

                max_payments_on_one_order = max(
                    max_payments_on_one_order,
                    len(payment_ids),
                )

        form_submissions = len(group)
        square_payment_count = len(square_payment_ids)
        paid_order_count = len(paid_orders)

        form_duplicate = form_submissions > 1
        square_duplicate = square_payment_count > 1
        same_order_multi_payment = max_payments_on_one_order > 1

        if form_duplicate and square_duplicate:
            duplicate_type = (
                "MULTIPLE FORM SUBMISSIONS + MULTIPLE SQUARE PAYMENTS"
            )
        elif form_duplicate:
            duplicate_type = "MULTIPLE FORM SUBMISSIONS ONLY"
        elif square_duplicate:
            duplicate_type = "MULTIPLE SQUARE PAYMENTS ONLY"
        else:
            duplicate_type = ""

        duplicate_notes = []

        if form_duplicate:
            duplicate_notes.append(
                f"{form_submissions} form submissions"
            )

        if square_duplicate:
            duplicate_notes.append(
                f"{square_payment_count} distinct Square payments received"
            )

        if paid_order_count > 1:
            duplicate_notes.append(
                f"{paid_order_count} distinct paid order IDs"
            )

        if same_order_multi_payment:
            duplicate_notes.append(
                "at least one single order ID has multiple Square payments"
            )

        paid = bool(group["Row Paid"].max())

        payment_evidence = []

        if bool(group["Square Paid"].max()):
            payment_evidence.append("Square completed payment")

        if bool(group["Manual Paid"].max()):
            payment_evidence.append("Manual Paid marker")

        if not payment_evidence:
            payment_evidence.append("No completed payment found")

        output_rows.append(
            {
                "First_Name": group.iloc[0]["Bowlers First Name"],
                "Last_Name": group.iloc[0]["Bowlers Last Name"],
                "Date_of_Birth": group.iloc[0]["Bowlers Date of Birth"],
                "Status": "PAID" if paid else "UNPAID",
                "Payment_Evidence": "; ".join(payment_evidence),

                "Form_Submissions": form_submissions,
                "Square_Payment_Count": square_payment_count,
                "Paid_Order_Count": paid_order_count,

                "Form_Duplicate_Flag": form_duplicate,
                "Square_Duplicate_Payment_Flag": square_duplicate,
                "Duplicate_Type": duplicate_type,
                "Duplicate_Notes": "; ".join(duplicate_notes),

                # Internal field: exported ONLY to duplicate_review.csv.
                "_Square_Customer_Names": "; ".join(
                    sorted(square_customer_names)
                ),

                "Email": join_unique(group["_email"]),
                "Order_IDs": join_unique(group["_order_id"]),
            }
        )

    result = pd.DataFrame(output_rows)

    result = result.sort_values(
        [
            "Square_Duplicate_Payment_Flag",
            "Form_Duplicate_Flag",
            "Status",
            "Last_Name",
            "First_Name",
        ],
        ascending=[False, False, True, True, True],
    )

    # ------------------------------------------------------------
    # 4. Main output: NO Square customer names
    # ------------------------------------------------------------
    main_output = result.drop(
        columns=["_Square_Customer_Names"]
    )

    main_output.to_csv(args.output, index=False)

    # ------------------------------------------------------------
    # 5. Duplicate review: INCLUDE Square customer names
    # ------------------------------------------------------------
    output_path = Path(args.output)
    duplicate_output = output_path.with_name(
        "duplicate_review.csv"
    )

    duplicates = result[
        result["Form_Duplicate_Flag"]
        | result["Square_Duplicate_Payment_Flag"]
    ].copy()

    duplicates = duplicates.rename(
        columns={
            "_Square_Customer_Names": "Square_Customer_Names"
        }
    )

    # Put the Square customer name near the bowler's name so it's easy to see.
    duplicate_columns = [
        "First_Name",
        "Last_Name",
        "Square_Customer_Names",
        "Date_of_Birth",
        "Status",
        "Form_Submissions",
        "Square_Payment_Count",
        "Paid_Order_Count",
        "Duplicate_Type",
        "Duplicate_Notes",
        "Payment_Evidence",
        "Email",
        "Order_IDs",
        "Form_Duplicate_Flag",
        "Square_Duplicate_Payment_Flag",
    ]

    duplicates = duplicates[duplicate_columns]
    duplicates.to_csv(duplicate_output, index=False)

    # ------------------------------------------------------------
    # 6. Console summary
    # ------------------------------------------------------------
    paid = main_output[main_output["Status"] == "PAID"]
    unpaid = main_output[main_output["Status"] == "UNPAID"]

    form_only = main_output[
        main_output["Duplicate_Type"].eq(
            "MULTIPLE FORM SUBMISSIONS ONLY"
        )
    ]

    square_only = main_output[
        main_output["Duplicate_Type"].eq(
            "MULTIPLE SQUARE PAYMENTS ONLY"
        )
    ]

    both = main_output[
        main_output["Duplicate_Type"].eq(
            "MULTIPLE FORM SUBMISSIONS + MULTIPLE SQUARE PAYMENTS"
        )
    ]

    print()
    print(f"Paid: {len(paid)}")
    print(f"Unpaid: {len(unpaid)}")
    print()
    print(
        "Multiple form submissions only: "
        f"{len(form_only)}"
    )
    print(
        "Multiple Square payments only: "
        f"{len(square_only)}"
    )
    print(
        "Multiple form submissions AND multiple Square payments: "
        f"{len(both)}"
    )
    print()
    print(f"Saved: {args.output}")
    print(f"Duplicate review: {duplicate_output}")
    print()

    print("DUPLICATE REVIEW:")
    if duplicates.empty:
        print("  None")
    else:
        for _, row in duplicates.iterrows():
            customer_names = (
                row["Square_Customer_Names"]
                if row["Square_Customer_Names"]
                else "No Square customer name"
            )

            print(
                f"  {row['First_Name']} {row['Last_Name']} — "
                f"{row['Duplicate_Type']} — "
                f"Square customer(s): {customer_names}"
            )


if __name__ == "__main__":
    main()