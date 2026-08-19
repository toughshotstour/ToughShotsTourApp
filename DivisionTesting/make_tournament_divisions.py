#!/usr/bin/env python3
"""
Create tournament divisions from a paid/demographic status CSV.

A participant is eligible only when:
    Paid_Entry == YES
    Demographic_Entry == YES
    and, if present:
    Designation == BOTH - PAID + DEMOGRAPHIC

Division rules from the supplied chart:

    Date of Birth              Division
    -------------------------------------
    08/01/2014 or later        U12 Mixed
    08/01/2012 - 07/31/2014    U14
    08/01/2010 - 07/31/2012    U16
    08/01/2008 - 07/31/2010    U18

Gender rules:
    U12 is MIXED — gender does not restrict participation.
    U14, U16, and U18 are split into Boys and Girls.

Generated files:
    tournament_divisions/
        all_divisions.csv
        U12_Mixed.csv
        U14_Boys.csv
        U14_Girls.csv
        U16_Boys.csv
        U16_Girls.csv
        U18_Boys.csv
        U18_Girls.csv
        needs_review.csv

Usage:
    python make_tournament_divisions.py "paid_demographic_check(2).csv"

Optional:
    python make_tournament_divisions.py input.csv --output-dir tournament_divisions
"""

import argparse
from pathlib import Path
import re

import pandas as pd


# ------------------------------------------------------------
# Division date configuration
#
# These are intentionally kept together so they are easy to change
# in future tournament years.
# ------------------------------------------------------------
U12_START = pd.Timestamp("2014-08-01")  # This date or later

U14_START = pd.Timestamp("2012-08-01")
U14_END   = pd.Timestamp("2014-07-31")

U16_START = pd.Timestamp("2010-08-01")
U16_END   = pd.Timestamp("2012-07-31")

U18_START = pd.Timestamp("2008-08-01")
U18_END   = pd.Timestamp("2010-07-31")


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize_gender(value):
    """
    Normalize common gender labels.

    U12 does not require Boy/Girl because it is mixed.
    U14/U16/U18 require Boy or Girl to determine the final division.
    """
    text = clean_text(value).casefold()

    if text in {"boy", "boys", "male", "m"}:
        return "Boy"

    if text in {"girl", "girls", "female", "f"}:
        return "Girl"

    return clean_text(value)


def parse_birthdate(value):
    """
    Parse a birthdate and repair obvious short-year entries such as:
        9/27/0010 -> 9/27/2010
        8/19/0009 -> 8/19/2009
    """
    if pd.isna(value):
        return pd.NaT

    raw = str(value).strip()

    if not raw:
        return pd.NaT

    short_year_match = re.fullmatch(
        r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s*",
        raw,
    )

    if short_year_match:
        month, day, year = map(int, short_year_match.groups())

        if year < 100:
            year += 2000

        try:
            return pd.Timestamp(
                year=year,
                month=month,
                day=day,
            )
        except ValueError:
            pass

    return pd.to_datetime(raw, errors="coerce")


def get_age_division(dob):
    """
    Return U12, U14, U16, U18, or None when outside the allowed ranges.
    """
    if pd.isna(dob):
        return None

    if dob >= U12_START:
        return "U12"

    if U14_START <= dob <= U14_END:
        return "U14"

    if U16_START <= dob <= U16_END:
        return "U16"

    if U18_START <= dob <= U18_END:
        return "U18"

    return None


def get_final_division(age_division, gender):
    """
    Convert age division + gender into the actual tournament division.
    """
    if age_division == "U12":
        return "U12 Mixed"

    if age_division in {"U14", "U16", "U18"}:
        if gender == "Boy":
            return f"{age_division} Boys"

        if gender == "Girl":
            return f"{age_division} Girls"

    return None


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "input_file",
        help="Combined paid/demographic CSV",
    )

    parser.add_argument(
        "--output-dir",
        default="tournament_divisions",
        help="Output folder (default: tournament_divisions)",
    )

    args = parser.parse_args()

    input_path = Path(args.input_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path)

    # ------------------------------------------------------------
    # Validate required columns.
    # ------------------------------------------------------------
    required = {
        "First_Name",
        "Last_Name",
        "Gender",
        "Paid_Entry",
        "Demographic_Entry",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            f"Input file is missing columns: {sorted(missing)}"
        )

    if (
        "Payment_DOB" not in df.columns
        and "Demographic_DOB" not in df.columns
    ):
        raise ValueError(
            "Input file must contain Payment_DOB and/or Demographic_DOB."
        )

    # ------------------------------------------------------------
    # Only people with BOTH a paid entry and demographic form
    # are eligible.
    # ------------------------------------------------------------
    paid = (
        df["Paid_Entry"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .eq("YES")
    )

    demographic = (
        df["Demographic_Entry"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
        .eq("YES")
    )

    eligible_mask = paid & demographic

    if "Designation" in df.columns:
        both_designation = (
            df["Designation"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.upper()
            .eq("BOTH - PAID + DEMOGRAPHIC")
        )

        eligible_mask = eligible_mask & both_designation

    eligible = df.loc[eligible_mask].copy()

    # ------------------------------------------------------------
    # Normalize gender.
    # ------------------------------------------------------------
    eligible["Gender"] = eligible["Gender"].map(normalize_gender)

    # ------------------------------------------------------------
    # Determine the DOB used for division placement.
    #
    # Payment_DOB is preferred because it comes from the tournament
    # entry record. Demographic_DOB is used as a fallback.
    # ------------------------------------------------------------
    if "Payment_DOB" in eligible.columns:
        eligible["_payment_dob"] = eligible["Payment_DOB"].map(
            parse_birthdate
        )
    else:
        eligible["_payment_dob"] = pd.NaT

    if "Demographic_DOB" in eligible.columns:
        eligible["_demographic_dob"] = eligible["Demographic_DOB"].map(
            parse_birthdate
        )
    else:
        eligible["_demographic_dob"] = pd.NaT

    eligible["Birthdate_Used"] = eligible["_payment_dob"].fillna(
        eligible["_demographic_dob"]
    )

    eligible["Birthdate_Source"] = eligible.apply(
        lambda row: (
            "Payment_DOB"
            if pd.notna(row["_payment_dob"])
            else (
                "Demographic_DOB"
                if pd.notna(row["_demographic_dob"])
                else ""
            )
        ),
        axis=1,
    )

    # Keep DOB discrepancies visible for review.
    eligible["DOB_Discrepancy_Flag"] = (
        eligible["_payment_dob"].notna()
        & eligible["_demographic_dob"].notna()
        & (eligible["_payment_dob"] != eligible["_demographic_dob"])
    )

    # ------------------------------------------------------------
    # Assign age division and final division.
    # ------------------------------------------------------------
    eligible["Age_Division"] = eligible["Birthdate_Used"].map(
        get_age_division
    )

    eligible["Division"] = eligible.apply(
        lambda row: get_final_division(
            row["Age_Division"],
            row["Gender"],
        ),
        axis=1,
    )

    # ------------------------------------------------------------
    # Anything without a final division goes to needs_review.csv.
    #
    # Examples:
    # - DOB older than the U18 range
    # - Missing/unreadable DOB
    # - U14/U16/U18 participant without Boy/Girl gender
    #
    # U12 does NOT require a Boy/Girl value because it is mixed.
    # ------------------------------------------------------------
    assigned = eligible[
        eligible["Division"].notna()
    ].copy()

    needs_review = eligible[
        eligible["Division"].isna()
    ].copy()

    def review_reason(row):
        if pd.isna(row["Birthdate_Used"]):
            return "Missing or invalid birthdate"

        if row["Age_Division"] is None or pd.isna(row["Age_Division"]):
            if row["Birthdate_Used"] < U18_START:
                return "Born before U18 eligibility range"

            return "Birthdate does not fit a configured division"

        if row["Age_Division"] in {"U14", "U16", "U18"}:
            if row["Gender"] not in {"Boy", "Girl"}:
                return (
                    f"{row['Age_Division']} requires Boy/Girl "
                    "for division placement"
                )

        return "Could not assign division"

    if not needs_review.empty:
        needs_review["Review_Reason"] = needs_review.apply(
            review_reason,
            axis=1,
        )
    else:
        needs_review["Review_Reason"] = pd.Series(dtype=str)

    # ------------------------------------------------------------
    # Make dates readable.
    # ------------------------------------------------------------
    for frame in (assigned, needs_review):
        frame["Birthdate_Used"] = pd.to_datetime(
            frame["Birthdate_Used"],
            errors="coerce",
        ).dt.strftime("%m/%d/%Y")

    # Remove internal helper columns.
    helper_columns = [
        "_payment_dob",
        "_demographic_dob",
    ]

    assigned = assigned.drop(
        columns=[
            col for col in helper_columns
            if col in assigned.columns
        ]
    )

    needs_review = needs_review.drop(
        columns=[
            col for col in helper_columns
            if col in needs_review.columns
        ]
    )

    # ------------------------------------------------------------
    # Put useful fields first.
    # ------------------------------------------------------------
    preferred_columns = [
        "First_Name",
        "Last_Name",
        "Gender",
        "Birthdate_Used",
        "Age_Division",
        "Division",
        "Payment_DOB",
        "Demographic_DOB",
        "DOB_Discrepancy_Flag",
        "Birthdate_Source",
        "Designation",
        "Paid_Entry",
        "Demographic_Entry",
    ]

    assigned_columns = [
        col for col in preferred_columns if col in assigned.columns
    ] + [
        col for col in assigned.columns if col not in preferred_columns
    ]

    assigned = assigned[assigned_columns]

    # ------------------------------------------------------------
    # Sort and save the master file.
    # ------------------------------------------------------------
    division_order = {
        "U12 Mixed": 1,
        "U14 Boys": 2,
        "U14 Girls": 3,
        "U16 Boys": 4,
        "U16 Girls": 5,
        "U18 Boys": 6,
        "U18 Girls": 7,
    }

    assigned["_division_sort"] = assigned["Division"].map(
        division_order
    )

    assigned = assigned.sort_values(
        [
            "_division_sort",
            "Birthdate_Used",
            "Last_Name",
            "First_Name",
        ],
        kind="stable",
    ).drop(columns=["_division_sort"])

    assigned.to_csv(
        output_dir / "all_divisions.csv",
        index=False,
    )

    # ------------------------------------------------------------
    # Save all seven division files.
    # ------------------------------------------------------------
    division_files = {
        "U12 Mixed": "U12_Mixed.csv",
        "U14 Boys": "U14_Boys.csv",
        "U14 Girls": "U14_Girls.csv",
        "U16 Boys": "U16_Boys.csv",
        "U16 Girls": "U16_Girls.csv",
        "U18 Boys": "U18_Boys.csv",
        "U18 Girls": "U18_Girls.csv",
    }

    for division, filename in division_files.items():
        division_df = assigned[
            assigned["Division"] == division
        ].copy()

        division_df.to_csv(
            output_dir / filename,
            index=False,
        )

    needs_review.to_csv(
        output_dir / "needs_review.csv",
        index=False,
    )

    # ------------------------------------------------------------
    # Console summary.
    # ------------------------------------------------------------
    print()
    print(f"Eligible paid + demographic participants: {len(eligible)}")
    print()
    print("DIVISION COUNTS:")

    for division in division_files:
        count = int(
            (assigned["Division"] == division).sum()
        )
        print(f"  {division}: {count}")

    print()
    print(f"Assigned to a division: {len(assigned)}")
    print(f"Needs review: {len(needs_review)}")
    print()
    print("DATE RULES:")
    print("  U12 Mixed: 08/01/2014 or later")
    print("  U14:       08/01/2012 - 07/31/2014")
    print("  U16:       08/01/2010 - 07/31/2012")
    print("  U18:       08/01/2008 - 07/31/2010")
    print()
    print(f"Files saved in: {output_dir.resolve()}")


if __name__ == "__main__":
    main()