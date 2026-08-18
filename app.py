#!/usr/bin/env python3
"""Tough Shots Tournament Suite.

A single, guided front end for the four tools in this project:
1. Payment reconciliation
2. Paid/demographic matching
3. Division creation
4. Tournament scoring and bracket management

The existing processing scripts remain usable from the command line.  This UI
runs those scripts directly so their established behavior remains the source of
truth.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from lane_scoring import (
    assign_lanes, create_scoresheet_pdf, fetch_cloud_scores, load_manifest,
    publish_manifest, save_manifest, list_scorers, create_scorer,
    reset_scorer_pin, delete_scorer,
)
from import_archive import archive_imports

try:
    import pandas as pd
except ImportError:  # handled cleanly at startup
    pd = None


APP_TITLE = "Tough Shots Tournament Suite"
BASE_DIR = Path(__file__).resolve().parent
PAYMENT_SCRIPT = BASE_DIR / "PaidEntriesTesting" / "payment_check.py"
DEMOGRAPHIC_SCRIPT = BASE_DIR / "DemographicFormTesting" / "compare_paid_demographics.py"
DIVISION_SCRIPT = BASE_DIR / "DivisionTesting" / "make_tournament_divisions.py"
TOURNAMENT_SCRIPT = BASE_DIR / "TournamentCode" / "bowling_tournament_manager.py"

CSV_TYPES = [("CSV files", "*.csv"), ("All files", "*.*")]


class FilePicker(ttk.Frame):
    def __init__(
        self,
        parent,
        *,
        label: str,
        variable: tk.StringVar,
        choose_command,
        hint: str = "",
        directory: bool = False,
    ):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.variable = variable

        ttk.Label(self, text=label, style="FieldLabel.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        if hint:
            ttk.Label(self, text=hint, style="Hint.TLabel").grid(
                row=1, column=0, sticky="w", pady=(2, 6)
            )
            entry_row = 2
        else:
            entry_row = 1

        row = ttk.Frame(self)
        row.grid(row=entry_row, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)

        ttk.Entry(row, textvariable=variable).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(
            row,
            text="Choose Folder" if directory else "Browse…",
            command=choose_command,
            style="Secondary.TButton",
        ).grid(row=0, column=1)


class WorkflowPage(ttk.Frame):
    def __init__(self, parent, title: str, subtitle: str):
        super().__init__(parent, padding=28)
        self.columnconfigure(0, weight=1)

        ttk.Label(self, text=title, style="PageTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            self,
            text=subtitle,
            style="PageSubtitle.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(5, 20))

        self.body = ttk.Frame(self, style="Card.TFrame", padding=22)
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)


class ToughShotsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x790")
        self.minsize(980, 680)

        self._configure_styles()
        self._busy = False

        default_workspace = BASE_DIR / "TournamentWorkspace"
        self.registration_var = tk.StringVar()
        self.transactions_var = tk.StringVar()
        self.demographics_var = tk.StringVar()
        self.workspace_var = tk.StringVar(value=str(default_workspace))
        self.payment_input_var = tk.StringVar()
        self.demographic_input_var = tk.StringVar()
        self.division_input_var = tk.StringVar()
        self.division_output_var = tk.StringVar(
            value=str(default_workspace / "tournament_divisions")
        )
        self.tournament_roster_var = tk.StringVar()
        self.event_name_var = tk.StringVar(value="Tough Shots Tournament")
        self.lane_count_var = tk.StringVar(value="8")
        self.cloud_url_var = tk.StringVar()
        self.cloud_admin_key_var = tk.StringVar(value=os.environ.get("TOUGHSHOTS_ADMIN_KEY", ""))
        self.lane_manifest_var = tk.StringVar()
        self.lane_pdf_var = tk.StringVar()
        self.auto_sync_var = tk.BooleanVar(value=False)
        self._sync_in_progress = False
        self.status_var = tk.StringVar(value="Ready")

        self.pages = {}
        self.nav_buttons = {}
        self._build_shell()
        self._build_pages()
        self.show_page("overview")
        self._sync_pipeline_paths()

        if pd is None:
            self.after(100, self._show_missing_dependency)

    # ------------------------------------------------------------------
    # Styling / shell
    # ------------------------------------------------------------------
    def _configure_styles(self):
        self.configure(bg="#f4f7fb")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background="#f4f7fb")
        style.configure("Sidebar.TFrame", background="#13233a")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure(
            "Brand.TLabel",
            background="#13233a",
            foreground="#ffffff",
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "BrandSub.TLabel",
            background="#13233a",
            foreground="#aebed2",
            font=("Segoe UI", 9),
        )
        style.configure(
            "PageTitle.TLabel",
            background="#f4f7fb",
            foreground="#152238",
            font=("Segoe UI", 24, "bold"),
        )
        style.configure(
            "PageSubtitle.TLabel",
            background="#f4f7fb",
            foreground="#64748b",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Section.TLabel",
            background="#ffffff",
            foreground="#152238",
            font=("Segoe UI", 13, "bold"),
        )
        style.configure(
            "FieldLabel.TLabel",
            background="#ffffff",
            foreground="#24344d",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Hint.TLabel",
            background="#ffffff",
            foreground="#718096",
            font=("Segoe UI", 9),
        )
        style.configure(
            "CardText.TLabel",
            background="#ffffff",
            foreground="#475569",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Status.TLabel",
            background="#eaf0f8",
            foreground="#334155",
            font=("Segoe UI", 9),
            padding=(10, 7),
        )
        style.configure(
            "Primary.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
            background="#1f6feb",
            foreground="#ffffff",
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1859bd"), ("disabled", "#94a3b8")],
            foreground=[("disabled", "#e2e8f0")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 9),
            padding=(10, 7),
        )
        style.configure(
            "Nav.TButton",
            anchor="w",
            font=("Segoe UI", 10),
            padding=(16, 11),
            background="#13233a",
            foreground="#cbd5e1",
            borderwidth=0,
        )
        style.map(
            "Nav.TButton",
            background=[("active", "#1c3352")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "NavSelected.TButton",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
            padding=(16, 11),
            background="#1f6feb",
            foreground="#ffffff",
            borderwidth=0,
        )

    def _build_shell(self):
        shell = ttk.Frame(self, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", padding=(16, 22))
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.configure(width=230)
        sidebar.grid_propagate(False)

        ttk.Label(sidebar, text="TOUGH SHOTS", style="Brand.TLabel").pack(
            anchor="w", padx=6
        )
        ttk.Label(
            sidebar, text="Tournament operations", style="BrandSub.TLabel"
        ).pack(anchor="w", padx=6, pady=(1, 24))

        nav_items = [
            ("overview", "Overview"),
            ("payments", "1  Payment Check"),
            ("demographics", "2  Demographics"),
            ("divisions", "3  Divisions"),
            ("tournament", "4  Tournament"),
            ("lanes", "5  Lanes + Mobile"),
        ]
        for key, text in nav_items:
            btn = ttk.Button(
                sidebar,
                text=text,
                command=lambda k=key: self.show_page(k),
                style="Nav.TButton",
            )
            btn.pack(fill="x", pady=2)
            self.nav_buttons[key] = btn

        ttk.Separator(sidebar).pack(fill="x", pady=(22, 14))
        ttk.Button(
            sidebar,
            text="Open Workspace",
            command=self.open_workspace,
            style="Nav.TButton",
        ).pack(fill="x")

        main_area = ttk.Frame(shell, style="App.TFrame")
        main_area.grid(row=0, column=1, sticky="nsew")
        main_area.columnconfigure(0, weight=1)
        main_area.rowconfigure(0, weight=1)

        self.page_host = ttk.Frame(main_area, style="App.TFrame")
        self.page_host.grid(row=0, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

        status = ttk.Frame(main_area, style="App.TFrame", padding=(26, 0, 26, 16))
        status.grid(row=1, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="ew"
        )

    def _build_pages(self):
        self.pages["overview"] = self._build_overview_page()
        self.pages["payments"] = self._build_payment_page()
        self.pages["demographics"] = self._build_demographics_page()
        self.pages["divisions"] = self._build_divisions_page()
        self.pages["tournament"] = self._build_tournament_page()
        self.pages["lanes"] = self._build_lanes_page()

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def show_page(self, key: str):
        self.pages[key].tkraise()
        for nav_key, button in self.nav_buttons.items():
            button.configure(
                style="NavSelected.TButton" if nav_key == key else "Nav.TButton"
            )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def _build_overview_page(self):
        page = WorkflowPage(
            self.page_host,
            "Tournament Prep",
            "Choose the three source files once, then run the full preparation pipeline. "
            "Each completed step automatically feeds the next one.",
        )
        body = page.body

        ttk.Label(body, text="Source files", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 14)
        )
        FilePicker(
            body,
            label="Tournament registration CSV",
            variable=self.registration_var,
            choose_command=lambda: self.choose_csv(self.registration_var),
            hint="Google Form registration export used for payment reconciliation.",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 14))
        FilePicker(
            body,
            label="Square transactions CSV",
            variable=self.transactions_var,
            choose_command=lambda: self.choose_csv(self.transactions_var),
            hint="Square export containing completed payment records.",
        ).grid(row=2, column=0, sticky="ew", pady=(0, 14))
        FilePicker(
            body,
            label="Demographic form CSV",
            variable=self.demographics_var,
            choose_command=lambda: self.choose_csv(self.demographics_var),
            hint="Demographic form response export used to confirm eligibility and gender.",
        ).grid(row=3, column=0, sticky="ew", pady=(0, 14))
        FilePicker(
            body,
            label="Workspace folder",
            variable=self.workspace_var,
            choose_command=self.choose_workspace,
            hint="All generated CSVs, division files, and the tournament database stay together here.",
            directory=True,
        ).grid(row=4, column=0, sticky="ew", pady=(0, 18))

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew")
        ttk.Button(
            actions,
            text="Run Full Prep Pipeline",
            command=self.run_all,
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Go to Tournament",
            command=lambda: self.show_page("tournament"),
            style="Secondary.TButton",
        ).pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="Open Imported Files",
            command=self.open_import_archive,
            style="Secondary.TButton",
        ).pack(side="left")
        ttk.Label(
            body,
            text="Every run automatically preserves timestamped copies of the files it consumes in TournamentWorkspace/imported_files, along with a checksum manifest.",
            style="Hint.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=6, column=0, sticky="w", pady=(12, 0))
        return page

    def _build_payment_page(self):
        page = WorkflowPage(
            self.page_host,
            "Payment Check",
            "Match tournament registrations to completed Square payments and produce the existing payment_status.csv and duplicate_review.csv outputs.",
        )
        body = page.body
        FilePicker(
            body,
            label="Tournament registration CSV",
            variable=self.registration_var,
            choose_command=lambda: self.choose_csv(self.registration_var),
        ).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Square transactions CSV",
            variable=self.transactions_var,
            choose_command=lambda: self.choose_csv(self.transactions_var),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Workspace folder",
            variable=self.workspace_var,
            choose_command=self.choose_workspace,
            directory=True,
        ).grid(row=2, column=0, sticky="ew", pady=(0, 20))
        ttk.Button(
            body,
            text="Run Payment Check",
            command=self.run_payment,
            style="Primary.TButton",
        ).grid(row=3, column=0, sticky="w")
        return page

    def _build_demographics_page(self):
        page = WorkflowPage(
            self.page_host,
            "Demographic Matching",
            "Compare payment status against demographic submissions using the project's tolerant name/DOB matching rules.",
        )
        body = page.body
        FilePicker(
            body,
            label="Payment status CSV",
            variable=self.payment_input_var,
            choose_command=lambda: self.choose_csv(self.payment_input_var),
            hint="Defaults to the payment_status.csv created by Step 1.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Demographic form CSV",
            variable=self.demographics_var,
            choose_command=lambda: self.choose_csv(self.demographics_var),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Workspace folder",
            variable=self.workspace_var,
            choose_command=self.choose_workspace,
            directory=True,
        ).grid(row=2, column=0, sticky="ew", pady=(0, 20))
        ttk.Button(
            body,
            text="Run Demographic Match",
            command=self.run_demographics,
            style="Primary.TButton",
        ).grid(row=3, column=0, sticky="w")
        return page

    def _build_divisions_page(self):
        page = WorkflowPage(
            self.page_host,
            "Division Builder",
            "Create U12/U14/U16/U18 division rosters from paid participants with completed demographic forms, including the existing needs-review output.",
        )
        body = page.body
        FilePicker(
            body,
            label="Paid + demographic check CSV",
            variable=self.division_input_var,
            choose_command=lambda: self.choose_csv(self.division_input_var),
            hint="Defaults to the paid_demographic_check.csv created by Step 2.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Division output folder",
            variable=self.division_output_var,
            choose_command=self.choose_division_output,
            directory=True,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 20))
        ttk.Button(
            body,
            text="Build Divisions",
            command=self.run_divisions,
            style="Primary.TButton",
        ).grid(row=2, column=0, sticky="w")
        return page

    def _build_tournament_page(self):
        page = WorkflowPage(
            self.page_host,
            "Tournament Manager",
            "Open the existing scoring and bracket manager with the generated all_divisions.csv roster. Scores continue to auto-save to SQLite and all existing exports remain available.",
        )
        body = page.body
        FilePicker(
            body,
            label="Tournament roster (all_divisions.csv)",
            variable=self.tournament_roster_var,
            choose_command=lambda: self.choose_csv(self.tournament_roster_var),
            hint="Defaults to the roster generated by Step 3. You can also choose any compatible roster manually.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 20))

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="w")
        ttk.Button(
            actions,
            text="Open Tournament Manager",
            command=self.launch_tournament_manager,
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Open Division Folder",
            command=lambda: self.open_path(Path(self.division_output_var.get())),
            style="Secondary.TButton",
        ).pack(side="left", padx=8)

        ttk.Label(
            body,
            text=(
                "The tournament window contains Qualifying, Match Play, and Summary tabs, "
                "plus cut-size settings, re-seeding, automatic byes, winner advancement, "
                "and result exports."
            ),
            style="CardText.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(22, 0))
        return page

    def _build_lanes_page(self):
        page = WorkflowPage(
            self.page_host,
            "Lane Assignment + Mobile Scoring",
            "Randomly balance all bowlers across the available lanes, publish one QR scoring page per lane pair, and create compact six-game score sheets with two lanes per printed page.",
        )
        body = page.body

        FilePicker(
            body,
            label="Tournament roster (all_divisions.csv)",
            variable=self.tournament_roster_var,
            choose_command=lambda: self.choose_csv(self.tournament_roster_var),
            hint="Bowler names are pulled from this roster. Division, age, and gender are ignored for lane assignment.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 14))

        settings = ttk.Frame(body, style="Card.TFrame")
        settings.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Tournament name", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(settings, textvariable=self.event_name_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(settings, text="Available lanes", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Spinbox(settings, textvariable=self.lane_count_var, from_=1, to=200, width=8).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(settings, text="Cloud scoring URL", style="FieldLabel.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(settings, textvariable=self.cloud_url_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(settings, text="Cloud admin key", style="FieldLabel.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(settings, textvariable=self.cloud_admin_key_var, show="•").grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(
            settings,
            text="After deploying the included cloud folder, paste its https://...onrender.com address and the admin key you configured there.",
            style="Hint.TLabel",
            wraplength=650,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="w", pady=(4, 10))
        ttk.Button(actions, text="Prepare Lane Scoring", command=self.prepare_lane_scoring, style="Primary.TButton").pack(side="left")
        ttk.Button(actions, text="Manage Scorer PINs", command=self.manage_scorer_pins, style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(actions, text="Retry Publish", command=self.retry_lane_publish, style="Secondary.TButton").pack(side="left")
        ttk.Button(actions, text="Sync Mobile Scores", command=self.sync_mobile_scores, style="Secondary.TButton").pack(side="left", padx=(8, 0))

        outputs = ttk.Frame(body, style="Card.TFrame")
        outputs.grid(row=3, column=0, sticky="ew", pady=(4, 8))
        outputs.columnconfigure(1, weight=1)
        ttk.Label(outputs, text="Assignment", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(outputs, textvariable=self.lane_manifest_var, state="readonly").grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(outputs, text="Score sheets", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(outputs, textvariable=self.lane_pdf_var, state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Button(outputs, text="Open PDF", command=self.open_lane_pdf, style="Secondary.TButton").grid(row=1, column=2, padx=(8, 0))

        ttk.Checkbutton(
            body,
            text="Auto-sync cloud scores into the Tournament Manager database every 15 seconds",
            variable=self.auto_sync_var,
            command=self._auto_sync_changed,
        ).grid(row=4, column=0, sticky="w", pady=(8, 4))
        ttk.Label(
            body,
            text=(
                "Prepare Lane Scoring performs a new random draw, balances lane counts so they differ by at most one bowler, publishes the lane pages, "
                "writes lane_assignments.csv, and generates a landscape PDF with two lanes and one shared QR code per page. Scorers sign in with individual PINs; scores stay editable and every change is audit-attributed. Re-running the lane draw resets cloud qualifying scores for this tournament session."
            ),
            style="CardText.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))
        return page

    # ------------------------------------------------------------------
    # File selection / path management
    # ------------------------------------------------------------------
    def choose_csv(self, variable: tk.StringVar):
        initial = self._initial_dir(variable.get())
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose CSV file",
            initialdir=initial,
            filetypes=CSV_TYPES,
        )
        if path:
            variable.set(path)

    def choose_workspace(self):
        folder = filedialog.askdirectory(
            parent=self,
            title="Choose tournament workspace",
            initialdir=self._initial_dir(self.workspace_var.get()),
        )
        if folder:
            self.workspace_var.set(folder)
            self._sync_pipeline_paths()

    def choose_division_output(self):
        folder = filedialog.askdirectory(
            parent=self,
            title="Choose division output folder",
            initialdir=self._initial_dir(self.division_output_var.get()),
        )
        if folder:
            self.division_output_var.set(folder)
            self.tournament_roster_var.set(str(Path(folder) / "all_divisions.csv"))

    def _initial_dir(self, value: str):
        path = Path(value).expanduser() if value else BASE_DIR
        if path.is_file():
            return str(path.parent)
        if path.exists():
            return str(path)
        if path.parent.exists():
            return str(path.parent)
        return str(BASE_DIR)

    def _sync_pipeline_paths(self):
        workspace = Path(self.workspace_var.get()).expanduser()
        payment = workspace / "payment_status.csv"
        demographic = workspace / "paid_demographic_check.csv"
        division_dir = workspace / "tournament_divisions"
        roster = division_dir / "all_divisions.csv"

        # Only replace pipeline-derived defaults; never overwrite a deliberate
        # custom selection when the user is working on a single step.
        self.payment_input_var.set(str(payment))
        self.demographic_input_var.set(str(demographic))
        self.division_input_var.set(str(demographic))
        self.division_output_var.set(str(division_dir))
        self.tournament_roster_var.set(str(roster))
        lane_dir = workspace / "lane_scoring"
        self.lane_manifest_var.set(str(lane_dir / "lane_manifest.json"))
        self.lane_pdf_var.set(str(lane_dir / "lane_scoresheets.pdf"))

    def _archive_inputs(self, stage: str, *items):
        """Preserve the exact input files used for a processing stage."""
        workspace = Path(self.workspace_var.get()).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            batch = archive_imports(workspace, stage, items)
            self.status_var.set(f"Inputs archived — {batch.name}")
            return batch
        except Exception as exc:
            messagebox.showerror(
                "Could not archive imported files",
                f"The operation was stopped before processing so the source files would not be used without a saved copy.\n\n{exc}",
                parent=self,
            )
            return None

    def open_import_archive(self):
        path = Path(self.workspace_var.get()).expanduser() / "imported_files"
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    # ------------------------------------------------------------------
    # Running the tools
    # ------------------------------------------------------------------
    def run_payment(self):
        if not self._require_files(
            ("Tournament registration CSV", self.registration_var.get()),
            ("Square transactions CSV", self.transactions_var.get()),
        ):
            return
        if self._busy:
            return
        if not self._archive_inputs(
            "payment_check",
            ("tournament_registration", self.registration_var.get()),
            ("square_transactions", self.transactions_var.get()),
        ):
            return
        self._sync_pipeline_paths_if_workspace_changed()
        workspace = Path(self.workspace_var.get()).expanduser()
        output = workspace / "payment_status.csv"
        command = [
            sys.executable,
            str(PAYMENT_SCRIPT),
            self.registration_var.get(),
            self.transactions_var.get(),
            "--output",
            str(output),
        ]
        self._run_command_async(
            "Payment check",
            command,
            before=lambda: workspace.mkdir(parents=True, exist_ok=True),
            after=lambda: self._after_payment(output),
        )

    def run_demographics(self):
        if not self._require_files(
            ("Payment status CSV", self.payment_input_var.get()),
            ("Demographic form CSV", self.demographics_var.get()),
        ):
            return
        if self._busy:
            return
        if not self._archive_inputs(
            "demographic_match",
            ("payment_status", self.payment_input_var.get()),
            ("demographic_form", self.demographics_var.get()),
        ):
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        output = workspace / "paid_demographic_check.csv"
        command = [
            sys.executable,
            str(DEMOGRAPHIC_SCRIPT),
            self.payment_input_var.get(),
            self.demographics_var.get(),
            "--output",
            str(output),
        ]
        self._run_command_async(
            "Demographic match",
            command,
            before=lambda: workspace.mkdir(parents=True, exist_ok=True),
            after=lambda: self._after_demographics(output),
        )

    def run_divisions(self):
        if not self._require_files(
            ("Paid + demographic check CSV", self.division_input_var.get()),
        ):
            return
        if self._busy:
            return
        if not self._archive_inputs(
            "division_builder",
            ("paid_demographic_check", self.division_input_var.get()),
        ):
            return
        output_dir = Path(self.division_output_var.get()).expanduser()
        command = [
            sys.executable,
            str(DIVISION_SCRIPT),
            self.division_input_var.get(),
            "--output-dir",
            str(output_dir),
        ]
        self._run_command_async(
            "Division builder",
            command,
            before=lambda: output_dir.mkdir(parents=True, exist_ok=True),
            after=lambda: self._after_divisions(output_dir),
        )

    def run_all(self):
        if not self._require_files(
            ("Tournament registration CSV", self.registration_var.get()),
            ("Square transactions CSV", self.transactions_var.get()),
            ("Demographic form CSV", self.demographics_var.get()),
        ):
            return
        if self._busy:
            return
        if not self._archive_inputs(
            "full_prep_pipeline",
            ("tournament_registration", self.registration_var.get()),
            ("square_transactions", self.transactions_var.get()),
            ("demographic_form", self.demographics_var.get()),
        ):
            return

        self._sync_pipeline_paths()
        workspace = Path(self.workspace_var.get()).expanduser()
        payment = workspace / "payment_status.csv"
        demographic = workspace / "paid_demographic_check.csv"
        division_dir = workspace / "tournament_divisions"

        commands = [
            (
                "1 of 3 — Payment check",
                [
                    sys.executable,
                    str(PAYMENT_SCRIPT),
                    self.registration_var.get(),
                    self.transactions_var.get(),
                    "--output",
                    str(payment),
                ],
            ),
            (
                "2 of 3 — Demographic match",
                [
                    sys.executable,
                    str(DEMOGRAPHIC_SCRIPT),
                    str(payment),
                    self.demographics_var.get(),
                    "--output",
                    str(demographic),
                ],
            ),
            (
                "3 of 3 — Division builder",
                [
                    sys.executable,
                    str(DIVISION_SCRIPT),
                    str(demographic),
                    "--output-dir",
                    str(division_dir),
                ],
            ),
        ]

        self._busy = True
        self.status_var.set("Preparing tournament files…")
        workspace.mkdir(parents=True, exist_ok=True)
        division_dir.mkdir(parents=True, exist_ok=True)

        def worker():
            try:
                logs = []
                for label, command in commands:
                    self.after(0, lambda text=label: self.status_var.set(text))
                    completed = self._subprocess_run(command)
                    logs.append(completed.stdout.strip())
                    if completed.returncode != 0:
                        detail = (completed.stderr or completed.stdout).strip()
                        raise RuntimeError(f"{label} failed.\n\n{detail}")
                self.after(0, lambda: self._full_pipeline_complete(payment, demographic, division_dir, logs))
            except Exception as exc:
                self.after(0, lambda err=exc: self._job_failed("Tournament prep", err))

        threading.Thread(target=worker, daemon=True).start()

    def _run_command_async(self, label, command, before=None, after=None):
        if self._busy:
            messagebox.showinfo(
                "Task in progress",
                "Another operation is already running.",
                parent=self,
            )
            return
        self._busy = True
        self.status_var.set(f"{label} running…")
        if before:
            before()

        def worker():
            try:
                completed = self._subprocess_run(command)
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()
                    raise RuntimeError(detail or f"{label} failed.")
                self.after(0, lambda: self._job_complete(label, completed.stdout, after))
            except Exception as exc:
                self.after(0, lambda err=exc: self._job_failed(label, err))

        threading.Thread(target=worker, daemon=True).start()

    def _subprocess_run(self, command):
        kwargs = {
            "capture_output": True,
            "text": True,
            "cwd": str(BASE_DIR),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(command, **kwargs)

    def _job_complete(self, label, stdout, after):
        self._busy = False
        if after:
            after()
        summary = self._compact_log(stdout)
        self.status_var.set(f"{label} complete" + (f" — {summary}" if summary else ""))

    def _job_failed(self, label, exc):
        self._busy = False
        self.status_var.set(f"{label} failed")
        messagebox.showerror(
            f"{label} failed",
            str(exc),
            parent=self,
        )

    def _after_payment(self, output: Path):
        self.payment_input_var.set(str(output))
        self.status_var.set(f"Payment check complete — saved {output.name}")

    def _after_demographics(self, output: Path):
        self.division_input_var.set(str(output))
        self.status_var.set(f"Demographic match complete — saved {output.name}")

    def _after_divisions(self, output_dir: Path):
        roster = output_dir / "all_divisions.csv"
        self.tournament_roster_var.set(str(roster))
        self.status_var.set(f"Division builder complete — roster ready: {roster.name}")

    def _full_pipeline_complete(self, payment, demographic, division_dir, logs):
        self._busy = False
        self.payment_input_var.set(str(payment))
        self.division_input_var.set(str(demographic))
        self.division_output_var.set(str(division_dir))
        roster = division_dir / "all_divisions.csv"
        self.tournament_roster_var.set(str(roster))
        self.status_var.set("Tournament prep complete — all_divisions.csv is ready")

        details = self._pipeline_summary(payment, demographic, division_dir)
        messagebox.showinfo(
            "Tournament Prep Complete",
            "All preparation steps finished successfully.\n\n" + details,
            parent=self,
        )
        self.show_page("tournament")

    def _pipeline_summary(self, payment: Path, demographic: Path, division_dir: Path):
        if pd is None:
            return f"Roster: {division_dir / 'all_divisions.csv'}"
        try:
            payment_df = pd.read_csv(payment)
            demo_df = pd.read_csv(demographic)
            divisions_df = pd.read_csv(division_dir / "all_divisions.csv")
            needs_review = pd.read_csv(division_dir / "needs_review.csv")
            paid = int(payment_df["Status"].astype(str).str.upper().eq("PAID").sum())
            both = int(demo_df["Designation"].astype(str).eq("BOTH - PAID + DEMOGRAPHIC").sum())
            return (
                f"Paid entries: {paid}\n"
                f"Paid + demographic: {both}\n"
                f"Assigned to divisions: {len(divisions_df)}\n"
                f"Needs review: {len(needs_review)}"
            )
        except Exception:
            return f"Roster: {division_dir / 'all_divisions.csv'}"

    @staticmethod
    def _compact_log(text: str):
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _sync_pipeline_paths_if_workspace_changed(self):
        workspace = Path(self.workspace_var.get()).expanduser()
        self.payment_input_var.set(str(workspace / "payment_status.csv"))

    # ------------------------------------------------------------------
    # Lanes / cloud mobile scoring

    def _cloud_credentials(self):
        cloud_url = self.cloud_url_var.get().strip()
        admin_key = self.cloud_admin_key_var.get().strip()
        if not cloud_url or not admin_key:
            raise ValueError("Enter both the cloud scoring URL and cloud admin key first.")
        return cloud_url, admin_key

    def manage_scorer_pins(self):
        try:
            cloud_url, admin_key = self._cloud_credentials()
        except Exception as exc:
            messagebox.showerror("Scorer PINs", str(exc), parent=self)
            return

        win = tk.Toplevel(self)
        win.title("Manage Scorer PINs")
        win.geometry("590x430")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Authorized Scorers", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Each scorer gets a private 6-digit PIN. Their phone stays signed in for the tournament session; saved scores remain editable.", wraplength=540, justify="left").pack(anchor="w", pady=(3, 12))

        tree = ttk.Treeview(frame, columns=("name",), show="headings", height=11, selectmode="browse")
        tree.heading("name", text="Scorer Name")
        tree.column("name", width=470, anchor="w")
        tree.pack(fill="both", expand=True)

        addrow = ttk.Frame(frame)
        addrow.pack(fill="x", pady=(10, 6))
        name_var = tk.StringVar()
        ttk.Entry(addrow, textvariable=name_var).pack(side="left", fill="x", expand=True)

        def refresh():
            try:
                data = list_scorers(cloud_url, admin_key)
                for item in tree.get_children(): tree.delete(item)
                for scorer in data.get("scorers", []):
                    tree.insert("", "end", iid=str(scorer["id"]), values=(scorer["name"],))
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def add():
            try:
                result = create_scorer(cloud_url, admin_key, name_var.get())
                scorer = result["scorer"]
                name_var.set("")
                refresh()
                messagebox.showinfo("Scorer PIN Created", f"Scorer: {scorer['name']}\nPIN: {scorer['pin']}\n\nGive this PIN only to that scorer. It is shown only now; use Reset PIN if it is lost.", parent=win)
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def selected_id():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def reset_pin():
            sid = selected_id()
            if sid is None:
                messagebox.showinfo("Select scorer", "Select a scorer first.", parent=win); return
            try:
                result = reset_scorer_pin(cloud_url, admin_key, sid)
                scorer = result["scorer"]
                messagebox.showinfo("New Scorer PIN", f"Scorer: {scorer['name']}\nNew PIN: {scorer['pin']}\n\nThe old PIN and existing phone sessions are now invalid.", parent=win)
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def remove():
            sid = selected_id()
            if sid is None:
                messagebox.showinfo("Select scorer", "Select a scorer first.", parent=win); return
            name = tree.item(str(sid), "values")[0]
            if not messagebox.askyesno("Remove scorer?", f"Remove {name} and invalidate their phone session?", parent=win): return
            try:
                delete_scorer(cloud_url, admin_key, sid); refresh()
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        ttk.Button(addrow, text="Add Scorer + Generate PIN", command=add, style="Primary.TButton").pack(side="left", padx=(8,0))
        buttons = ttk.Frame(frame); buttons.pack(fill="x", pady=(2,0))
        ttk.Button(buttons, text="Reset Selected PIN", command=reset_pin).pack(side="left")
        ttk.Button(buttons, text="Remove Selected", command=remove).pack(side="left", padx=8)
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        refresh()

    # ------------------------------------------------------------------
    def _lane_folder(self):
        folder = Path(self.workspace_var.get()).expanduser() / "lane_scoring"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def prepare_lane_scoring(self):
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            messagebox.showerror("Roster not found", "Choose an existing all_divisions.csv file first.", parent=self)
            return
        if self._busy:
            return
        try:
            lane_count = int(self.lane_count_var.get())
            cloud_url = self.cloud_url_var.get().strip()
            admin_key = self.cloud_admin_key_var.get().strip()
            event_name = self.event_name_var.get().strip() or "Tough Shots Tournament"
            if not cloud_url or not admin_key:
                raise ValueError("Enter both the cloud scoring URL and cloud admin key.")
        except Exception as exc:
            messagebox.showerror("Lane setup", str(exc), parent=self)
            return

        folder = self._lane_folder()
        existing_manifest = folder / "lane_manifest.json"
        existing_id = None
        if existing_manifest.is_file():
            if not messagebox.askyesno(
                "Create a New Lane Draw?",
                "This will replace the current lane assignment, invalidate the old QR score sheets, and clear cloud qualifying scores for this tournament. Continue?",
                parent=self,
            ):
                return
            try:
                existing_id = load_manifest(existing_manifest).get("tournament_id")
            except Exception:
                existing_id = None

        if not self._archive_inputs(
            "lane_assignment",
            ("tournament_roster", roster),
        ):
            return

        self._busy = True
        self.status_var.set("Randomizing lanes, publishing QR pages, and building score sheets…")

        def worker():
            try:
                manifest = assign_lanes(
                    roster,
                    lane_count,
                    tournament_name=event_name,
                    tournament_id=existing_id,
                )
                manifest_path, assignment_csv = save_manifest(manifest, folder)
                publish_manifest(manifest, cloud_url, admin_key)
                # Save again so published URL/timestamp are retained.
                save_manifest(manifest, folder)
                pdf_path = folder / "lane_scoresheets.pdf"
                create_scoresheet_pdf(manifest, pdf_path, cloud_url)
                self.after(0, lambda: self._lane_prepare_complete(manifest_path, assignment_csv, pdf_path, manifest))
            except Exception as exc:
                self.after(0, lambda err=exc: self._job_failed("Lane scoring setup", err))

        threading.Thread(target=worker, daemon=True).start()

    def _lane_prepare_complete(self, manifest_path, assignment_csv, pdf_path, manifest):
        self._busy = False
        self.lane_manifest_var.set(str(manifest_path))
        self.lane_pdf_var.set(str(pdf_path))
        counts = [len(lane["bowlers"]) for lane in manifest["lanes"]]
        self.status_var.set(
            f"Lane scoring ready — {sum(counts)} bowlers across {len(counts)} lanes ({min(counts)}-{max(counts)} per lane)"
        )
        messagebox.showinfo(
            "Lane Scoring Ready",
            f"Random lane assignment published successfully.\n\n"
            f"Bowlers: {sum(counts)}\nLanes: {len(counts)}\n"
            f"Bowlers per lane: {min(counts)}-{max(counts)}\n\n"
            f"Assignments: {assignment_csv}\nScore sheets: {pdf_path}",
            parent=self,
        )

    def retry_lane_publish(self):
        manifest_path = Path(self.lane_manifest_var.get()).expanduser()
        if not manifest_path.is_file():
            messagebox.showerror("No lane assignment", "Run Prepare Lane Scoring first.", parent=self)
            return
        cloud_url = self.cloud_url_var.get().strip()
        admin_key = self.cloud_admin_key_var.get().strip()
        if not cloud_url or not admin_key:
            messagebox.showerror("Cloud settings", "Enter both the cloud scoring URL and admin key.", parent=self)
            return
        if self._busy:
            return
        self._busy = True
        self.status_var.set("Publishing existing lane assignment…")

        def worker():
            try:
                manifest = load_manifest(manifest_path)
                publish_manifest(manifest, cloud_url, admin_key)
                save_manifest(manifest, manifest_path.parent)
                pdf_path = manifest_path.parent / "lane_scoresheets.pdf"
                create_scoresheet_pdf(manifest, pdf_path, cloud_url)
                self.after(0, lambda: self._retry_publish_complete(pdf_path))
            except Exception as exc:
                self.after(0, lambda err=exc: self._job_failed("Lane publish", err))
        threading.Thread(target=worker, daemon=True).start()

    def _retry_publish_complete(self, pdf_path):
        self._busy = False
        self.lane_pdf_var.set(str(pdf_path))
        self.status_var.set("Lane assignment published — QR score sheets refreshed")

    def sync_mobile_scores(self, silent=False):
        if self._sync_in_progress:
            return
        manifest_path = Path(self.lane_manifest_var.get()).expanduser()
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not manifest_path.is_file() or not roster.is_file():
            if not silent:
                messagebox.showerror("Cannot sync", "Prepare lane scoring and choose the tournament roster first.", parent=self)
            return
        cloud_url = self.cloud_url_var.get().strip()
        admin_key = self.cloud_admin_key_var.get().strip()
        if not cloud_url or not admin_key:
            if not silent:
                messagebox.showerror("Cloud settings", "Enter the cloud scoring URL and admin key.", parent=self)
            return
        self._sync_in_progress = True
        if not silent:
            self.status_var.set("Syncing mobile scores…")

        def worker():
            try:
                manifest = load_manifest(manifest_path)
                result = fetch_cloud_scores(manifest, cloud_url, admin_key)
                from TournamentCode.bowling_tournament_manager import TournamentDB, resolve_database_path
                db_path = resolve_database_path(roster)
                tournament_db = TournamentDB(db_path)
                try:
                    if not tournament_db.roster_loaded():
                        tournament_db.import_roster(roster)
                    imported = 0
                    skipped = 0
                    for item in result.get("scores", []):
                        if tournament_db.bowler(item["bowler_id"]):
                            tournament_db.set_score(item["bowler_id"], item["game_no"], item["score"])
                            imported += 1
                        else:
                            skipped += 1
                finally:
                    tournament_db.close()
                self.after(0, lambda: self._sync_complete(imported, skipped, db_path, silent))
            except Exception as exc:
                self.after(0, lambda err=exc: self._sync_failed(err, silent))
        threading.Thread(target=worker, daemon=True).start()

    def _sync_complete(self, imported, skipped, db_path, silent):
        self._sync_in_progress = False
        self.status_var.set(f"Mobile sync complete — {imported} score cells imported")
        if not silent:
            extra = f"\nSkipped unknown bowlers: {skipped}" if skipped else ""
            messagebox.showinfo(
                "Mobile Scores Synced",
                f"Imported {imported} qualifying score cells into:\n{db_path}{extra}\n\nIf the Tournament Manager is already open, click Refresh Mobile Scores on its Qualifying tab.",
                parent=self,
            )

    def _sync_failed(self, exc, silent):
        self._sync_in_progress = False
        self.status_var.set(f"Mobile sync failed — {exc}")
        if not silent:
            messagebox.showerror("Mobile sync failed", str(exc), parent=self)

    def _auto_sync_changed(self):
        if self.auto_sync_var.get():
            self.sync_mobile_scores(silent=True)
            self.after(15000, self._auto_sync_tick)

    def _auto_sync_tick(self):
        if not self.auto_sync_var.get():
            return
        self.sync_mobile_scores(silent=True)
        self.after(15000, self._auto_sync_tick)

    def open_lane_pdf(self):
        path = Path(self.lane_pdf_var.get()).expanduser()
        if not path.is_file():
            messagebox.showerror("PDF not found", "Prepare lane scoring first.", parent=self)
            return
        self.open_path(path)

    # ------------------------------------------------------------------
    # Tournament / OS helpers
    # ------------------------------------------------------------------
    def launch_tournament_manager(self):
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            messagebox.showerror(
                "Roster not found",
                "Choose an existing all_divisions.csv file or complete Step 3 first.",
                parent=self,
            )
            return
        if not self._archive_inputs(
            "tournament_manager",
            ("tournament_roster", roster),
        ):
            return
        try:
            kwargs = {"cwd": str(BASE_DIR)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                [sys.executable, str(TOURNAMENT_SCRIPT), str(roster)],
                **kwargs,
            )
            self.status_var.set(f"Tournament Manager opened with {roster.name}")
        except Exception as exc:
            messagebox.showerror(
                "Could not open Tournament Manager",
                str(exc),
                parent=self,
            )

    def open_workspace(self):
        path = Path(self.workspace_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    def open_path(self, path: Path):
        path = Path(path).expanduser()
        if not path.exists():
            messagebox.showerror("Not found", f"Could not find:\n{path}", parent=self)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Could not open location", str(exc), parent=self)

    def _require_files(self, *items):
        missing = []
        for label, value in items:
            if not value or not Path(value).expanduser().is_file():
                missing.append(label)
        if missing:
            messagebox.showerror(
                "Missing input",
                "Please choose:\n\n• " + "\n• ".join(missing),
                parent=self,
            )
            return False
        return True

    def _show_missing_dependency(self):
        messagebox.showerror(
            "Missing dependency",
            "This application requires pandas. Install the project requirements with:\n\n"
            "python -m pip install -r requirements.txt",
            parent=self,
        )


def main():
    app = ToughShotsApp()
    app.mainloop()


if __name__ == "__main__":
    main()
