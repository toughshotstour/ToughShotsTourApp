#!/usr/bin/env python3
"""
Compare a payment-status CSV with a demographic-form CSV.

The program determines whether each individual has:
  - at least one PAID entry in the payment file, and
  - an entry in the demographic form.

It outputs one combined CSV with a clear Designation for each person.

Designations:
  BOTH - PAID + DEMOGRAPHIC
  PAID ONLY - MISSING DEMOGRAPHIC
  DEMOGRAPHIC + UNPAID PAYMENT ENTRY
  UNPAID PAYMENT ENTRY ONLY

The matcher is intentionally a little tolerant of common form-entry errors:
  - capitalization / punctuation differences
  - apostrophes and extra spaces
  - a nickname embedded in a first name
  - an extra word in a last-name field
  - obvious DOB formatting errors such as year 0010 instead of 2010
  - exact name matches where the DOB differs

Any non-exact match is shown in the Match_Method column so it can be reviewed.

Usage:
    python compare_paid_demographics.py payment_status.csv demographic_form.csv

Optional:
    python compare_paid_demographics.py payment_status.csv demographic_form.csv --output paid_demographic_check.csv
"""

import argparse
import re
import sqlite3
import unicodedata
from pathlib import Path
from difflib import SequenceMatcher

import pandas as pd



def load_demographics(source):
    """Load demographic information from the local master SQLite database or a legacy CSV."""
    path = Path(source)
    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT first_name,last_name,birthdate,gender,usbc_id,email,division_override,bowler_id,jr_gold_status FROM demographics"
            ).fetchall()
        finally:
            conn.close()
        data = []
        for r in rows:
            # Use the manually selected division when present; otherwise leave it blank
            # and let the division builder derive it from DOB/gender as before.
            data.append({
                "Bowlers First Name": r["first_name"],
                "Bowlers Last Name": r["last_name"],
                "Date of birth": r["birthdate"],
                "Gender": r["gender"] or "",
                "USBC ID": r["usbc_id"] or "",
                "Email Address": r["email"] or "",
                "Division": r["division_override"] or "",
                "Bowler ID": r["bowler_id"] or "",
                "Jr Gold Status": r["jr_gold_status"] or "",
            })
        return pd.DataFrame(data, columns=[
            "Bowlers First Name", "Bowlers Last Name", "Date of birth", "Gender",
            "USBC ID", "Email Address", "Division", "Bowler ID", "Jr Gold Status"
        ])
    return pd.read_csv(path)

def clean_text(value):
    """Normalize text for matching."""
    if pd.isna(value):
        return ""

    value = str(value).strip()

    # Remove accents while preserving letters/numbers.
    value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )

    # Treat punctuation, apostrophes, hyphens, etc. as spaces.
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def normalize_dob(value):
    """
    Normalize dates to YYYY-MM-DD when possible.

    Also repairs years such as 0010 -> 2010 and 0009 -> 2009.
    """
    if pd.isna(value):
        return ""

    raw = str(value).strip()

    parsed = pd.to_datetime(raw, errors="coerce")
    if not pd.isna(parsed):
        return parsed.strftime("%Y-%m-%d")

    # Handle dates such as 9/27/0010.
    match = re.fullmatch(
        r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s*",
        raw,
    )

    if match:
        month, day, year = map(int, match.groups())

        if year < 100:
            year += 2000

        try:
            return pd.Timestamp(
                year=year,
                month=month,
                day=day,
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return clean_text(raw)


def join_unique(values):
    """Join unique non-blank values while preserving first-seen order."""
    seen = []

    for value in values:
        if pd.isna(value):
            continue

        value = str(value).strip()

        if value and value not in seen:
            seen.append(value)

    return "; ".join(seen)


def build_person_fields(df, first_col, last_col, dob_col):
    """Add normalized helper columns used for matching."""
    df = df.copy()

    df["_first"] = df[first_col].map(clean_text)
    df["_last"] = df[last_col].map(clean_text)
    df["_dob"] = df[dob_col].map(normalize_dob)

    df["_full_name"] = (
        df["_first"] + " " + df["_last"]
    ).str.strip()

    df["_identity_key"] = (
        df["_first"] + "|" + df["_last"] + "|" + df["_dob"]
    )

    return df


def score_match(payment_row, demo_row):
    """
    Return (score, method) for a possible person match.

    A score of 0 means "do not match".
    Higher scores are preferred.
    """
    pf = payment_row["_first"]
    pl = payment_row["_last"]
    pdob = payment_row["_dob"]
    pfull = payment_row["_full_name"]

    df = demo_row["_first"]
    dl = demo_row["_last"]
    ddob = demo_row["_dob"]
    dfull = demo_row["_full_name"]

    if not pf or not pl:
        return 0, ""

    dob_equal = bool(pdob and ddob and pdob == ddob)

    # Best case: exact normalized name + exact DOB.
    if pf == df and pl == dl:
        if dob_equal:
            return 120, "EXACT NAME + DOB"

        # Exact names are still a strong match even if someone mistyped DOB.
        return 105, "EXACT NAME; DOB DIFFERS"

    payment_tokens = set(pfull.split())
    demo_tokens = set(dfull.split())

    # The remaining fuzzy rules require the DOB to agree.
    if dob_equal:
        # Handles cases where the payment full name appears inside a
        # longer/mis-shifted demographic name field.
        if (
            len(payment_tokens) >= 2
            and payment_tokens.issubset(demo_tokens)
        ):
            return 112, "DOB + NAME TOKENS"

        # Same first name, last name has a small typo or extra word.
        if pf == df:
            last_similarity = SequenceMatcher(
                None,
                pl,
                dl,
            ).ratio()

            if (
                pl in dl.split()
                or dl in pl.split()
                or last_similarity >= 0.75
            ):
                return 108, "DOB + FIRST NAME + SIMILAR LAST"

        # Same last name, first name/nickname has a small variation.
        if pl == dl:
            first_similarity = SequenceMatcher(
                None,
                pf,
                df,
            ).ratio()

            if (
                pf in df.split()
                or df in pf.split()
                or first_similarity >= 0.75
            ):
                return 108, "DOB + LAST NAME + SIMILAR FIRST"

        # Final conservative fuzzy-name fallback, but only when DOB matches.
        full_similarity = SequenceMatcher(
            None,
            pfull,
            dfull,
        ).ratio()

        if full_similarity >= 0.82:
            return 100, "DOB + SIMILAR NAME"

    return 0, ""


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "payment_file",
        help="payment_status.csv from the payment-check program",
    )

    parser.add_argument(
        "demographic_file",
        help="Local master bowler SQLite database (preferred) or legacy demographic CSV",
    )

    parser.add_argument(
        "--output",
        default="paid_demographic_check.csv",
        help="Output filename (default: paid_demographic_check.csv)",
    )

    args = parser.parse_args()

    payments = pd.read_csv(args.payment_file)
    demographics = load_demographics(args.demographic_file)

    # ------------------------------------------------------------
    # Validate expected columns
    # ------------------------------------------------------------
    payment_required = {
        "First_Name",
        "Last_Name",
        "Date_of_Birth",
        "Status",
    }

    demographic_required = {
        "Bowlers First Name",
        "Bowlers Last Name",
        "Date of birth",
    }

    missing_payment = payment_required - set(payments.columns)
    missing_demo = demographic_required - set(demographics.columns)

    if missing_payment:
        raise ValueError(
            "Payment file is missing columns: "
            f"{sorted(missing_payment)}"
        )

    if missing_demo:
        raise ValueError(
            "Local bowler database/demographic source is missing columns: "
            f"{sorted(missing_demo)}"
        )

    # ------------------------------------------------------------
    # Normalize both files
    # ------------------------------------------------------------
    payments = build_person_fields(
        payments,
        "First_Name",
        "Last_Name",
        "Date_of_Birth",
    )

    demographics = build_person_fields(
        demographics,
        "Bowlers First Name",
        "Bowlers Last Name",
        "Date of birth",
    )

    # ------------------------------------------------------------
    # Consolidate duplicate payment rows for the same exact identity.
    # "Has_Paid" is true if ANY matching payment row is PAID.
    # ------------------------------------------------------------
    payments["_is_paid"] = (
        payments["Status"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .eq("PAID")
    )

    if "Email" not in payments.columns:
        payments["Email"] = ""

    payment_people = []

    for identity, group in payments.groupby(
        "_identity_key",
        sort=False,
    ):
        payment_people.append(
            {
                "_identity_key": identity,
                "_first": group.iloc[0]["_first"],
                "_last": group.iloc[0]["_last"],
                "_dob": group.iloc[0]["_dob"],
                "_full_name": group.iloc[0]["_full_name"],
                "Payment_First_Name": group.iloc[0]["First_Name"],
                "Payment_Last_Name": group.iloc[0]["Last_Name"],
                "Payment_DOB": group.iloc[0]["Date_of_Birth"],
                "Payment_Status": join_unique(group["Status"]),
                "Has_Paid": bool(group["_is_paid"].max()),
                "Payment_Email": join_unique(group["Email"]),
                "Payment_Entries": len(group),
            }
        )

    payment_people = pd.DataFrame(payment_people)

    # ------------------------------------------------------------
    # Consolidate duplicate demographic submissions for the same
    # exact normalized identity.
    # ------------------------------------------------------------
    if "Email Address" not in demographics.columns:
        demographics["Email Address"] = ""
    if "Division" not in demographics.columns:
        demographics["Division"] = ""
    if "Bowler ID" not in demographics.columns:
        demographics["Bowler ID"] = ""
    if "Jr Gold Status" not in demographics.columns:
        demographics["Jr Gold Status"] = ""

    demo_people = []

    for identity, group in demographics.groupby(
        "_identity_key",
        sort=False,
    ):
        demo_people.append(
            {
                "_identity_key": identity,
                "_first": group.iloc[0]["_first"],
                "_last": group.iloc[0]["_last"],
                "_dob": group.iloc[0]["_dob"],
                "_full_name": group.iloc[0]["_full_name"],
                "Demo_First_Name": group.iloc[0]["Bowlers First Name"],
                "Demo_Last_Name": group.iloc[0]["Bowlers Last Name"],
                "Demo_DOB": group.iloc[0]["Date of birth"],
                "Gender": join_unique(group["Gender"]),
                "Division_Override": join_unique(group["Division"]),
                "Bowler_ID": join_unique(group["Bowler ID"]),
                "Jr_Gold_Status": join_unique(group["Jr Gold Status"]),
                "Demographic_Email": join_unique(
                    group["Email Address"]
                ),
                "Demographic_Submissions": len(group),
            }
        )

    demo_people = pd.DataFrame(demo_people)

    # ------------------------------------------------------------
    # Build all reasonable payment <-> demographic match candidates.
    # Then choose the strongest matches one-to-one.
    # ------------------------------------------------------------
    candidates = []

    for payment_index, payment_row in payment_people.iterrows():
        for demo_index, demo_row in demo_people.iterrows():
            score, method = score_match(
                payment_row,
                demo_row,
            )

            if score > 0:
                candidates.append(
                    {
                        "score": score,
                        "payment_index": payment_index,
                        "demo_index": demo_index,
                        "method": method,
                    }
                )

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    matched_payment_indexes = set()
    matched_demo_indexes = set()
    matches = []

    for candidate in candidates:
        pi = candidate["payment_index"]
        di = candidate["demo_index"]

        if pi in matched_payment_indexes:
            continue

        if di in matched_demo_indexes:
            continue

        matched_payment_indexes.add(pi)
        matched_demo_indexes.add(di)

        matches.append(candidate)

    # ------------------------------------------------------------
    # Build output rows
    # ------------------------------------------------------------
    output_rows = []

    # Matched people
    for match in matches:
        payment_row = payment_people.loc[
            match["payment_index"]
        ]
        demo_row = demo_people.loc[
            match["demo_index"]
        ]

        if payment_row["Has_Paid"]:
            designation = "BOTH - PAID + DEMOGRAPHIC"
        else:
            designation = (
                "DEMOGRAPHIC + UNPAID PAYMENT ENTRY"
            )

        output_rows.append(
            {
                "First_Name": payment_row["Payment_First_Name"],
                "Last_Name": payment_row["Payment_Last_Name"],
                "Gender": demo_row["Gender"],
                "Division_Override": demo_row.get("Division_Override", ""),
                "Bowler_ID": demo_row.get("Bowler_ID", ""),
                "Jr_Gold_Status": demo_row.get("Jr_Gold_Status", ""),
                "Designation": designation,
                "Paid_Entry": (
                    "YES" if payment_row["Has_Paid"] else "NO"
                ),
                "Demographic_Entry": "YES",
                "Payment_Status": payment_row["Payment_Status"],
                "Payment_DOB": payment_row["Payment_DOB"],
                "Demographic_DOB": demo_row["Demo_DOB"],
                "Match_Method": match["method"],
                "Payment_Email": payment_row["Payment_Email"],
                "Demographic_Email": demo_row["Demographic_Email"],
                "Payment_Entries": payment_row["Payment_Entries"],
                "Demographic_Submissions": demo_row[
                    "Demographic_Submissions"
                ],
                "Demographic_Name_As_Entered": (
                    f"{demo_row['Demo_First_Name']} "
                    f"{demo_row['Demo_Last_Name']}"
                ).strip(),
            }
        )

    # Payment entries with no demographic match
    for payment_index, payment_row in payment_people.iterrows():
        if payment_index in matched_payment_indexes:
            continue

        if payment_row["Has_Paid"]:
            designation = "PAID ONLY - MISSING DEMOGRAPHIC"
        else:
            designation = "UNPAID PAYMENT ENTRY ONLY"

        output_rows.append(
            {
                "First_Name": payment_row["Payment_First_Name"],
                "Last_Name": payment_row["Payment_Last_Name"],
                "Gender": "",
                "Division_Override": "",
                "Bowler_ID": "",
                "Jr_Gold_Status": "",
                "Designation": designation,
                "Paid_Entry": (
                    "YES" if payment_row["Has_Paid"] else "NO"
                ),
                "Demographic_Entry": "NO",
                "Payment_Status": payment_row["Payment_Status"],
                "Payment_DOB": payment_row["Payment_DOB"],
                "Demographic_DOB": "",
                "Match_Method": "NO DEMOGRAPHIC MATCH",
                "Payment_Email": payment_row["Payment_Email"],
                "Demographic_Email": "",
                "Payment_Entries": payment_row["Payment_Entries"],
                "Demographic_Submissions": 0,
                "Demographic_Name_As_Entered": "",
            }
        )

    # Important: demographic-only people are intentionally excluded.
    # A payment/entry record is required for someone to appear in the output.

    result = pd.DataFrame(output_rows)

    # Sort the most important group first.
    designation_order = {
        "BOTH - PAID + DEMOGRAPHIC": 1,
        "PAID ONLY - MISSING DEMOGRAPHIC": 2,
        "DEMOGRAPHIC + UNPAID PAYMENT ENTRY": 3,
        "UNPAID PAYMENT ENTRY ONLY": 4,
    }

    result["_sort"] = result["Designation"].map(
        designation_order
    ).fillna(99)

    result = result.sort_values(
        ["_sort", "Last_Name", "First_Name"],
        kind="stable",
    ).drop(columns=["_sort"])

    # HARD SAFETY RULE:
    # Nobody can appear in the output unless they originated in the
    # payment/entry file. This prevents demographic-only individuals
    # from ever being exported.
    result = result[
        result["Payment_Entries"].fillna(0).astype(int) > 0
    ].copy()

    result.to_csv(args.output, index=False)

    # ------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------
    counts = result["Designation"].value_counts()

    print()
    print(f"Saved: {args.output}")
    print()

    for designation in designation_order:
        print(
            f"{designation}: "
            f"{counts.get(designation, 0)}"
        )

    print()

    non_exact = result[
        ~result["Match_Method"].isin(
            [
                "EXACT NAME + DOB",
                "NO DEMOGRAPHIC MATCH",
                "NO PAYMENT MATCH",
            ]
        )
    ]

    print(
        "Non-exact matches to review: "
        f"{len(non_exact)}"
    )

    if not non_exact.empty:
        print()
        print("REVIEW THESE MATCHES:")

        for _, row in non_exact.iterrows():
            print(
                f"  {row['First_Name']} "
                f"{row['Last_Name']} -> "
                f"{row['Demographic_Name_As_Entered']} "
                f"({row['Match_Method']})"
            )


if __name__ == "__main__":
    main()