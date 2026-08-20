#!/usr/bin/env python3
"""Tough Shots Tournament Suite.

A single, guided front end for the four tools in this project:
1. Payment reconciliation
2. Tournament entries matched to the local master bowler database
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
import webbrowser
import shutil
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from core.lane_scoring import (
    assign_lanes, create_scoresheet_pdf, fetch_cloud_scores, load_manifest,
    publish_manifest, save_manifest, list_scorers, create_scorer,
    reset_scorer_pin, delete_scorer,
)
from core.import_archive import archive_imports
from core.local_demographics import (
    update_from_csv as update_local_demographic_db,
    require_database as require_local_bowler_database,
    count_rows as local_demographic_count,
    list_local_bowlers, add_local_bowler, update_local_bowler, delete_local_bowler,
    set_local_jr_gold_by_bowler_ids, DIVISIONS as LOCAL_DIVISIONS,
)
from core.results_portal import (
    import_bowlers as portal_import_bowlers, list_bowlers as portal_list_bowlers,
    set_jr_gold as portal_set_jr_gold, publish_qualifying as portal_publish_qualifying,
    publish_jr_gold as portal_publish_jr_gold, archive_tournament as portal_archive_tournament,
)

try:
    import pandas as pd
except ImportError:  # handled cleanly at startup
    pd = None


APP_TITLE = "Tough Shots Tournament Suite"
BASE_DIR = Path(__file__).resolve().parent.parent
PAYMENT_SCRIPT = BASE_DIR / "processors" / "payment_check.py"
DEMOGRAPHIC_SCRIPT = BASE_DIR / "processors" / "compare_paid_demographics.py"
DIVISION_SCRIPT = BASE_DIR / "processors" / "make_tournament_divisions.py"
TOURNAMENT_SCRIPT = BASE_DIR / "tournament" / "bowling_tournament_manager.py"

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
        self.event_date_var = tk.StringVar(value=date.today().isoformat())
        self._sync_in_progress = False
        self.status_var = tk.StringVar(value="Ready")

        self.pages = {}
        self.nav_buttons = {}
        self._build_shell()
        self._build_pages()
        self.show_page("overview")
        self._sync_pipeline_paths()
        self._load_workspace_state()
        self.after(250, self._detect_saved_tournament)
        self.protocol("WM_DELETE_WINDOW", self._on_suite_close)

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
            ("demographics", "2  Bowler Database"),
            ("divisions", "3  Divisions"),
            ("tournament", "4  Tournament"),
            ("lanes", "5  Lanes + Mobile"),
            ("results", "6  Bowlers + Results"),
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
        self.pages["results"] = self._build_results_page()

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
            "Choose the registration and Square files, then run the full preparation pipeline. "
            "Demographics come from the reusable local demographic database maintained in Step 2.",
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
            label="Workspace folder",
            variable=self.workspace_var,
            choose_command=self.choose_workspace,
            hint="All generated CSVs, division files, and the tournament database stay together here.",
            directory=True,
        ).grid(row=3, column=0, sticky="ew", pady=(0, 18))

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=4, column=0, sticky="ew")
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
            "Local Master Bowler Database",
            "Import a demographic form only to create or update the master bowler database. Tournament prep, entry checks, divisions, Jr. Gold, and cloud sync use the database itself.",
        )
        body = page.body
        FilePicker(
            body,
            label="Optional demographic form CSV to import",
            variable=self.demographics_var,
            choose_command=lambda: self.choose_csv(self.demographics_var),
            hint="Choose a fresh demographic export when you need to add/update bowlers. You do not need to select it again for every tournament.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        FilePicker(
            body,
            label="Workspace folder",
            variable=self.workspace_var,
            choose_command=self.choose_workspace,
            directory=True,
            hint="The reusable local_demographics.sqlite3 database is stored here and is not removed by tournament reset.",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 18))
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="w")
        ttk.Button(actions, text="Update Master Database from Demographic Form", command=self.update_local_demographics, style="Primary.TButton").pack(side="left")
        ttk.Button(actions, text="Manage Local Bowlers", command=self.manage_local_bowlers, style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(actions, text="Check Entries Against Master Database", command=self.run_demographics, style="Secondary.TButton").pack(side="left")
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
            text="Reload Tournament from Workspace",
            command=self.reload_tournament_from_workspace,
            style="Secondary.TButton",
        ).pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="Open Division Folder",
            command=lambda: self.open_path(Path(self.division_output_var.get())),
            style="Secondary.TButton",
        ).pack(side="left")

        return page

    def _build_lanes_page(self):
        page = WorkflowPage(
            self.page_host,
            "Lane Assignment + Mobile Scoring",
            "Randomly balance bowlers evenly across lane-pair scorecards, publish one QR scoring page per pair, and create compact six-game score sheets.",
        )
        body = page.body

        FilePicker(
            body,
            label="Tournament roster (all_divisions.csv)",
            variable=self.tournament_roster_var,
            choose_command=lambda: self.choose_csv(self.tournament_roster_var),
            hint="Bowler names are pulled from this roster. Pair sizes are balanced globally, with divisions kept together as much as possible.",
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
        return page

    def _build_results_page(self):
        page = WorkflowPage(
            self.page_host,
            "Permanent Bowlers + Public Results",
            "Sync the private permanent cloud bowler database from the reusable local demographic database, manage Jr. Gold status, and publish standings/results.",
        )
        body = page.body
        settings = ttk.Frame(body, style="Card.TFrame")
        settings.grid(row=0, column=0, sticky="ew", pady=(0, 14)); settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Event date", style="FieldLabel.TLabel").grid(row=0,column=0,sticky="w",padx=(0,10),pady=4)
        ttk.Entry(settings, textvariable=self.event_date_var, width=16).grid(row=0,column=1,sticky="w",pady=4)

        ttk.Label(body, text="Private bowler database", style="Section.TLabel").grid(row=1,column=0,sticky="w",pady=(4,8))
        row1=ttk.Frame(body,style="Card.TFrame"); row1.grid(row=2,column=0,sticky="w",pady=(0,14))
        ttk.Button(row1,text="Sync Permanent Bowlers from Local DB",command=self.import_permanent_bowlers,style="Primary.TButton").pack(side="left")
        ttk.Button(row1,text="Manage Bowler JG / Q Status",command=self.manage_permanent_bowlers,style="Secondary.TButton").pack(side="left",padx=8)

        ttk.Label(body, text="Public Render site", style="Section.TLabel").grid(row=3,column=0,sticky="w",pady=(4,8))
        row2=ttk.Frame(body,style="Card.TFrame"); row2.grid(row=4,column=0,sticky="w",pady=(0,12))
        ttk.Button(row2,text="Push Current Qualifying Standings",command=self.push_public_qualifying,style="Primary.TButton").pack(side="left")
        ttk.Button(row2,text="Push Jr. Gold Standings",command=self.push_jr_gold_qualifying,style="Secondary.TButton").pack(side="left",padx=8)
        ttk.Button(row2,text="Archive Tournament + Update BOY Data",command=self.archive_public_tournament,style="Secondary.TButton").pack(side="left",padx=(0,8))
        ttk.Button(row2,text="Open Public Site",command=self.open_public_site,style="Secondary.TButton").pack(side="left")

        ttk.Label(body, text="Next tournament", style="Section.TLabel").grid(row=5,column=0,sticky="w",pady=(8,8))
        row3=ttk.Frame(body,style="Card.TFrame"); row3.grid(row=6,column=0,sticky="w",pady=(0,12))
        ttk.Button(row3,text="Reset for Next Tournament",command=self.reset_for_next_tournament,style="Primary.TButton").pack(side="left")
        ttk.Label(body,text=(
            "When the tournament is finished, archive the results first. Then use Reset for Next Tournament to clear the active tournament while keeping your saved bowler information and past results."
        ),style="CardText.TLabel",wraplength=760,justify="left").grid(row=7,column=0,sticky="w",pady=(8,0))
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
            self._load_workspace_state()
            self._detect_saved_tournament()

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

    def _workspace_state_path(self):
        return Path(self.workspace_var.get()).expanduser() / ".toughshots_active.json"

    def _save_workspace_state(self):
        """Persist non-secret desktop state needed to reconstruct the active event."""
        try:
            path = self._workspace_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "event_name": self.event_name_var.get().strip(),
                "event_date": self.event_date_var.get().strip(),
                "lane_count": self.lane_count_var.get().strip(),
                "cloud_url": self.cloud_url_var.get().strip(),
                "registration": self.registration_var.get().strip(),
                "transactions": self.transactions_var.get().strip(),
                "tournament_roster": self.tournament_roster_var.get().strip(),
                "lane_manifest": self.lane_manifest_var.get().strip(),
                "lane_pdf": self.lane_pdf_var.get().strip(),
            }
            path.write_text(__import__("json").dumps(data, indent=2), encoding="utf-8")
        except Exception:
            # State convenience must never interrupt tournament scoring.
            pass

    def _load_workspace_state(self):
        path = self._workspace_state_path()
        if not path.is_file():
            return
        try:
            data = __import__("json").loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        mapping = [
            ("event_name", self.event_name_var),
            ("event_date", self.event_date_var),
            ("lane_count", self.lane_count_var),
            ("cloud_url", self.cloud_url_var),
            ("registration", self.registration_var),
            ("transactions", self.transactions_var),
            ("tournament_roster", self.tournament_roster_var),
            ("lane_manifest", self.lane_manifest_var),
            ("lane_pdf", self.lane_pdf_var),
        ]
        for key, variable in mapping:
            value = data.get(key)
            if value not in (None, ""):
                variable.set(str(value))

    def _on_suite_close(self):
        self._save_workspace_state()
        self.destroy()

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

    def update_local_demographics(self):
        demo = Path(self.demographics_var.get()).expanduser()
        if not demo.is_file():
            messagebox.showerror("Missing demographic form", "Choose a demographic form CSV to import.", parent=self)
            return
        if self._busy:
            return
        if not self._archive_inputs("local_demographic_update", ("demographic_form", demo)):
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        try:
            result = update_local_demographic_db(workspace, demo)
            self.status_var.set(f"Local demographics updated — {result['created']} new, {result['updated']} refreshed")
            messagebox.showinfo(
                "Local Demographic Database Updated",
                f"New records: {result['created']}\nUpdated records: {result['updated']}\nSkipped: {result['skipped']}\n\nLocal database: {result['database']}\nSnapshot used by tournament prep: {result['snapshot']}",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Demographic update failed", str(exc), parent=self)

    def manage_local_bowlers(self):
        workspace = Path(self.workspace_var.get()).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)

        win = tk.Toplevel(self)
        win.title("Manage Local Bowlers")
        win.geometry("1120x680")
        win.minsize(900, 560)
        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=3)
        outer.columnconfigure(1, weight=2)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text="Local Bowler Database", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(outer, text="Search and correct the local master bowler database. The CSV export is updated automatically as a convenience copy.", wraplength=1000, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 10))

        left = ttk.Frame(outer)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        search_var = tk.StringVar()
        search_row = ttk.Frame(left)
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_row.columnconfigure(0, weight=1)
        ttk.Label(search_row, text="Search").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))

        cols = ("name", "division", "usbc", "bowler_id", "jg")
        tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, label, width in zip(cols, ("Bowler", "Division", "USBC ID", "Bowler ID", "JG"), (190, 110, 110, 110, 45)):
            tree.heading(c, text=label)
            tree.column(c, width=width, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        form = ttk.LabelFrame(outer, text="Bowler Details", padding=12)
        form.grid(row=2, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)

        first_var, last_var = tk.StringVar(), tk.StringVar()
        gender_var, birth_var = tk.StringVar(), tk.StringVar()
        division_var, usbc_var = tk.StringVar(), tk.StringVar()
        bowler_id_var, jg_var, email_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        selected_key = {"value": None}

        def field(row, label, var, widget="entry", values=None, readonly=False):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=5)
            if widget == "combo":
                w = ttk.Combobox(form, textvariable=var, values=values or [], state="readonly")
            else:
                w = ttk.Entry(form, textvariable=var, state="readonly" if readonly else "normal")
            w.grid(row=row, column=1, sticky="ew", pady=5)
            return w

        field(0, "First name", first_var)
        field(1, "Last name", last_var)
        field(2, "Gender", gender_var, "combo", ["Boy", "Girl"])
        field(3, "Birthdate", birth_var)
        field(4, "Division", division_var, "combo", [""] + list(LOCAL_DIVISIONS))
        field(5, "USBC ID", usbc_var)
        field(6, "Bowler ID", bowler_id_var, readonly=True)
        ttk.Label(form, text="Bowler ID stays fixed once generated so past results remain linked.", style="Hint.TLabel", wraplength=340, justify="left").grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 5))
        field(8, "Jr. Gold status", jg_var, "combo", ["", "JG", "Q"])
        field(9, "Email", email_var)

        status = tk.StringVar(value="")
        ttk.Label(form, textvariable=status, style="Hint.TLabel", wraplength=340, justify="left").grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 4))

        def clear_form():
            selected_key["value"] = None
            for v in (first_var, last_var, gender_var, birth_var, division_var, usbc_var, bowler_id_var, jg_var, email_var):
                v.set("")
            tree.selection_remove(tree.selection())
            status.set("Enter details, then choose Add Bowler.")

        def rows_now():
            return list_local_bowlers(workspace, search_var.get())

        def refresh(select_key=None):
            current = select_key or selected_key["value"]
            tree.delete(*tree.get_children())
            for b in rows_now():
                iid = b["identity_key"]
                tree.insert("", "end", iid=iid, values=(f"{b['first_name']} {b['last_name']}", b.get("division") or "", b.get("usbc_id") or "", b.get("bowler_id") or "", b.get("jr_gold_status") or ""))
            if current and tree.exists(current):
                tree.selection_set(current); tree.focus(current); tree.see(current)
            status.set(f"{len(tree.get_children())} bowlers shown")

        def load_selected(_event=None):
            sel = tree.selection()
            if not sel:
                return
            key = sel[0]
            match = next((b for b in list_local_bowlers(workspace) if b["identity_key"] == key), None)
            if not match:
                return
            selected_key["value"] = key
            first_var.set(match.get("first_name") or "")
            last_var.set(match.get("last_name") or "")
            gender_var.set(match.get("gender") or "")
            birth_var.set(match.get("birthdate") or "")
            division_var.set(match.get("division") or "")
            usbc_var.set(match.get("usbc_id") or "")
            bowler_id_var.set(match.get("bowler_id") or "")
            jg_var.set(match.get("jr_gold_status") or "")
            email_var.set(match.get("email") or "")
            status.set("Editing selected local bowler.")

        def payload():
            return dict(first_name=first_var.get(), last_name=last_var.get(), gender=gender_var.get(), birthdate=birth_var.get(), usbc_id=usbc_var.get(), division=division_var.get(), jr_gold_status=jg_var.get(), email=email_var.get())

        def add_record():
            try:
                bid = add_local_bowler(workspace, **payload())
                refresh()
                clear_form()
                status.set(f"Bowler added. Generated Bowler ID: {bid}")
            except Exception as exc:
                messagebox.showerror("Could not add bowler", str(exc), parent=win)

        def save_record():
            key = selected_key["value"]
            if not key:
                messagebox.showinfo("Select a bowler", "Select a bowler to edit, or use Add Bowler for a new record.", parent=win)
                return
            try:
                bid = update_local_bowler(workspace, key, **payload())
                # USBC edits can change identity_key, so locate by the stable Bowler ID afterward.
                match = next((b for b in list_local_bowlers(workspace) if b.get("bowler_id") == bid), None)
                selected_key["value"] = match["identity_key"] if match else None
                refresh(selected_key["value"])
                status.set("Changes saved to the local master bowler database.")
            except Exception as exc:
                messagebox.showerror("Could not save bowler", str(exc), parent=win)

        def remove_record():
            key = selected_key["value"]
            if not key:
                messagebox.showinfo("Select a bowler", "Select the bowler you want to remove.", parent=win)
                return
            name = f"{first_var.get()} {last_var.get()}".strip()
            if not messagebox.askyesno("Remove local bowler", f"Remove {name} from the local bowler database?\n\nThis does not delete already archived tournament results or the cloud permanent-bowler record.", parent=win):
                return
            try:
                delete_local_bowler(workspace, key)
                clear_form(); refresh()
                status.set(f"{name} removed from the local database.")
            except Exception as exc:
                messagebox.showerror("Could not remove bowler", str(exc), parent=win)

        buttons = ttk.Frame(form)
        buttons.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        tk.Button(buttons, text="Add Bowler", command=add_record, padx=12, pady=6).pack(side="left")
        tk.Button(buttons, text="Save Changes", command=save_record, padx=12, pady=6).pack(side="left", padx=6)
        tk.Button(buttons, text="New / Clear", command=clear_form, padx=12, pady=6).pack(side="left")
        tk.Button(buttons, text="Remove Bowler", command=remove_record, padx=12, pady=6).pack(side="right")

        tree.bind("<<TreeviewSelect>>", load_selected)
        search_var.trace_add("write", lambda *_: refresh())
        search_entry.bind("<Escape>", lambda _e: search_var.set(""))
        refresh()

    def run_demographics(self):
        if not self._require_files(("Payment status CSV", self.payment_input_var.get())):
            return
        if self._busy:
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        try:
            local_bowler_db = require_local_bowler_database(workspace)
        except Exception as exc:
            messagebox.showerror("Master bowler database required", str(exc), parent=self)
            return
        if not self._archive_inputs(
            "demographic_match",
            ("payment_status", self.payment_input_var.get()),
            ("local_bowler_database", local_bowler_db),
        ):
            return
        output = workspace / "paid_demographic_check.csv"
        command = [
            sys.executable, str(DEMOGRAPHIC_SCRIPT), self.payment_input_var.get(),
            str(local_bowler_db), "--output", str(output),
        ]
        self._run_command_async(
            "Master database match", command,
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
        ):
            return
        if self._busy:
            return
        self._sync_pipeline_paths()
        workspace = Path(self.workspace_var.get()).expanduser()
        try:
            local_bowler_db = require_local_bowler_database(workspace)
        except Exception as exc:
            messagebox.showerror("Master bowler database required", str(exc), parent=self)
            self.show_page("demographics")
            return
        if not self._archive_inputs(
            "full_prep_pipeline",
            ("tournament_registration", self.registration_var.get()),
            ("square_transactions", self.transactions_var.get()),
            ("local_bowler_database", local_bowler_db),
        ):
            return

        payment = workspace / "payment_status.csv"
        demographic = workspace / "paid_demographic_check.csv"
        division_dir = workspace / "tournament_divisions"
        commands = [
            ("1 of 3 — Payment check", [sys.executable, str(PAYMENT_SCRIPT), self.registration_var.get(), self.transactions_var.get(), "--output", str(payment)]),
            ("2 of 3 — Check entries against master database", [sys.executable, str(DEMOGRAPHIC_SCRIPT), str(payment), str(local_bowler_db), "--output", str(demographic)]),
            ("3 of 3 — Division builder", [sys.executable, str(DIVISION_SCRIPT), str(demographic), "--output-dir", str(division_dir)]),
        ]
        self._busy = True
        self.status_var.set("Preparing tournament files…")
        workspace.mkdir(parents=True, exist_ok=True); division_dir.mkdir(parents=True, exist_ok=True)

        def worker():
            try:
                logs = []
                for label, command in commands:
                    self.after(0, lambda text=label: self.status_var.set(text))
                    completed = self._subprocess_run(command); logs.append(completed.stdout.strip())
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
        self.status_var.set(f"Master database match complete — saved {output.name}")

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
        win.geometry("700x470")
        win.minsize(650, 430)
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Authorized Scorers", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Create each scorer with your own 6-digit PIN. PINs must be unique. Phones stay signed in for the tournament session, and saved scores remain editable.",
            wraplength=650, justify="left"
        ).pack(anchor="w", pady=(3, 12))

        tree = ttk.Treeview(frame, columns=("name",), show="headings", height=11, selectmode="browse")
        tree.heading("name", text="Scorer Name")
        tree.column("name", width=590, anchor="w")
        tree.pack(fill="both", expand=True)

        addrow = ttk.Frame(frame)
        addrow.pack(fill="x", pady=(10, 6))
        name_var = tk.StringVar()
        pin_var = tk.StringVar()
        ttk.Label(addrow, text="Name:").pack(side="left")
        ttk.Entry(addrow, textvariable=name_var, width=28).pack(side="left", padx=(5, 10), fill="x", expand=True)
        ttk.Label(addrow, text="6-digit PIN:").pack(side="left")
        pin_entry = ttk.Entry(addrow, textvariable=pin_var, width=9)
        pin_entry.pack(side="left", padx=(5, 10))

        def refresh():
            try:
                data = list_scorers(cloud_url, admin_key)
                for item in tree.get_children():
                    tree.delete(item)
                for scorer in data.get("scorers", []):
                    tree.insert("", "end", iid=str(scorer["id"]), values=(scorer["name"],))
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def add():
            try:
                result = create_scorer(cloud_url, admin_key, name_var.get(), pin_var.get())
                scorer = result["scorer"]
                name_var.set("")
                pin_var.set("")
                refresh()
                messagebox.showinfo(
                    "Scorer Added",
                    f"Scorer: {scorer['name']}\nPIN: {scorer['pin']}\n\nThe scorer can now use this PIN on any lane-pair QR page.",
                    parent=win,
                )
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def selected_id():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def set_pin():
            sid = selected_id()
            if sid is None:
                messagebox.showinfo("Select scorer", "Select a scorer first.", parent=win)
                return
            name = tree.item(str(sid), "values")[0]
            pin = simpledialog.askstring(
                "Set Scorer PIN",
                f"Enter a new 6-digit PIN for {name}:",
                parent=win,
            )
            if pin is None:
                return
            try:
                result = reset_scorer_pin(cloud_url, admin_key, sid, pin)
                scorer = result["scorer"]
                messagebox.showinfo(
                    "Scorer PIN Updated",
                    f"Scorer: {scorer['name']}\nPIN: {scorer['pin']}\n\nExisting phone sessions for this scorer were signed out.",
                    parent=win,
                )
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        def remove():
            sid = selected_id()
            if sid is None:
                messagebox.showinfo("Select scorer", "Select a scorer first.", parent=win)
                return
            name = tree.item(str(sid), "values")[0]
            if not messagebox.askyesno("Remove scorer?", f"Remove {name} and invalidate their phone session?", parent=win):
                return
            try:
                delete_scorer(cloud_url, admin_key, sid)
                refresh()
            except Exception as exc:
                messagebox.showerror("Scorer PINs", str(exc), parent=win)

        # Native tk.Button is used here deliberately instead of themed ttk.Button so
        # button captions remain visible across Windows/macOS Tk themes.
        btn_opts = dict(font=("Segoe UI", 9, "bold"), padx=10, pady=6, relief="raised", bd=1)
        tk.Button(
            addrow, text="Add Scorer", command=add,
            bg="#1f6feb", fg="white", activebackground="#1859bd", activeforeground="white",
            **btn_opts
        ).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(2, 0))
        tk.Button(buttons, text="Set Selected PIN", command=set_pin, **btn_opts).pack(side="left")
        tk.Button(buttons, text="Remove Selected", command=remove, **btn_opts).pack(side="left", padx=8)
        tk.Button(buttons, text="Refresh", command=refresh, **btn_opts).pack(side="left")
        tk.Button(buttons, text="Close", command=win.destroy, **btn_opts).pack(side="right")
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

        self._save_workspace_state()
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
        by_lane = {int(lane["lane_no"]): len(lane["bowlers"]) for lane in manifest["lanes"]}
        pair_counts = [sum(by_lane.get(int(n), 0) for n in pair.get("lane_nos", [])) for pair in manifest.get("lane_pairs", [])]
        self.status_var.set(
            f"Lane scoring ready — {sum(counts)} bowlers across {len(pair_counts)} scorecards ({min(pair_counts)}-{max(pair_counts)} per pair)"
        )
        messagebox.showinfo(
            "Lane Scoring Ready",
            f"Random lane assignment published successfully.\n\n"
            f"Bowlers: {sum(counts)}\nLanes: {len(counts)}\nScorecards/lane pairs: {len(pair_counts)}\n"
            f"Bowlers per scorecard: {min(pair_counts)}-{max(pair_counts)}\n\n"
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
                from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
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

    # ------------------------------------------------------------------
    # Permanent bowlers / public results portal
    # ------------------------------------------------------------------
    def _portal_credentials(self):
        url=self.cloud_url_var.get().strip(); key=self.cloud_admin_key_var.get().strip()
        if not url or not key:
            raise ValueError("Enter the Cloud scoring URL and Cloud admin key on the Lanes + Mobile page first.")
        return url,key

    def import_permanent_bowlers(self):
        try:
            url,key=self._portal_credentials()
            demo=require_local_bowler_database(Path(self.workspace_var.get()).expanduser())
        except Exception as exc:
            messagebox.showerror("Permanent bowler sync",str(exc),parent=self); return
        self.status_var.set("Updating permanent bowler database from the local master bowler database…")
        def worker():
            try:
                result=portal_import_bowlers(url,key,demo)
                self.after(0,lambda:self._permanent_import_done(result))
            except Exception as exc:
                self.after(0,lambda e=exc:self._job_failed("Permanent bowler import",e))
        threading.Thread(target=worker,daemon=True).start()

    def _permanent_import_done(self,result):
        self.status_var.set(f"Permanent bowlers updated — {result.get('created',0)} new, {result.get('updated',0)} refreshed")
        errors=result.get("errors") or []
        detail=("\n\nReview:\n"+"\n".join(errors[:8])) if errors else ""
        messagebox.showinfo("Permanent Bowler Database",f"Created: {result.get('created',0)}\nUpdated: {result.get('updated',0)}\nSkipped: {result.get('skipped',0)}{detail}",parent=self)

    def manage_permanent_bowlers(self):
        try: url,key=self._portal_credentials()
        except Exception as exc: messagebox.showerror("Cloud settings",str(exc),parent=self); return
        win=tk.Toplevel(self); win.title("Private Permanent Bowler Database"); win.geometry("900x560"); win.minsize(760,420)
        outer=ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Permanent Bowlers",font=("Segoe UI",16,"bold")).pack(anchor="w")
        ttk.Label(outer,text="This list is private. Select a bowler and set the Jr. Gold state to blank, JG (trying to qualify), or Q (already qualified).",wraplength=820,justify="left").pack(anchor="w",pady=(3,10))
        cols=("id","name","gender","birthdate","division","jg")
        tree=ttk.Treeview(outer,columns=cols,show="headings",selectmode="browse")
        widths=(125,210,75,100,110,65)
        labels=("Bowler ID","Bowler","Gender","Birthdate","Division","JG State")
        for c,label,w in zip(cols,labels,widths): tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True)
        status=tk.StringVar(value="Loading…"); ttk.Label(outer,textvariable=status).pack(anchor="w",pady=(7,4))
        controls=ttk.Frame(outer); controls.pack(fill="x")
        def refresh():
            try:
                data=portal_list_bowlers(url,key)
                tree.delete(*tree.get_children())
                for b in data.get("bowlers",[]):
                    tree.insert("","end",iid=b["bowler_id"],values=(b["bowler_id"],f"{b['first_name']} {b['last_name']}",b["gender"],b["birthdate"],b["division"],b["jr_gold_state"] or "—"))
                status.set(f"{len(data.get('bowlers',[]))} permanent bowlers")
            except Exception as exc: status.set(str(exc)); messagebox.showerror("Could not load bowlers",str(exc),parent=win)
        def set_state(state):
            sel=tree.selection()
            if not sel: messagebox.showinfo("Select bowler","Select a bowler first.",parent=win); return
            try: portal_set_jr_gold(url,key,sel[0],state); refresh()
            except Exception as exc: messagebox.showerror("Could not update status",str(exc),parent=win)
        # Native tk buttons avoid theme-specific blank-label issues seen on some Windows installs.
        tk.Button(controls,text="Set Blank",command=lambda:set_state(""),padx=12,pady=6).pack(side="left")
        tk.Button(controls,text="Set JG",command=lambda:set_state("JG"),padx=12,pady=6).pack(side="left",padx=6)
        tk.Button(controls,text="Set Q",command=lambda:set_state("Q"),padx=12,pady=6).pack(side="left")
        tk.Button(controls,text="Refresh",command=refresh,padx=12,pady=6).pack(side="right")
        refresh()

    def _portal_roster_ready(self):
        roster=Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file(): raise ValueError("Choose or create tournament_divisions/all_divisions.csv first.")
        manifest=Path(self.lane_manifest_var.get()).expanduser()
        return roster, (manifest if manifest.is_file() else None)

    def push_public_qualifying(self):
        try:
            url,key=self._portal_credentials(); roster,manifest=self._portal_roster_ready()
        except Exception as exc: messagebox.showerror("Cannot publish standings",str(exc),parent=self); return
        self.status_var.set("Publishing qualifying standings…")
        def worker():
            try:
                result=portal_publish_qualifying(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                self.after(0,lambda:self._qualifying_publish_done(result))
            except Exception as exc: self.after(0,lambda e=exc:self._job_failed("Qualifying standings publish",e))
        threading.Thread(target=worker,daemon=True).start()

    def _qualifying_publish_done(self,result):
        unmatched=result.get("unmatched_bowlers",0); self.status_var.set("Public qualifying standings published")
        note=f"\n\nUnmatched permanent bowlers: {unmatched}" if unmatched else ""
        messagebox.showinfo("Standings Published",f"Current qualifying standings are now live on the Render site.{note}",parent=self)

    def push_jr_gold_qualifying(self):
        try:
            url,key=self._portal_credentials(); roster,manifest=self._portal_roster_ready()
        except Exception as exc: messagebox.showerror("Cannot publish Jr. Gold standings",str(exc),parent=self); return
        self.status_var.set("Publishing Jr. Gold qualifying standings…")
        def worker():
            try:
                result=portal_publish_jr_gold(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                self.after(0,lambda:self._jr_gold_publish_done(result))
            except Exception as exc: self.after(0,lambda e=exc:self._job_failed("Jr. Gold standings publish",e))
        threading.Thread(target=worker,daemon=True).start()

    def _jr_gold_publish_done(self,result):
        self.status_var.set("Jr. Gold standings published")
        messagebox.showinfo("Jr. Gold Published",f"Published {result.get('published_bowlers',0)} eligible bowlers across {result.get('groups',0)} Jr. Gold group(s).",parent=self)

    def archive_public_tournament(self):
        try:
            url,key=self._portal_credentials(); roster,manifest=self._portal_roster_ready()
        except Exception as exc: messagebox.showerror("Cannot archive tournament",str(exc),parent=self); return
        if not messagebox.askyesno("Archive tournament?","This publishes the latest qualifying standings and saves the current qualifying + match-play performance as a FINAL historical tournament. Continue?",parent=self): return
        self.status_var.set("Archiving tournament and updating season performance…")
        def worker():
            try:
                portal_publish_qualifying(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                portal_publish_jr_gold(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                result=portal_archive_tournament(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                self.after(0,lambda:self._archive_publish_done(result))
            except Exception as exc: self.after(0,lambda e=exc:self._job_failed("Tournament archive",e))
        threading.Thread(target=worker,daemon=True).start()

    def _archive_publish_done(self,result):
        self.status_var.set("Tournament archived — BOY points updated")
        unmatched=result.get("unmatched_bowlers",0)
        promoted=result.get("jr_gold_promoted",0)
        promoted_ids=result.get("jr_gold_promoted_ids") or []
        if promoted_ids:
            try:
                set_local_jr_gold_by_bowler_ids(Path(self.workspace_var.get()).expanduser(), promoted_ids, "Q")
            except Exception:
                pass
        messagebox.showinfo("Tournament Archived",f"Saved {result.get('archived',0)} bowler performances to the public archive and recalculated Bowler-of-the-Year points.\n\nJr. Gold bowlers newly qualified: {promoted}\nUnmatched permanent bowlers: {unmatched}",parent=self)

    def reset_for_next_tournament(self):
        workspace = Path(self.workspace_var.get()).expanduser()
        if not messagebox.askyesno(
            "Reset for next tournament?",
            "Use this after you have archived the completed tournament. The current tournament files will be moved into completed_tournaments, while the local demographic database and imported-file archive are preserved. Continue?",
            parent=self,
        ):
            return
        stamp = date.today().isoformat() + "_" + __import__("datetime").datetime.now().strftime("%H%M%S")
        safe_event = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (self.event_name_var.get().strip() or "tournament"))[:60]
        archive_dir = workspace / "completed_tournaments" / f"{stamp}_{safe_event}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        candidates = [
            workspace / "payment_status.csv", workspace / "duplicate_review.csv",
            workspace / "paid_demographic_check.csv", workspace / "tournament_divisions",
            workspace / "lane_scoring",
        ]
        moved = []
        try:
            for src in candidates:
                if src.exists():
                    dest = archive_dir / src.name
                    if dest.exists():
                        if dest.is_dir(): shutil.rmtree(dest)
                        else: dest.unlink()
                    shutil.move(str(src), str(dest)); moved.append(src.name)
            # Clear only tournament-specific selections/settings. Permanent local demographics remain.
            self.registration_var.set(""); self.transactions_var.set("")
            self.event_name_var.set("Tough Shots Tournament"); self.event_date_var.set(date.today().isoformat())
            try:
                self._workspace_state_path().unlink(missing_ok=True)
            except Exception:
                pass
            self._sync_pipeline_paths()
            self.status_var.set("Ready for next tournament")
            messagebox.showinfo(
                "Tournament Reset Complete",
                "The active workspace is ready for a new tournament.\n\n"
                f"Previous local tournament files were preserved in:\n{archive_dir}\n\n"
                "The local demographic database, imported source-file archive, permanent cloud bowlers, public archive, and scorer PINs were not removed.",
                parent=self,
            )
            self.show_page("overview")
        except Exception as exc:
            messagebox.showerror("Reset failed", f"The reset stopped before completion.\n\n{exc}", parent=self)

    def open_public_site(self):
        url=self.cloud_url_var.get().strip().rstrip("/")
        if not url: messagebox.showerror("Cloud URL missing","Enter the Render Cloud scoring URL first.",parent=self); return
        webbrowser.open(url)

    def open_lane_pdf(self):
        path = Path(self.lane_pdf_var.get()).expanduser()
        if not path.is_file():
            messagebox.showerror("PDF not found", "Prepare lane scoring first.", parent=self)
            return
        self.open_path(path)

    # ------------------------------------------------------------------
    # Tournament / OS helpers
    # ------------------------------------------------------------------
    def _workspace_tournament_paths(self):
        workspace = Path(self.workspace_var.get()).expanduser()
        roster = workspace / "tournament_divisions" / "all_divisions.csv"
        db_path = roster.with_name(roster.stem + "_tournament.sqlite3")
        lane_manifest = workspace / "lane_scoring" / "lane_manifest.json"
        lane_pdf = workspace / "lane_scoring" / "lane_scoresheets.pdf"
        return workspace, roster, db_path, lane_manifest, lane_pdf

    def _detect_saved_tournament(self):
        """Surface a saved active tournament without opening anything automatically."""
        try:
            _, roster, db_path, lane_manifest, lane_pdf = self._workspace_tournament_paths()
            if roster.is_file() and db_path.is_file():
                self.tournament_roster_var.set(str(roster))
                if lane_manifest.is_file():
                    self.lane_manifest_var.set(str(lane_manifest))
                    try:
                        data = __import__("json").loads(lane_manifest.read_text(encoding="utf-8"))
                        if data.get("tournament_name"):
                            self.event_name_var.set(str(data["tournament_name"]))
                    except Exception:
                        pass
                if lane_pdf.is_file():
                    self.lane_pdf_var.set(str(lane_pdf))
                self.status_var.set("Saved active tournament found — use Reload Tournament from Workspace to resume it")
        except Exception:
            pass

    def reload_tournament_from_workspace(self):
        """Resume the complete active tournament saved in the selected workspace."""
        self._load_workspace_state()
        workspace, roster, db_path, lane_manifest, lane_pdf = self._workspace_tournament_paths()
        if not roster.is_file() or not db_path.is_file():
            messagebox.showerror(
                "Saved tournament not found",
                "No resumable active tournament was found in this workspace.\n\n"
                "Expected both:\n"
                f"{roster}\n"
                f"{db_path}\n\n"
                "If you already used Reset for Next Tournament, the previous tournament is under completed_tournaments instead of the active workspace.",
                parent=self,
            )
            return

        # Restore every active-workspace path used by the surrounding suite.
        self._sync_pipeline_paths()
        self.tournament_roster_var.set(str(roster))
        if lane_manifest.is_file():
            self.lane_manifest_var.set(str(lane_manifest))
            try:
                data = __import__("json").loads(lane_manifest.read_text(encoding="utf-8"))
                if data.get("tournament_name"):
                    self.event_name_var.set(str(data["tournament_name"]))
                if data.get("lane_count"):
                    self.lane_count_var.set(str(data["lane_count"]))
            except Exception:
                pass
        if lane_pdf.is_file():
            self.lane_pdf_var.set(str(lane_pdf))

        try:
            kwargs = {"cwd": str(BASE_DIR)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                [sys.executable, str(TOURNAMENT_SCRIPT), str(roster), "--db", str(db_path), "--resume"],
                **kwargs,
            )
            self.status_var.set(f"Reloaded saved tournament from {workspace.name}")
            self.show_page("tournament")
        except Exception as exc:
            messagebox.showerror("Could not reload tournament", str(exc), parent=self)

    def launch_tournament_manager(self):
        self._save_workspace_state()
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
