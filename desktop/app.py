#!/usr/bin/env python3
"""Tough Shots Tournament Suite desktop application.

The UI is organized around connection, the reusable master bowler database,
workspace-owned tournament inputs, tournament setup, live tournament
management, and public-site publishing. The preparation processors and
Tournament Manager remain the underlying sources of truth.
"""

from __future__ import annotations

import os
import csv
import json
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
    reset_scorer_pin, delete_scorer, move_bowler_to_lane, roster_rows, get_admin_pin_status, set_admin_pin,
)
from core.import_archive import archive_imports
from core.printing import create_qualifying_pdf, create_jr_gold_pdf, create_current_brackets_pdf, send_pdf_to_printer
from core.local_demographics import (
    update_from_csv as update_local_demographic_db,
    require_database as require_local_bowler_database,
    count_rows as local_demographic_count,
    list_local_bowlers, add_local_bowler, update_local_bowler, delete_local_bowler,
    set_local_jr_gold_by_bowler_ids, DIVISIONS as LOCAL_DIVISIONS, missing_from_registration,
    sync_from_cloud_bowlers, preview_csv_update, proper_name,
)
from core.theme import THEMES, load_theme_name, save_theme_name, apply_theme
from core.results_portal import (
    import_bowlers as portal_import_bowlers, list_bowlers as portal_list_bowlers,
    set_jr_gold as portal_set_jr_gold, publish_qualifying as portal_publish_qualifying,
    publish_jr_gold as portal_publish_jr_gold, archive_tournament as portal_archive_tournament,
    publish_match_play as portal_publish_match_play, clear_current_tournament as portal_clear_current_tournament,
    publish_lane_assignments as portal_publish_lane_assignments,
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
        super().__init__(parent, padding=28, style="App.TFrame")
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

        self.theme_var = tk.StringVar(value=load_theme_name())
        self._configure_styles()
        self._busy = False

        default_workspace = BASE_DIR / "TournamentWorkspace"
        # Source selections are temporary. Once imported, all tournament processing
        # uses the workspace copies in tournament_inputs/.
        self.registration_source_var = tk.StringVar()
        self.transactions_source_var = tk.StringVar()
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
        self.show_page("connect")
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
        # Keep native Tk buttons visually consistent with ttk action buttons.
        self.option_add("*Button.font", ("Segoe UI", 10, "bold"))
        self.option_add("*Button.padX", 14)
        self.option_add("*Button.padY", 9)
        apply_theme(self, self.theme_var.get(), suite_styles=True)

    def _theme_changed(self, *_args):
        name = self.theme_var.get()
        if name not in THEMES:
            return
        apply_theme(self, name, suite_styles=True)
        save_theme_name(name)
        # Reassert selected navigation style after the style palette changes.
        current = next((k for k, b in self.nav_buttons.items() if str(b.cget("style")) == "NavSelected.TButton"), None)
        if current:
            self.show_page(current)
        self.status_var.set(f"Theme changed to {name}.")

    def _build_shell(self):
        shell = ttk.Frame(self, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", padding=(16, 22))
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.configure(width=245)
        sidebar.grid_propagate(False)

        ttk.Label(sidebar, text="TOUGH SHOTS", style="Brand.TLabel").pack(anchor="w", padx=6)
        ttk.Label(sidebar, text="Tournament operations", style="BrandSub.TLabel").pack(anchor="w", padx=6, pady=(1, 24))

        nav_items = [
            ("connect", "1  Connect to Website"),
            ("database", "2  Bowler Database"),
            ("files", "3  Tournament Files"),
            ("setup", "4  Tournament Setup"),
            ("manager", "5  Tournament Manager"),
            ("public", "6  Website"),
        ]
        for key, text in nav_items:
            btn = ttk.Button(sidebar, text=text, command=lambda k=key: self.show_page(k), style="Nav.TButton")
            btn.pack(fill="x", pady=2)
            self.nav_buttons[key] = btn

        ttk.Separator(sidebar).pack(fill="x", pady=(22, 14))
        ttk.Label(sidebar, text="Theme", style="BrandSub.TLabel").pack(anchor="w", padx=6, pady=(0, 5))
        theme_box = ttk.Combobox(
            sidebar,
            textvariable=self.theme_var,
            values=list(THEMES.keys()),
            state="readonly",
            width=22,
        )
        theme_box.pack(fill="x", padx=4, pady=(0, 12))
        theme_box.bind("<<ComboboxSelected>>", self._theme_changed)
        ttk.Separator(sidebar).pack(fill="x", pady=(0, 10))
        ttk.Button(sidebar, text="Choose Workspace", command=self.choose_workspace, style="Nav.TButton").pack(fill="x", pady=2)
        ttk.Button(sidebar, text="Open Workspace", command=self.open_workspace, style="Nav.TButton").pack(fill="x", pady=2)

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
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="ew")

    def _build_pages(self):
        self.pages["connect"] = self._build_connect_page()
        self.pages["database"] = self._build_database_page()
        self.pages["files"] = self._build_tournament_files_page()
        self.pages["setup"] = self._build_setup_page()
        self.pages["manager"] = self._build_manager_page()
        self.pages["public"] = self._build_public_page()
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def show_page(self, key: str):
        self.pages[key].tkraise()
        for nav_key, button in self.nav_buttons.items():
            button.configure(style="NavSelected.TButton" if nav_key == key else "Nav.TButton")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def _build_connect_page(self):
        page = WorkflowPage(
            self.page_host,
            "1 Connect to Website",
            "Enter the Render website address and private admin key used by Tough Shots. These settings are shared by mobile scoring, bowler sync, and public results.",
        )
        body = page.body
        settings = ttk.Frame(body, style="Card.TFrame")
        settings.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Website URL", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(settings, textvariable=self.cloud_url_var).grid(row=0, column=1, sticky="ew", pady=7)
        ttk.Label(settings, text="Admin key", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(settings, textvariable=self.cloud_admin_key_var, show="•").grid(row=1, column=1, sticky="ew", pady=7)
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="w")
        ttk.Button(actions, text="Test Website Connection", command=self.test_website_connection, style="Primary.TButton").pack(side="left")
        ttk.Button(actions, text="Open Public Site", command=self.open_public_site, style="Secondary.TButton").pack(side="left", padx=8)
        return page

    def _build_database_page(self):
        page = WorkflowPage(
            self.page_host,
            "2 Bowler Database",
            "The demographic form is used only to create or update the master bowler database. Tournament setup reads demographic information from the master database, not from the form itself.",
        )
        body = page.body
        FilePicker(
            body,
            label="Demographic form CSV",
            variable=self.demographics_var,
            choose_command=lambda: self.choose_csv(self.demographics_var),
            hint="Import a fresh demographic export whenever new bowlers or corrections need to be added to the master database.",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 18))

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure((0, 1), weight=1)
        ttk.Button(actions, text="Download Master Database", command=self.download_master_bowlers, style="Primary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=6)
        ttk.Button(actions, text="Update Local Database", command=self.update_local_demographics, style="Primary.TButton").grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=6)
        ttk.Button(actions, text="Manage Local Database", command=self.manage_local_bowlers, style="Secondary.TButton").grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=6)
        ttk.Button(actions, text="Manage JG / Q Status", command=self.manage_bowler_jg_status, style="Secondary.TButton").grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=6)
        ttk.Button(actions, text="Update Master Database", command=self.import_permanent_bowlers, style="Primary.TButton").grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        return page

    def _build_tournament_files_page(self):
        page = WorkflowPage(
            self.page_host,
            "3 Tournament Files",
            "Choose the registration and Square exports, then import them into the active workspace. After import, Tough Shots uses only the saved workspace copies for this tournament.",
        )
        body = page.body
        FilePicker(body, label="Tournament registration CSV", variable=self.registration_source_var,
                   choose_command=lambda: self.choose_csv(self.registration_source_var)).grid(row=0, column=0, sticky="ew", pady=(0, 14))
        FilePicker(body, label="Square transactions CSV", variable=self.transactions_source_var,
                   choose_command=lambda: self.choose_csv(self.transactions_source_var)).grid(row=1, column=0, sticky="ew", pady=(0, 18))
        ttk.Button(body, text="Import Tournament Files to Workspace", command=self.import_tournament_files, style="Primary.TButton").grid(row=2, column=0, sticky="ew", pady=(0, 18))

        saved = ttk.LabelFrame(body, text="Active workspace copies", padding=12)
        saved.grid(row=3, column=0, sticky="ew")
        saved.columnconfigure(1, weight=1)
        ttk.Label(saved, text="Registration", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(saved, textvariable=self.registration_var, state="readonly").grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(saved, text="Square transactions", style="FieldLabel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(saved, textvariable=self.transactions_var, state="readonly").grid(row=1, column=1, sticky="ew", pady=4)
        return page

    def _build_setup_page(self):
        page = WorkflowPage(
            self.page_host,
            "4 Tournament Setup",
            "Run tournament setup from the imported workspace files, review anything that needs attention, edit the local registration copy if needed, then create lane assignments and score sheets.",
        )
        body = page.body
        ttk.Button(body, text="Setup Tournament", command=self.run_all, style="Primary.TButton").grid(row=0, column=0, sticky="ew", pady=(0, 14))

        review = ttk.LabelFrame(body, text="Review & error-catching files", padding=12)
        review.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        review.columnconfigure(0, weight=1)
        buttons = [
            ("Duplicate Payments", lambda: self.open_review_file("duplicate_review.csv")),
            ("Payment Status", lambda: self.open_review_file("payment_status.csv")),
            ("Entry / Demographic Review", lambda: self.open_review_file("paid_demographic_check.csv")),
            ("Invalid / Needs Review Bowlers", lambda: self.open_review_file("tournament_divisions/needs_review.csv")),
            ("Missing Demographics", self.show_missing_demographics),
        ]
        # Two responsive rows: both stay visually centered while buttons expand
        # and contract with the application window.
        top_review = ttk.Frame(review)
        top_review.grid(row=0, column=0, sticky="ew")
        for c in range(3):
            top_review.columnconfigure(c, weight=1, uniform="review_top")
        bottom_review = ttk.Frame(review)
        bottom_review.grid(row=1, column=0, sticky="ew")
        for c in range(2):
            bottom_review.columnconfigure(c, weight=1, uniform="review_bottom")
        for c, (text, cmd) in enumerate(buttons[:3]):
            ttk.Button(top_review, text=text, command=cmd, style="Secondary.TButton").grid(
                row=0, column=c, sticky="ew", padx=5, pady=5
            )
        for c, (text, cmd) in enumerate(buttons[3:]):
            ttk.Button(bottom_review, text=text, command=cmd, style="Secondary.TButton").grid(
                row=0, column=c, sticky="ew", padx=5, pady=5
            )

        ttk.Button(body, text="Edit Local Tournament Registration", command=self.edit_local_registration, style="Primary.TButton").grid(row=2, column=0, sticky="ew", pady=(0, 14))

        lane = ttk.LabelFrame(body, text="Lane assignment & score sheets", padding=12)
        lane.grid(row=3, column=0, sticky="ew")
        lane.columnconfigure((0, 1, 2), weight=1)
        lane.rowconfigure(2, minsize=58)
        ttk.Label(lane, text="Tournament name", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(lane, textvariable=self.event_name_var).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 10))
        ttk.Label(lane, text="Event date", style="FieldLabel.TLabel").grid(row=0, column=1, sticky="w", pady=(0, 4))
        ttk.Entry(lane, textvariable=self.event_date_var).grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 10))
        ttk.Label(lane, text="Available lanes", style="FieldLabel.TLabel").grid(row=0, column=2, sticky="w", padx=(8, 0), pady=(0, 4))
        ttk.Spinbox(lane, textvariable=self.lane_count_var, from_=1, to=200, width=9).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(0, 10))
        ttk.Button(lane, text="1. Set Lane Groups\n(Optional)", command=self.manage_lane_groups, style="Secondary.TButton").grid(row=2, column=0, sticky="nsew", padx=(0, 5), pady=5)
        ttk.Button(lane, text="2. Build / Edit Lane Assignment", command=self.build_or_edit_lane_assignment, style="Primary.TButton").grid(row=2, column=1, sticky="nsew", padx=5, pady=5)
        ttk.Button(lane, text="3. Generate Score Sheets", command=self.generate_lane_scoresheets, style="Primary.TButton").grid(row=2, column=2, sticky="nsew", padx=(5, 0), pady=5)
        return page

    def _build_manager_page(self):
        page = WorkflowPage(
            self.page_host,
            "5 Tournament Manager",
            "Run the tournament, manage the active field, sync mobile scoring, and print tournament forms from one place.",
        )
        body = page.body
        top = ttk.Frame(body, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.columnconfigure(0, weight=1, uniform="manager_outer")
        top.columnconfigure(1, weight=2)
        top.columnconfigure(2, weight=1, uniform="manager_outer")
        ttk.Button(top, text="Open Tournament Manager", command=self.launch_tournament_manager, style="Primary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(top, text="Manage Current Tournament Bowlers", command=self.manage_current_tournament_bowlers, style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(top, text="Jr. Gold Settings", command=self.open_jr_gold_settings, style="Secondary.TButton").grid(row=0, column=2, sticky="ew", padx=(5, 0))

        mobile = ttk.LabelFrame(body, text="Mobile scoring & assignment tools", padding=12)
        mobile.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for c in range(3): mobile.columnconfigure(c, weight=1)
        ttk.Button(mobile, text="Manage PINs", command=self.manage_scorer_pins, style="Secondary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(mobile, text="Republish Mobile Scoring", command=self.retry_lane_publish, style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(mobile, text="Sync Mobile Scores", command=self.sync_mobile_scores, style="Secondary.TButton").grid(row=0, column=2, sticky="ew", padx=(5, 0))
        ttk.Checkbutton(mobile, text="Auto-sync cloud scores every 15 seconds", variable=self.auto_sync_var, command=self._auto_sync_changed).grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        printing = ttk.LabelFrame(body, text="Print Center", padding=12)
        printing.grid(row=2, column=0, sticky="ew")
        printing.columnconfigure((0, 1, 2), weight=1)
        ttk.Button(printing, text="Print Qualifying - All Divisions", command=self.print_all_qualifying, style="Primary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(printing, text="Print Jr. Gold Qualifying", command=self.print_all_jr_gold, style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(printing, text="Print Current Match Play Round", command=self.print_current_match_round, style="Secondary.TButton").grid(row=0, column=2, sticky="ew", padx=(5, 0))
        return page

    def _build_public_page(self):
        page = WorkflowPage(
            self.page_host,
            "6 Website",
            "Publish and manage the Tough Shots website, archive final results, or clear the live Current Tournament section when needed.",
        )
        body = page.body

        public_site = ttk.LabelFrame(body, text="Public Site", padding=12)
        public_site.grid(row=0, column=0, sticky="ew")
        for c in range(2):
            public_site.columnconfigure(c, weight=1)

        ttk.Button(
            public_site, text="Open Public Site", command=self.open_public_site, style="Secondary.TButton"
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=6)
        ttk.Button(
            public_site, text="Push Current Qualifying Standings", command=self.push_public_qualifying, style="Primary.TButton"
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=6)
        ttk.Button(
            public_site, text="Push Jr. Gold Standings", command=self.push_jr_gold_qualifying, style="Secondary.TButton"
        ).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=6)
        ttk.Button(
            public_site, text="Push Match Play Brackets", command=self.push_public_match_play, style="Secondary.TButton"
        ).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=6)
        ttk.Button(
            public_site, text="Archive Tournament + Update BOY Data", command=self.archive_public_tournament, style="Primary.TButton"
        ).grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=6)
        ttk.Button(
            public_site, text="Clear Current Tournament from Website", command=self.clear_current_tournament_website, style="Secondary.TButton"
        ).grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=6)

        admin_row = ttk.Frame(body, style="Card.TFrame")
        admin_row.grid(row=1, column=0, sticky="ew", pady=(12, 6))
        admin_row.columnconfigure(0, weight=1)
        ttk.Button(
            admin_row, text="Admin Controls", command=self.open_admin_controls, style="Secondary.TButton"
        ).grid(row=0, column=0, sticky="ew")

        next_box = ttk.LabelFrame(body, text="After the tournament", padding=12)
        next_box.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        next_box.columnconfigure(0, weight=1)
        ttk.Button(
            next_box, text="Reset for Next Tournament", command=self.reset_for_next_tournament, style="Secondary.TButton"
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            next_box,
            text="Archive the tournament first. Reset then moves the active local tournament files aside while keeping the master bowler database and historical results.",
            style="CardText.TLabel", wraplength=760, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
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

        self.payment_input_var.set(str(payment))
        self.demographic_input_var.set(str(demographic))
        self.division_input_var.set(str(demographic))
        self.division_output_var.set(str(division_dir))
        self.tournament_roster_var.set(str(roster))
        lane_dir = workspace / "lane_scoring"
        self.lane_manifest_var.set(str(lane_dir / "lane_manifest.json"))
        self.lane_pdf_var.set(str(lane_dir / "lane_scoresheets.pdf"))

        # Tournament processing always uses workspace-owned copies, never the
        # original source paths selected on the Tournament Files page.
        inputs = workspace / "tournament_inputs"
        reg = inputs / "tournament_registration.csv"
        txn = inputs / "square_transactions.csv"
        self.registration_var.set(str(reg) if reg.is_file() else "")
        self.transactions_var.set(str(txn) if txn.is_file() else "")

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
            ("tournament_roster", self.tournament_roster_var),
            ("lane_manifest", self.lane_manifest_var),
            ("lane_pdf", self.lane_pdf_var),
        ]
        for key, variable in mapping:
            value = data.get(key)
            if value not in (None, ""):
                variable.set(str(value))
        self._use_workspace_input_copies()

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

    def _tournament_inputs_dir(self):
        path = Path(self.workspace_var.get()).expanduser() / "tournament_inputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _workspace_registration_path(self):
        return self._tournament_inputs_dir() / "tournament_registration.csv"

    def _workspace_transactions_path(self):
        return self._tournament_inputs_dir() / "square_transactions.csv"

    def _use_workspace_input_copies(self):
        reg = self._workspace_registration_path()
        txn = self._workspace_transactions_path()
        self.registration_var.set(str(reg) if reg.is_file() else "")
        self.transactions_var.set(str(txn) if txn.is_file() else "")

    def _require_workspace_tournament_files(self):
        self._use_workspace_input_copies()
        missing = []
        if not Path(self.registration_var.get()).is_file():
            missing.append("Tournament registration")
        if not Path(self.transactions_var.get()).is_file():
            missing.append("Square transactions")
        if missing:
            raise ValueError(
                "Import the tournament files on 3 Tournament Files first.\n\nMissing workspace copy: "
                + ", ".join(missing)
            )
        return Path(self.registration_var.get()), Path(self.transactions_var.get())

    def import_tournament_files(self):
        """Copy source registration/Square exports into the active workspace.

        The selected originals are archived for provenance, then all downstream
        tournament processing uses only the fixed workspace filenames.
        """
        reg_src = Path(self.registration_source_var.get()).expanduser()
        txn_src = Path(self.transactions_source_var.get()).expanduser()
        if not reg_src.is_file() or not txn_src.is_file():
            messagebox.showerror(
                "Tournament files required",
                "Choose both the tournament registration CSV and Square transactions CSV.",
                parent=self,
            )
            return
        if self._busy:
            return
        if not self._archive_inputs(
            "tournament_file_import",
            ("tournament_registration_source", reg_src),
            ("square_transactions_source", txn_src),
        ):
            return
        dest_dir = self._tournament_inputs_dir()
        reg_dest = dest_dir / "tournament_registration.csv"
        txn_dest = dest_dir / "square_transactions.csv"
        try:
            # Write through temporary files so a failed copy cannot leave a
            # half-written active tournament input.
            reg_tmp = dest_dir / ".tournament_registration.csv.tmp"
            txn_tmp = dest_dir / ".square_transactions.csv.tmp"
            shutil.copy2(reg_src, reg_tmp)
            shutil.copy2(txn_src, txn_tmp)
            reg_tmp.replace(reg_dest)
            txn_tmp.replace(txn_dest)
            self.registration_var.set(str(reg_dest))
            self.transactions_var.set(str(txn_dest))
            self._save_workspace_state()
            self.status_var.set("Tournament files imported to workspace — ready for Setup Tournament")
            messagebox.showinfo(
                "Tournament Files Imported",
                "The tournament files were copied into the active workspace.\n\n"
                "From this point forward, tournament setup uses only these local workspace copies. "
                "Moving, renaming, or editing the original downloaded files will not affect the tournament.\n\n"
                f"Registration:\n{reg_dest}\n\nSquare transactions:\n{txn_dest}",
                parent=self,
            )
            self.show_page("setup")
        except Exception as exc:
            messagebox.showerror("Could not import tournament files", str(exc), parent=self)

    def test_website_connection(self):
        try:
            url, key = self._portal_credentials()
            result = portal_list_bowlers(url, key)
            count = len(result.get("bowlers", []))
            self._save_workspace_state()
            self.status_var.set("Website connection successful")
            messagebox.showinfo(
                "Website Connected",
                f"Connection succeeded.\n\nPrivate permanent bowlers currently on the website: {count}",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Website connection failed", str(exc), parent=self)

    def open_review_file(self, relative_path):
        path = Path(self.workspace_var.get()).expanduser() / Path(relative_path)
        if not path.is_file():
            messagebox.showinfo(
                "Review file not available",
                "That review file has not been created yet. Run Setup Tournament first.",
                parent=self,
            )
            return
        self.open_path(path)

    @staticmethod
    def _registration_person_key(first, last, dob=""):
        def norm(value):
            return " ".join(str(value or "").strip().casefold().split())
        d = str(dob or "").strip()
        if pd is not None and d:
            try:
                parsed = pd.to_datetime(d, errors="coerce")
                if not pd.isna(parsed):
                    d = parsed.strftime("%Y-%m-%d")
            except Exception:
                pass
        return (norm(first), norm(last), norm(d))

    @staticmethod
    def _first_present(row, names):
        for name in names:
            if name in row and str(row.get(name, "")).strip():
                return str(row.get(name, "")).strip()
        return ""

    def _registration_issue_map(self):
        """Collect setup warnings keyed by bowler name/DOB for the local editor."""
        workspace = Path(self.workspace_var.get()).expanduser()
        issues = {}

        def add(key, text):
            issues.setdefault(key, [])
            if text and text not in issues[key]:
                issues[key].append(text)

        def read_rows(path):
            if not path.is_file():
                return []
            try:
                with path.open("r", newline="", encoding="utf-8-sig") as f:
                    return list(csv.DictReader(f))
            except Exception:
                return []

        for row in read_rows(workspace / "duplicate_review.csv"):
            first = self._first_present(row, ["First_Name", "Bowlers First Name", "First Name"])
            last = self._first_present(row, ["Last_Name", "Bowlers Last Name", "Last Name"])
            dob = self._first_present(row, ["Date_of_Birth", "Payment_DOB", "Bowlers Date of Birth", "Date of birth"])
            add(self._registration_person_key(first, last, dob), "Duplicate payment / registration review")

        for row in read_rows(workspace / "payment_status.csv"):
            status = self._first_present(row, ["Status"])
            if status and status.upper() != "PAID":
                first = self._first_present(row, ["First_Name", "Bowlers First Name"])
                last = self._first_present(row, ["Last_Name", "Bowlers Last Name"])
                dob = self._first_present(row, ["Date_of_Birth", "Bowlers Date of Birth"])
                add(self._registration_person_key(first, last, dob), status)

        for row in read_rows(workspace / "paid_demographic_check.csv"):
            designation = self._first_present(row, ["Designation"])
            if designation and designation.upper() != "BOTH - PAID + DEMOGRAPHIC":
                first = self._first_present(row, ["First_Name", "Bowlers First Name"])
                last = self._first_present(row, ["Last_Name", "Bowlers Last Name"])
                dob = self._first_present(row, ["Payment_DOB", "Demographic_DOB", "Date_of_Birth"])
                add(self._registration_person_key(first, last, dob), designation)

        for row in read_rows(workspace / "tournament_divisions" / "needs_review.csv"):
            first = self._first_present(row, ["First_Name", "Bowlers First Name"])
            last = self._first_present(row, ["Last_Name", "Bowlers Last Name"])
            dob = self._first_present(row, ["Payment_DOB", "Demographic_DOB", "Birthdate_Used"])
            reason = self._first_present(row, ["Review_Reason"]) or "Needs division review"
            add(self._registration_person_key(first, last, dob), reason)
        return issues

    def edit_local_registration(self):
        """Search, sort, and safely edit the workspace registration copy."""
        try:
            reg, _ = self._require_workspace_tournament_files()
        except Exception as exc:
            messagebox.showerror("Local registration not available", str(exc), parent=self)
            return
        try:
            with reg.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                rows = [dict(r) for r in reader]
        except Exception as exc:
            messagebox.showerror("Could not open registration", str(exc), parent=self)
            return
        if not fieldnames:
            messagebox.showerror("Registration file", "The local registration CSV has no columns.", parent=self)
            return

        issues = self._registration_issue_map()
        first_col = next((c for c in ["Bowlers First Name", "First Name", "First_Name"] if c in fieldnames), fieldnames[0])
        last_col = next((c for c in ["Bowlers Last Name", "Last Name", "Last_Name"] if c in fieldnames), fieldnames[min(1, len(fieldnames)-1)])
        dob_col = next((c for c in ["Bowlers Date of Birth", "Date of birth", "Date_of_Birth"] if c in fieldnames), "")
        order_col = next((c for c in ["Payable Order ID", "Order ID"] if c in fieldnames), "")

        win = tk.Toplevel(self)
        win.title("Edit Local Tournament Registration")
        win.geometry("1050x650")
        win.minsize(850, 520)
        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="Local Tournament Registration", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Edits affect only the workspace copy used by this tournament. Rerun Setup Tournament after making corrections.", wraplength=950, justify="left").grid(row=1, column=0, sticky="w", pady=(3, 10))

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="new", pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        search_var = tk.StringVar()
        sort_var = tk.StringVar(value="Last name")
        errors_first_var = tk.BooleanVar(value=True)
        ttk.Label(controls, text="Search name").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(controls, textvariable=search_var).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(controls, text="Sort by").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Combobox(controls, textvariable=sort_var, values=["Last name", "First name"], state="readonly", width=12).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(controls, text="Errors first", variable=errors_first_var).grid(row=0, column=4, sticky="w", padx=(12, 0))

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=3, column=0, sticky="nsew")
        outer.rowconfigure(3, weight=1)
        table_frame.rowconfigure(0, weight=1); table_frame.columnconfigure(0, weight=1)
        cols = ("issue", "first", "last", "dob", "order")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        for c, label, width in (("issue", "Review", 260), ("first", "First Name", 150), ("last", "Last Name", 170), ("dob", "Birthdate", 110), ("order", "Order ID", 180)):
            tree.heading(c, text=label); tree.column(c, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview); sb.grid(row=0, column=1, sticky="ns"); tree.configure(yscrollcommand=sb.set)

        visible_index = {}
        def row_issue(row):
            key = self._registration_person_key(row.get(first_col, ""), row.get(last_col, ""), row.get(dob_col, "") if dob_col else "")
            found = issues.get(key, [])
            if found:
                return "; ".join(found)
            # Name-only fallback lets review outputs with differently formatted DOBs still surface.
            nk = key[:2]
            for ik, vals in issues.items():
                if ik[:2] == nk:
                    return "; ".join(vals)
            return ""

        def refresh(*_):
            tree.delete(*tree.get_children()); visible_index.clear()
            q = " ".join(search_var.get().casefold().split())
            indexed = list(enumerate(rows))
            def sort_key(item):
                _, row = item
                issue = bool(row_issue(row))
                first = str(row.get(first_col, "")).casefold()
                last = str(row.get(last_col, "")).casefold()
                namekey = (first, last) if sort_var.get() == "First name" else (last, first)
                return ((0 if issue else 1) if errors_first_var.get() else 0, namekey)
            indexed.sort(key=sort_key)
            for idx, row in indexed:
                name = f"{row.get(first_col,'')} {row.get(last_col,'')}".casefold()
                if q and q not in name:
                    continue
                iid = f"r{idx}"
                visible_index[iid] = idx
                tree.insert("", "end", iid=iid, values=(row_issue(row), row.get(first_col,""), row.get(last_col,""), row.get(dob_col,"") if dob_col else "", row.get(order_col,"") if order_col else ""))

        def save_file():
            tmp = reg.with_suffix(reg.suffix + ".tmp")
            with tmp.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader(); w.writerows(rows)
            tmp.replace(reg)

        def edit_selected(_event=None):
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Select a row", "Select a registration row to edit.", parent=win); return
            idx = visible_index.get(sel[0])
            if idx is None: return
            row = rows[idx]
            dlg = tk.Toplevel(win); dlg.title("Edit Registration Row"); dlg.geometry("720x700"); dlg.transient(win); dlg.grab_set()
            canvas = tk.Canvas(dlg, highlightthickness=0)
            scroll = ttk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, padding=14)
            inner.columnconfigure(1, weight=1)
            window_id = canvas.create_window((0,0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
            inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
            vars_ = {}
            for r, col in enumerate(fieldnames):
                ttk.Label(inner, text=col).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=4)
                v = tk.StringVar(value=str(row.get(col, "") or "")); vars_[col]=v
                ttk.Entry(inner, textvariable=v).grid(row=r, column=1, sticky="ew", pady=4)
            btns = ttk.Frame(inner); btns.grid(row=len(fieldnames), column=0, columnspan=2, sticky="e", pady=(12,0))
            def commit():
                rows[idx] = {col: vars_[col].get() for col in fieldnames}
                try:
                    save_file()
                except Exception as exc:
                    messagebox.showerror("Could not save registration", str(exc), parent=dlg); return
                dlg.destroy(); refresh(); self.status_var.set("Local tournament registration updated — rerun Setup Tournament")
            ttk.Button(btns, text="Cancel", command=dlg.destroy, style="Secondary.TButton").pack(side="right")
            ttk.Button(btns, text="Save Changes", command=commit, style="Primary.TButton").pack(side="right", padx=8)

        search_var.trace_add("write", refresh); sort_var.trace_add("write", refresh); errors_first_var.trace_add("write", refresh)
        tree.bind("<Double-1>", edit_selected)
        footer = ttk.Frame(outer); footer.grid(row=4, column=0, sticky="ew", pady=(10,0))
        ttk.Button(footer, text="Edit Selected Row", command=edit_selected, style="Primary.TButton").pack(side="left")
        ttk.Button(footer, text="Open CSV in Default App", command=lambda: self.open_path(reg), style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(footer, text="Done", command=win.destroy, style="Secondary.TButton").pack(side="right")
        refresh()

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
            preview = preview_csv_update(workspace, demo)
            questionable = preview.get("questionable") or []
            if questionable:
                lines = [f"• Row {x['row']}: {x['name']} — {x['reason']}" for x in questionable[:12]]
                if len(questionable) > 12:
                    lines.append(f"• ...and {len(questionable)-12} more questionable row(s).")
                msg = (
                    "The master database found possible identity conflicts. Nothing has been changed yet.\n\n"
                    + "\n".join(lines)
                    + "\n\nImport these rows anyway? Use Cancel if you want to review the source file or master database first."
                )
                if not messagebox.askyesno("Confirm Questionable Demographic Changes", msg, parent=self, icon="warning"):
                    self.status_var.set("Demographic import cancelled — master database unchanged")
                    return
            result = update_local_demographic_db(workspace, demo)
            self.status_var.set(f"Local demographics updated — {result['created']} new, {result['updated']} refreshed")
            messagebox.showinfo(
                "Local Demographic Database Updated",
                f"New records: {result['created']}\nUpdated records: {result['updated']}\nSkipped: {result['skipped']}\n\nLocal database: {result['database']}\nSnapshot used by tournament prep: {result['snapshot']}",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Demographic update failed", str(exc), parent=self)

    def download_master_bowlers(self):
        """Pull the private permanent-bowler list from Render into the local master DB."""
        url = self.cloud_url_var.get().strip()
        key = self.cloud_admin_key_var.get().strip()
        if not url or not key:
            messagebox.showerror(
                "Cloud settings required",
                "Enter the website URL and admin key on 1 Connect to Website first.",
                parent=self,
            )
            return
        if self._busy:
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        if not messagebox.askyesno(
            "Download Master Database?",
            "Download the private permanent bowler database from the website and merge it into this local workspace?\n\n"
            "Cloud demographic/Jr. Gold values will refresh matching local bowlers. Local-only bowlers and local email addresses will be kept.",
            parent=self,
        ):
            return
        self._busy = True
        self.status_var.set("Downloading permanent bowlers from website…")

        def worker():
            try:
                response = portal_list_bowlers(url, key)
                result = sync_from_cloud_bowlers(workspace, response.get("bowlers", []))
                self.after(0, lambda: finished(result))
            except Exception as exc:
                self.after(0, lambda e=exc: self._job_failed("Master database download", e))

        def finished(result):
            self._busy = False
            self.status_var.set("Local master bowler database refreshed from website")
            details = f"Added locally: {result['created']}\nUpdated locally: {result['updated']}\nSkipped: {result['skipped']}"
            if result.get("errors"):
                details += "\n\nNeeds attention:\n" + "\n".join(result["errors"][:12])
            messagebox.showinfo("Master Database Downloaded", details, parent=self)

        threading.Thread(target=worker, daemon=True).start()

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
        ttk.Label(form, text="Jr. Gold status").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=5)
        jg_controls=ttk.Frame(form); jg_controls.grid(row=8,column=1,sticky="w",pady=5)
        ttk.Radiobutton(jg_controls,text="Not trying",variable=jg_var,value="").pack(side="left")
        ttk.Radiobutton(jg_controls,text="JG — Trying",variable=jg_var,value="JG").pack(side="left",padx=8)
        ttk.Radiobutton(jg_controls,text="Q — Qualified",variable=jg_var,value="Q").pack(side="left")
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

    def _current_roster_data(self):
        path = Path(self.tournament_roster_var.get()).expanduser()
        if not path.is_file():
            raise ValueError("Choose or create the current tournament roster first.")
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        if not headers:
            raise ValueError("The tournament roster has no columns.")
        return path, headers, rows

    def _write_current_roster(self, path, headers, rows):
        required = ["First_Name", "Last_Name", "Gender", "Birthdate_Used", "Division", "Bowler_ID", "Jr_Gold_Status"]
        for col in required:
            if col not in headers:
                headers.append(col)
        order = {d: i for i, d in enumerate(LOCAL_DIVISIONS)}
        rows.sort(key=lambda r: (order.get(str(r.get("Division") or ""), 99), str(r.get("Last_Name") or "").casefold(), str(r.get("First_Name") or "").casefold()))
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        # Keep the per-division convenience CSV files aligned with all_divisions.csv.
        filenames = {"U12 Mixed":"U12_Mixed.csv","U14 Boys":"U14_Boys.csv","U14 Girls":"U14_Girls.csv","U16 Boys":"U16_Boys.csv","U16 Girls":"U16_Girls.csv","U18 Boys":"U18_Boys.csv","U18 Girls":"U18_Girls.csv"}
        for division, filename in filenames.items():
            with Path(path).with_name(filename).open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader(); writer.writerows([r for r in rows if str(r.get("Division") or "").strip() == division])
        # Merge the roster into an already-opened/saved Tournament Manager DB without clearing scores.
        try:
            from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
            db_path = resolve_database_path(path)
            if db_path.is_file():
                db = TournamentDB(db_path)
                try: db.sync_roster(path)
                finally: db.close()
        except Exception as exc:
            messagebox.showwarning("Tournament database update", f"The roster was saved, but the Tournament Manager database could not be refreshed:\n\n{exc}", parent=self)
        self._reconcile_lane_manifest_with_roster(path)

    def _reconcile_lane_manifest_with_roster(self, roster_path):
        manifest_path = Path(self.lane_manifest_var.get()).expanduser()
        if not manifest_path.is_file():
            return
        try:
            manifest = load_manifest(manifest_path)
            desired = {str(b["bowler_id"]): b for b in roster_rows(roster_path)}
            present = set()
            for lane in manifest.get("lanes", []):
                kept = []
                for bowler in lane.get("bowlers", []):
                    bid = str(bowler.get("bowler_id"))
                    if bid in desired:
                        bowler.update({k: desired[bid][k] for k in ("first_name","last_name","division")})
                        kept.append(bowler); present.add(bid)
                lane["bowlers"] = kept
            pairs = manifest.get("lane_pairs", [])
            lane_map = {int(x["lane_no"]): x for x in manifest.get("lanes", [])}
            for bid, bowler in desired.items():
                if bid in present:
                    continue
                choices = []
                for pair in pairs:
                    lane_nos = [int(x) for x in pair.get("lane_nos", [])]
                    members = [b for n in lane_nos for b in lane_map.get(n, {}).get("bowlers", [])]
                    divisions = {str(b.get("division") or "") for b in members}
                    choices.append((0 if bowler["division"] in divisions else 1, len(members), int(pair.get("pair_no", 999)), pair))
                if not choices:
                    continue
                pair = min(choices, key=lambda x: x[:3])[3]
                lane_nos = [int(x) for x in pair.get("lane_nos", [])]
                target = min(lane_nos, key=lambda n: (len(lane_map[n].get("bowlers", [])), n))
                lane_map[target].setdefault("bowlers", []).append(dict(bowler))
            manifest["edited_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            save_manifest(manifest, manifest_path.parent)
            # Update the mobile assignment without erasing scores already submitted.
            url, key = self.cloud_url_var.get().strip(), self.cloud_admin_key_var.get().strip()
            if url and key:
                try: publish_manifest(manifest, url, key, reset_scores=False)
                except Exception as exc: messagebox.showwarning("Mobile scoring update", f"Local roster/lane changes were saved, but mobile scoring could not be refreshed:\n\n{exc}", parent=self)
            # If score sheets had already been generated, refresh the PDF and matching public lane page.
            pdf_path = manifest_path.parent / "lane_scoresheets.pdf"
            if pdf_path.is_file():
                create_scoresheet_pdf(manifest, pdf_path, url, print_title=self.event_name_var.get().strip())
                self.lane_pdf_var.set(str(pdf_path))
                if url and key:
                    try: portal_publish_lane_assignments(url, key, manifest, self.event_date_var.get().strip())
                    except Exception: pass
        except Exception as exc:
            messagebox.showwarning("Lane assignment update", f"The tournament roster was saved, but the existing lane assignment could not be refreshed:\n\n{exc}", parent=self)

    def manage_current_tournament_bowlers(self):
        try:
            roster_path, headers, rows = self._current_roster_data()
        except Exception as exc:
            messagebox.showerror("Current tournament roster", str(exc), parent=self); return
        workspace = Path(self.workspace_var.get()).expanduser()
        win = tk.Toplevel(self); win.title("Manage Current Tournament Bowlers"); win.geometry("980x650"); win.minsize(820,520)
        outer = ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Current Tournament Bowlers",font=("Segoe UI",17,"bold")).pack(anchor="w")
        ttk.Label(outer,text="Add/remove affects only this tournament. Editing a bowler also saves the correction to the master bowler database.",style="Hint.TLabel").pack(anchor="w",pady=(3,10))
        search_var=tk.StringVar(); search=ttk.Entry(outer,textvariable=search_var); search.pack(fill="x",pady=(0,8))
        cols=("name","division","bowler_id","lane")
        tree=ttk.Treeview(outer,columns=cols,show="headings",selectmode="browse")
        for c,label,w in (("name","Bowler",300),("division","Division",150),("bowler_id","Bowler ID",140),("lane","Lane",70)):
            tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True)

        def lane_lookup():
            result={}; mp=Path(self.lane_manifest_var.get()).expanduser()
            if mp.is_file():
                try:
                    m=load_manifest(mp)
                    for lane in m.get("lanes",[]):
                        for b in lane.get("bowlers",[]): result[str(b.get("bowler_id"))]=lane.get("lane_no")
                except Exception: pass
            return result
        def refresh(select_bid=None):
            tree.delete(*tree.get_children()); lanes=lane_lookup(); needle=" ".join(search_var.get().casefold().split())
            for idx,row in enumerate(rows):
                name=f"{row.get('First_Name','')} {row.get('Last_Name','')}".strip(); bid=str(row.get("Bowler_ID") or row.get("BowlerID") or "").strip()
                if needle and needle not in f"{name} {row.get('Division','')} {bid}".casefold(): continue
                iid=f"r{idx}"; tree.insert("","end",iid=iid,values=(name,row.get("Division") or "",bid,lanes.get(bid,"")))
                if select_bid and bid==select_bid: tree.selection_set(iid); tree.see(iid)
        def selected_index():
            sel=tree.selection()
            if not sel: return None
            return int(sel[0][1:])
        def add_from_master():
            current_ids={str(r.get("Bowler_ID") or r.get("BowlerID") or "").strip() for r in rows}
            candidates=[b for b in list_local_bowlers(workspace) if str(b.get("bowler_id") or "") not in current_ids]
            pick=tk.Toplevel(win); pick.title("Add from Master Bowler Database"); pick.geometry("760x520")
            frame=ttk.Frame(pick,padding=12); frame.pack(fill="both",expand=True)
            sv=tk.StringVar(); ttk.Entry(frame,textvariable=sv).pack(fill="x",pady=(0,8))
            t=ttk.Treeview(frame,columns=("name","division","id"),show="headings",selectmode="extended")
            for c,l,w in (("name","Bowler",300),("division","Division",150),("id","Bowler ID",150)): t.heading(c,text=l); t.column(c,width=w,anchor="w")
            t.pack(fill="both",expand=True)
            by_iid={}
            def load(*_):
                t.delete(*t.get_children()); q=sv.get().casefold().strip(); by_iid.clear()
                for i,b in enumerate(candidates):
                    text=f"{b['first_name']} {b['last_name']} {b.get('division','')} {b.get('bowler_id','')}".casefold()
                    if q and q not in text: continue
                    iid=f"b{i}"; by_iid[iid]=b; t.insert("","end",iid=iid,values=(f"{b['first_name']} {b['last_name']}",b.get("division") or "",b.get("bowler_id") or ""))
            def add_selected():
                selected=t.selection()
                if not selected: return
                questionable=[]
                for iid in selected:
                    b=by_iid[iid]
                    missing=[label for label,key in (("division","division"),("birthdate","birthdate"),("USBC ID","usbc_id")) if not str(b.get(key) or "").strip()]
                    if missing:
                        questionable.append(f"{b.get('first_name','')} {b.get('last_name','')}: missing {', '.join(missing)}")
                if questionable:
                    msg="These master-database records have missing/questionable information:\n\n"+"\n".join(questionable[:12])+"\n\nAdd them to this tournament anyway?"
                    if not messagebox.askyesno("Confirm questionable additions",msg,parent=pick,icon="warning"):
                        return
                for iid in selected:
                    b=by_iid[iid]; row={h:"" for h in headers}
                    row.update({"First_Name":b["first_name"],"Last_Name":b["last_name"],"Gender":b.get("gender") or "","Birthdate_Used":b.get("birthdate") or "","Demographic_DOB":b.get("birthdate") or "","Division":b.get("division") or "","Age_Division":str(b.get("division") or "").split()[0],"Bowler_ID":b.get("bowler_id") or "","Jr_Gold_Status":b.get("jr_gold_status") or "","Demographic_Entry":"YES","Paid_Entry":"YES","Designation":"MANUAL TOURNAMENT ADD"})
                    rows.append(row)
                self._write_current_roster(roster_path,headers,rows); pick.destroy(); refresh()
            sv.trace_add("write",load); load(); ttk.Button(frame,text="Add Selected to Tournament",command=add_selected,style="Primary.TButton").pack(anchor="e",pady=(8,0))
        def add_unlisted_bowler():
            add = tk.Toplevel(win); add.title("Add Unlisted Bowler to This Tournament"); add.geometry("520x420")
            f = ttk.Frame(add, padding=14); f.pack(fill="both", expand=True); f.columnconfigure(1, weight=1)
            vars = {k: tk.StringVar() for k in ("first","last","gender","birthdate","division")}
            fields = [("first","First name"),("last","Last name"),("gender","Gender"),("birthdate","Birthdate"),("division","Division")]
            for r,(key,label) in enumerate(fields):
                ttk.Label(f,text=label).grid(row=r,column=0,sticky="w",padx=(0,8),pady=6)
                if key == "gender":
                    w = ttk.Combobox(f,textvariable=vars[key],values=["Boy","Girl"],state="readonly")
                elif key == "division":
                    w = ttk.Combobox(f,textvariable=vars[key],values=list(LOCAL_DIVISIONS),state="readonly")
                else:
                    w = ttk.Entry(f,textvariable=vars[key])
                w.grid(row=r,column=1,sticky="ew",pady=6)
            ttk.Label(
                f,
                text="Use this only when the bowler cannot be found in the master database. This adds them to the current tournament only and does not create a permanent bowler record.",
                style="Hint.TLabel", wraplength=460, justify="left"
            ).grid(row=len(fields),column=0,columnspan=2,sticky="w",pady=(8,12))
            def save_unlisted():
                first=vars["first"].get().strip(); last=vars["last"].get().strip(); division=vars["division"].get().strip()
                if not first or not last or not division:
                    messagebox.showerror("Missing information","First name, last name, and division are required.",parent=add); return
                if not messagebox.askyesno(
                    "Confirm unlisted bowler",
                    f"{first} {last} is being added to this tournament without a permanent master-database match.\n\nContinue?",
                    parent=add, icon="warning"
                ):
                    return
                row={h:"" for h in headers}
                temp_id="TEMP-"+__import__("uuid").uuid4().hex[:12].upper()
                row.update({
                    "First_Name":first,"Last_Name":last,"Gender":vars["gender"].get().strip(),
                    "Birthdate_Used":vars["birthdate"].get().strip(),"Demographic_DOB":vars["birthdate"].get().strip(),
                    "Division":division,"Age_Division":division.split()[0],"Bowler_ID":temp_id,
                    "Demographic_Entry":"NO","Paid_Entry":"YES","Designation":"MANUAL UNLISTED TOURNAMENT ADD"
                })
                rows.append(row); self._write_current_roster(roster_path,headers,rows); add.destroy(); refresh(temp_id)
            ttk.Button(f,text="Add to Current Tournament",command=save_unlisted,style="Primary.TButton").grid(row=len(fields)+1,column=0,columnspan=2,sticky="ew")

        def remove_selected():
            idx=selected_index()
            if idx is None: messagebox.showinfo("Select bowler","Select a tournament bowler first.",parent=win); return
            row=rows[idx]; name=f"{row.get('First_Name','')} {row.get('Last_Name','')}".strip()
            if not messagebox.askyesno("Remove from tournament",f"Remove {name} from this tournament only?\n\nTheir master database record will be kept.",parent=win): return
            rows.pop(idx); self._write_current_roster(roster_path,headers,rows); refresh()
        def edit_selected():
            idx=selected_index()
            if idx is None: messagebox.showinfo("Select bowler","Select a tournament bowler first.",parent=win); return
            row=rows[idx]; bid=str(row.get("Bowler_ID") or row.get("BowlerID") or "").strip()
            master=next((b for b in list_local_bowlers(workspace) if str(b.get("bowler_id") or "")==bid),None)
            if not master:
                messagebox.showerror("Master record not found","This tournament bowler is not linked to a master Bowler ID. Add/correct them in the Bowler Database first.",parent=win); return
            edit=tk.Toplevel(win); edit.title("Edit Tournament Bowler / Master Record"); edit.geometry("520x500")
            f=ttk.Frame(edit,padding=14); f.pack(fill="both",expand=True); f.columnconfigure(1,weight=1)
            vars={k:tk.StringVar(value=str(master.get(k) or "")) for k in ("first_name","last_name","gender","birthdate","division","usbc_id","jr_gold_status","email")}
            labels=[("first_name","First name"),("last_name","Last name"),("gender","Gender"),("birthdate","Birthdate"),("division","Division"),("usbc_id","USBC ID"),("jr_gold_status","Jr. Gold status"),("email","Email")]
            for n,(key,label) in enumerate(labels):
                ttk.Label(f,text=label).grid(row=n,column=0,sticky="w",padx=(0,8),pady=5)
                values=None
                if key=="gender": values=["Boy","Girl"]
                elif key=="division": values=[""]+list(LOCAL_DIVISIONS)
                elif key=="jr_gold_status": values=["","JG","Q"]
                if values is not None: w=ttk.Combobox(f,textvariable=vars[key],values=values,state="readonly")
                else: w=ttk.Entry(f,textvariable=vars[key])
                w.grid(row=n,column=1,sticky="ew",pady=5)
            def save_edit():
                try:
                    changes=[]
                    for key,label in (("first_name","First name"),("last_name","Last name"),("gender","Gender"),("birthdate","Birthdate"),("division","Division"),("usbc_id","USBC ID"),("jr_gold_status","Jr. Gold status"),("email","Email")):
                        before=str(master.get(key) or "").strip(); after=str(vars[key].get() or "").strip()
                        if before != after:
                            changes.append(f"{label}: {before or '—'} → {after or '—'}")
                    if changes:
                        msg="This edit will change the permanent master-database record and the current tournament:\n\n"+"\n".join(changes[:12])+"\n\nSave these changes?"
                        if not messagebox.askyesno("Confirm permanent bowler changes",msg,parent=edit,icon="warning"):
                            return
                    stable=update_local_bowler(workspace,master["identity_key"],first_name=vars["first_name"].get(),last_name=vars["last_name"].get(),gender=vars["gender"].get(),birthdate=vars["birthdate"].get(),usbc_id=vars["usbc_id"].get(),division=vars["division"].get(),jr_gold_status=vars["jr_gold_status"].get(),email=vars["email"].get())
                    updated=next(b for b in list_local_bowlers(workspace) if b.get("bowler_id")==stable)
                    row.update({"First_Name":updated["first_name"],"Last_Name":updated["last_name"],"Gender":updated.get("gender") or "","Birthdate_Used":updated.get("birthdate") or "","Demographic_DOB":updated.get("birthdate") or "","Division":updated.get("division") or "","Age_Division":str(updated.get("division") or "").split()[0],"Bowler_ID":stable,"Jr_Gold_Status":updated.get("jr_gold_status") or ""})
                    self._write_current_roster(roster_path,headers,rows); edit.destroy(); refresh(stable)
                except Exception as exc: messagebox.showerror("Could not save bowler",str(exc),parent=edit)
            ttk.Button(f,text="Save to Master Database + Tournament",command=save_edit,style="Primary.TButton").grid(row=len(labels),column=0,columnspan=2,sticky="ew",pady=(14,0))
        buttons=ttk.Frame(outer); buttons.pack(fill="x",pady=(10,0))
        ttk.Button(buttons,text="Add from Master Database",command=add_from_master,style="Primary.TButton").pack(side="left")
        ttk.Button(buttons,text="Add Unlisted Bowler",command=add_unlisted_bowler,style="Secondary.TButton").pack(side="left",padx=(8,0))
        ttk.Button(buttons,text="Edit Bowler",command=edit_selected,style="Secondary.TButton").pack(side="left",padx=8)
        ttk.Button(buttons,text="Remove from This Tournament",command=remove_selected,style="Secondary.TButton").pack(side="left")
        ttk.Button(buttons,text="Close",command=win.destroy,style="Secondary.TButton").pack(side="right")
        search_var.trace_add("write",lambda *_:refresh()); refresh()

    def show_missing_demographics(self):
        try:
            registration, _ = self._require_workspace_tournament_files()
        except Exception as exc:
            messagebox.showerror("Tournament entries required", str(exc), parent=self)
            return
        try:
            missing=missing_from_registration(Path(self.workspace_var.get()).expanduser(), registration)
        except Exception as exc:
            messagebox.showerror("Missing demographics", str(exc), parent=self)
            return
        win=tk.Toplevel(self); win.title("Tournament Entries Missing from Master Database"); win.geometry("760x520")
        outer=ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text=f"Missing demographic records: {len(missing)}",font=("Segoe UI",15,"bold")).pack(anchor="w",pady=(0,8))
        cols=("row","name","birthdate")
        tree=ttk.Treeview(outer,columns=cols,show="headings")
        for c,label,w in (("row","Entry Row",90),("name","Bowler",360),("birthdate","Birthdate",160)):
            tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True)
        for item in missing:
            tree.insert("","end",values=(item.get("row"),f"{item.get('first_name','')} {item.get('last_name','')}",item.get("birthdate") or ""))
        ttk.Label(outer,text="These bowlers are in the tournament entries but could not be matched to the local master bowler database.",wraplength=700,justify="left").pack(anchor="w",pady=(8,0))

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
        if self._busy:
            return
        self._sync_pipeline_paths()
        try:
            registration, transactions = self._require_workspace_tournament_files()
        except Exception as exc:
            messagebox.showerror("Tournament files required", str(exc), parent=self)
            self.show_page("files")
            return
        workspace = Path(self.workspace_var.get()).expanduser()
        try:
            local_bowler_db = require_local_bowler_database(workspace)
        except Exception as exc:
            messagebox.showerror("Master bowler database required", str(exc), parent=self)
            self.show_page("database")
            return
        if not self._archive_inputs(
            "full_prep_pipeline",
            ("tournament_registration", registration),
            ("square_transactions", transactions),
            ("local_bowler_database", local_bowler_db),
        ):
            return

        payment = workspace / "payment_status.csv"
        demographic = workspace / "paid_demographic_check.csv"
        division_dir = workspace / "tournament_divisions"
        commands = [
            ("1 of 3 — Payment check", [sys.executable, str(PAYMENT_SCRIPT), str(registration), str(transactions), "--output", str(payment)]),
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
                self.after(0, lambda err=exc: self._job_failed("Tournament setup", err))
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
        self.status_var.set("Tournament setup complete — all_divisions.csv is ready")

        details = self._pipeline_summary(payment, demographic, division_dir)
        messagebox.showinfo(
            "Tournament Setup Complete",
            "All preparation steps finished successfully.\n\n" + details,
            parent=self,
        )
        self.show_page("setup")

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
            messagebox.showerror("Manage PINs", str(exc), parent=self)
            return

        win = tk.Toplevel(self)
        win.title("Manage PINs")
        win.geometry("700x470")
        win.minsize(650, 430)
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="PIN Management", font=("Segoe UI", 15, "bold")).pack(anchor="w")
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
                messagebox.showerror("Manage PINs", str(exc), parent=win)

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
                messagebox.showerror("Manage PINs", str(exc), parent=win)

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
                messagebox.showerror("Manage PINs", str(exc), parent=win)

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
                messagebox.showerror("Manage PINs", str(exc), parent=win)

        # Native tk.Button is used here deliberately instead of themed ttk.Button so
        # button captions remain visible across Windows/macOS Tk themes.
        btn_opts = dict(font=("Segoe UI", 9, "bold"), padx=10, pady=6, relief="raised", bd=1)
        tk.Button(
            addrow, text="Add Scorer", command=add,
            bg="#1f6feb", fg="white", activebackground="#1859bd", activeforeground="white",
            **btn_opts
        ).pack(side="left")

        admin_box = ttk.LabelFrame(frame, text="Website Admin PIN", padding=10)
        admin_box.pack(fill="x", pady=(8, 8))
        admin_status = tk.StringVar(value="Checking...")
        ttk.Label(admin_box, textvariable=admin_status).pack(side="left")
        def refresh_admin_status():
            try:
                status=get_admin_pin_status(cloud_url,admin_key)
                admin_status.set("Configured" if status.get("configured") else "Not configured")
            except Exception as exc:
                admin_status.set(f"Unavailable: {exc}")
        def set_website_admin_pin():
            pin=simpledialog.askstring("Set Website Admin PIN","Enter the 6-digit PIN required for website Admin Controls:",parent=win,show="*")
            if pin is None: return
            try:
                set_admin_pin(cloud_url,admin_key,pin); refresh_admin_status(); messagebox.showinfo("Admin PIN","Website Admin PIN updated. Existing website admin sessions were signed out.",parent=win)
            except Exception as exc:
                messagebox.showerror("Admin PIN",str(exc),parent=win)
        tk.Button(admin_box,text="Set Admin PIN",command=set_website_admin_pin,**btn_opts).pack(side="right")
        refresh_admin_status()

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

    def _lane_groups_path(self):
        return self._lane_folder() / "lane_groups.json"

    def _load_lane_groups(self):
        path=self._lane_groups_path()
        if not path.is_file(): return {}
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: return {}

    def manage_lane_groups(self):
        roster=Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            messagebox.showerror("Roster required","Choose the tournament roster first.",parent=self); return
        try: rows=roster_rows(roster)
        except Exception as exc: messagebox.showerror("Lane Groups",str(exc),parent=self); return
        groups=self._load_lane_groups()
        win=tk.Toplevel(self); win.title("Lane Pair Groups"); win.geometry("850x600"); win.minsize(700,450)
        outer=ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Keep Bowlers on the Same Lane Pair",font=("Segoe UI",16,"bold")).pack(anchor="w")
        ttk.Label(outer,text="Select one or more bowlers, type any group ID (for example FAMILY1), and apply it. Bowlers sharing an ID will stay on the same pair during automatic assignment.",wraplength=800,justify="left").pack(anchor="w",pady=(3,10))
        search_var=tk.StringVar()
        search_row=ttk.Frame(outer); search_row.pack(fill="x",pady=(0,8))
        ttk.Label(search_row,text="Search bowler:").pack(side="left")
        ttk.Entry(search_row,textvariable=search_var).pack(side="left",fill="x",expand=True,padx=(8,0))
        cols=("name","division","group")
        tree=ttk.Treeview(outer,columns=cols,show="headings",selectmode="extended")
        for c,label,w in (("name","Bowler",320),("division","Division",160),("group","Group ID",180)):
            tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True)
        def refresh_rows(*_):
            selected=set(tree.selection())
            tree.delete(*tree.get_children())
            q=" ".join(search_var.get().casefold().split())
            for b in rows:
                name=f"{b['first_name']} {b['last_name']}".strip()
                hay=f"{name} {b.get('division','')} {groups.get(str(b['bowler_id']),'')}".casefold()
                if q and q not in hay:
                    continue
                iid=str(b["bowler_id"]); tree.insert("","end",iid=iid,values=(name,b['division'],groups.get(iid,"")))
                if iid in selected: tree.selection_add(iid)
        search_var.trace_add("write",refresh_rows); refresh_rows()
        controls=ttk.Frame(outer); controls.pack(fill="x",pady=(10,0))
        gid=tk.StringVar()
        ttk.Label(controls,text="Group ID:").pack(side="left")
        ttk.Entry(controls,textvariable=gid,width=18).pack(side="left",padx=6)
        def apply_group(clear=False):
            selected=tree.selection()
            if not selected:
                messagebox.showinfo("Select bowlers","Select one or more bowlers first.",parent=win); return
            value="" if clear else gid.get().strip()
            if not clear and not value:
                messagebox.showinfo("Group ID","Type a group ID first.",parent=win); return
            for bid in selected:
                if value: groups[str(bid)]=value
                else: groups.pop(str(bid),None)
                if tree.exists(bid):
                    vals=list(tree.item(bid,"values")); vals[2]=value; tree.item(bid,values=vals)
            self._lane_groups_path().write_text(json.dumps(groups,indent=2),encoding="utf-8")
            refresh_rows()
        ttk.Button(controls,text="Apply to Selected",command=apply_group,style="Primary.TButton").pack(side="left")
        ttk.Button(controls,text="Clear Group",command=lambda:apply_group(True),style="Secondary.TButton").pack(side="left",padx=8)
        ttk.Button(controls,text="Done",command=win.destroy,style="Secondary.TButton").pack(side="right")


    def build_or_edit_lane_assignment(self):
        """Create the lane assignment once; thereafter edit it without resetting the draw."""
        path = Path(self.lane_manifest_var.get()).expanduser()
        if path.is_file():
            self.edit_lane_assignments()
            return
        fallback = self._lane_folder() / "lane_manifest.json"
        if fallback.is_file():
            self.lane_manifest_var.set(str(fallback))
            self.edit_lane_assignments()
            return
        self.prepare_lane_scoring()

    def prepare_lane_scoring(self):
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            messagebox.showerror("Roster not found", "Choose an existing all_divisions.csv file first.", parent=self)
            return
        if self._busy: return
        try:
            lane_count=int(self.lane_count_var.get()); cloud_url=self.cloud_url_var.get().strip(); admin_key=self.cloud_admin_key_var.get().strip()
            event_name=self.event_name_var.get().strip() or "Tough Shots Tournament"
            if not cloud_url or not admin_key: raise ValueError("Enter both the cloud scoring URL and cloud admin key.")
        except Exception as exc:
            messagebox.showerror("Lane setup",str(exc),parent=self); return
        self._save_workspace_state(); folder=self._lane_folder(); existing_manifest=folder/"lane_manifest.json"; existing_id=None
        if existing_manifest.is_file():
            if not messagebox.askyesno("Create a New Lane Draw?","This replaces the current assignment and republishes the mobile scoring pages. Continue?",parent=self): return
            try: existing_id=load_manifest(existing_manifest).get("tournament_id")
            except Exception: pass
        if not self._archive_inputs("lane_assignment",("tournament_roster",roster)): return
        self._busy=True; self.status_var.set("Building balanced lane assignment and publishing mobile scoring…")
        groups=self._load_lane_groups()
        def worker():
            try:
                manifest=assign_lanes(roster,lane_count,tournament_name=event_name,tournament_id=existing_id,group_assignments=groups)
                manifest_path,assignment_csv=save_manifest(manifest,folder)
                publish_manifest(manifest,cloud_url,admin_key); save_manifest(manifest,folder)
                self.after(0,lambda:self._lane_prepare_complete(manifest_path,assignment_csv,manifest))
            except Exception as exc: self.after(0,lambda err=exc:self._job_failed("Lane scoring setup",err))
        threading.Thread(target=worker,daemon=True).start()

    def _lane_prepare_complete(self, manifest_path, assignment_csv, manifest):
        self._busy=False; self.lane_manifest_var.set(str(manifest_path)); self.lane_pdf_var.set("")
        counts=[len(x["bowlers"]) for x in manifest["lanes"]]; by_lane={int(x["lane_no"]):len(x["bowlers"]) for x in manifest["lanes"]}
        pair_counts=[sum(by_lane.get(int(n),0) for n in pair.get("lane_nos",[])) for pair in manifest.get("lane_pairs",[])]
        self.status_var.set(f"Lane assignment ready — review it before generating score sheets")
        messagebox.showinfo("Lane Assignment Ready",f"Assignment published successfully.\n\nBowlers: {sum(counts)}\nScorecards: {len(pair_counts)}\nBowlers per scorecard: {min(pair_counts)}-{max(pair_counts)}\n\nThe review window will open next. Move anyone you need, save, then generate score sheets.",parent=self)
        self.after(100, self.edit_lane_assignments)

    def edit_lane_assignments(self):
        path=Path(self.lane_manifest_var.get()).expanduser()
        if not path.is_file(): messagebox.showerror("No assignment","Prepare a lane assignment first.",parent=self); return
        manifest=load_manifest(path)
        win=tk.Toplevel(self); win.title("Review / Move Lane Assignments"); win.geometry("900x620"); win.minsize(760,480)
        outer=ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Review Lane Assignments",font=("Segoe UI",16,"bold")).pack(anchor="w")
        ttk.Label(outer,text="Move bowlers without rebuilding the assignment. Existing mobile scores are preserved.",style="Hint.TLabel").pack(anchor="w",pady=(2,8))
        search_var=tk.StringVar()
        search_row=ttk.Frame(outer); search_row.pack(fill="x",pady=(0,8))
        ttk.Label(search_row,text="Search bowler:").pack(side="left")
        ttk.Entry(search_row,textvariable=search_var).pack(side="left",fill="x",expand=True,padx=(8,0))
        cols=("lane","name","division")
        tree=ttk.Treeview(outer,columns=cols,show="headings",selectmode="browse")
        for c,label,w in (("lane","Lane",80),("name","Bowler",340),("division","Division",180)):
            tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True,pady=(10,8))
        def refresh(select=None):
            tree.delete(*tree.get_children())
            needle=" ".join(search_var.get().casefold().split())
            for lane in manifest.get("lanes",[]):
                for b in lane.get("bowlers",[]):
                    name=f"{b['first_name']} {b['last_name']}".strip()
                    hay=f"{name} {b.get('division','')} {lane.get('lane_no','')}".casefold()
                    if needle and needle not in hay:
                        continue
                    tree.insert("","end",iid=str(b["bowler_id"]),values=(lane["lane_no"],name,b['division']))
            if select and tree.exists(str(select)): tree.selection_set(str(select)); tree.see(str(select))
        controls=ttk.Frame(outer); controls.pack(fill="x")
        lane_var=tk.StringVar(value="1")
        ttk.Label(controls,text="Move selected bowler to lane:").pack(side="left")
        ttk.Spinbox(controls,textvariable=lane_var,from_=1,to=max(int(x["lane_no"]) for x in manifest["lanes"]),width=7).pack(side="left",padx=6)
        def move():
            sel=tree.selection()
            if not sel: messagebox.showinfo("Select bowler","Select a bowler first.",parent=win); return
            try: move_bowler_to_lane(manifest,sel[0],int(lane_var.get())); refresh(sel[0])
            except Exception as exc: messagebox.showerror("Move bowler",str(exc),parent=win)
        ttk.Button(controls,text="Move",command=move,style="Primary.TButton").pack(side="left")
        def save_publish():
            try:
                save_manifest(manifest,path.parent)
                publish_manifest(manifest,self.cloud_url_var.get().strip(),self.cloud_admin_key_var.get().strip(), reset_scores=False)
                save_manifest(manifest,path.parent)
                self.lane_pdf_var.set("")
                messagebox.showinfo("Assignments Saved","Changes were saved and republished. Generate score sheets when you are ready.",parent=win)
            except Exception as exc: messagebox.showerror("Save assignments",str(exc),parent=win)
        ttk.Button(controls,text="Save + Republish",command=save_publish,style="Secondary.TButton").pack(side="left",padx=8)
        ttk.Button(controls,text="Close",command=win.destroy,style="Secondary.TButton").pack(side="right")
        search_var.trace_add("write",lambda *_:refresh())
        refresh()

    def generate_lane_scoresheets(self):
        path=Path(self.lane_manifest_var.get()).expanduser()
        if not path.is_file(): messagebox.showerror("No assignment","Prepare a lane assignment first.",parent=self); return
        try:
            pdf=path.parent/"lane_scoresheets.pdf"
            manifest = load_manifest(path)
            create_scoresheet_pdf(manifest,pdf,self.cloud_url_var.get().strip(),print_title=self.event_name_var.get().strip())
            self.lane_pdf_var.set(str(pdf)); self._save_workspace_state()
            try:
                portal_publish_lane_assignments(self.cloud_url_var.get().strip(), self.cloud_admin_key_var.get().strip(), manifest, self.event_date_var.get().strip())
                note = "Created score sheets and published the matching lane assignments."
            except Exception as publish_exc:
                note = f"Score sheets were created, but the public lane-assignment page could not be updated.\n\n{publish_exc}"
            messagebox.showinfo("Score Sheets Ready",f"{note}\n\n{pdf}",parent=self)
        except Exception as exc: messagebox.showerror("Score sheets",str(exc),parent=self)

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
                publish_manifest(manifest, cloud_url, admin_key, reset_scores=False)
                save_manifest(manifest, manifest_path.parent)
                pdf_path = manifest_path.parent / "lane_scoresheets.pdf"
                create_scoresheet_pdf(manifest, pdf_path, cloud_url, print_title=self.event_name_var.get().strip())
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
            raise ValueError("Enter the website URL and admin key on 1 Connect to Website first.")
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

    def manage_bowler_jg_status(self):
        """Manage Jr. Gold status in the local master DB, then mirror it to cloud."""
        workspace = Path(self.workspace_var.get()).expanduser()
        try:
            require_local_bowler_database(workspace)
        except Exception as exc:
            messagebox.showerror("Master bowler database required", str(exc), parent=self)
            return

        win = tk.Toplevel(self)
        win.title("Manage Bowler JG / Q Status")
        win.geometry("900x600")
        win.minsize(760, 470)
        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(2, weight=1); outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="Jr. Gold Status", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="The local master database is the source of truth. Status changes are also sent to the website when connected.", wraplength=820, justify="left").grid(row=1, column=0, sticky="w", pady=(3, 10))

        search_var = tk.StringVar()
        top = ttk.Frame(outer); top.grid(row=2, column=0, sticky="new", pady=(0, 8)); top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(top, textvariable=search_var).grid(row=0, column=1, sticky="ew")

        frame = ttk.Frame(outer); frame.grid(row=3, column=0, sticky="nsew"); outer.rowconfigure(3, weight=1); frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        cols=("name","division","id","jg")
        tree=ttk.Treeview(frame,columns=cols,show="headings",selectmode="browse")
        for c,label,w in (("name","Bowler",300),("division","Division",150),("id","Bowler ID",150),("jg","JG Status",100)):
            tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.grid(row=0,column=0,sticky="nsew")
        sb=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); sb.grid(row=0,column=1,sticky="ns"); tree.configure(yscrollcommand=sb.set)

        choice=tk.StringVar(value="")
        controls=ttk.Frame(outer); controls.grid(row=4,column=0,sticky="ew",pady=(10,0))
        ttk.Radiobutton(controls,text="Blank — Not trying",variable=choice,value="").pack(side="left")
        ttk.Radiobutton(controls,text="JG — Trying",variable=choice,value="JG").pack(side="left",padx=10)
        ttk.Radiobutton(controls,text="Q — Qualified",variable=choice,value="Q").pack(side="left")
        status=tk.StringVar(value="")
        ttk.Label(outer,textvariable=status,style="Hint.TLabel").grid(row=5,column=0,sticky="w",pady=(8,0))

        rows_by_id={}
        def refresh(*_):
            tree.delete(*tree.get_children()); rows_by_id.clear()
            q=search_var.get().strip()
            try:
                rows=list_local_bowlers(workspace,q)
            except Exception as exc:
                status.set(str(exc)); return
            for b in rows:
                bid=str(b.get("bowler_id") or "")
                if not bid: continue
                rows_by_id[bid]=b
                tree.insert("","end",iid=bid,values=(f"{proper_name(b.get('first_name'))} {proper_name(b.get('last_name'))}".strip(),b.get("division") or "",bid,b.get("jr_gold_status") or "—"))
            status.set(f"{len(rows)} bowler(s)")
        def on_select(_=None):
            sel=tree.selection()
            if sel:
                choice.set(str(rows_by_id.get(sel[0],{}).get("jr_gold_status") or ""))
        def apply():
            sel=tree.selection()
            if not sel:
                messagebox.showinfo("Select bowler","Select a bowler first.",parent=win); return
            bid=sel[0]; state=choice.get()
            try:
                set_local_jr_gold_by_bowler_ids(workspace,[bid],state)
            except Exception as exc:
                messagebox.showerror("Could not update status",str(exc),parent=win); return
            cloud_note=""
            try:
                url,key=self._portal_credentials()
                portal_set_jr_gold(url,key,bid,state)
                cloud_note=" Website updated too."
            except Exception:
                cloud_note=" Local database updated. Website was not updated; use Sync Permanent Bowlers when connected."
            refresh(); tree.selection_set(bid); tree.see(bid)
            status.set(("Status set to " + (state or "blank") + ".") + cloud_note)
        ttk.Button(controls,text="Apply Status",command=apply,style="Primary.TButton").pack(side="right")
        tree.bind("<<TreeviewSelect>>",on_select)
        search_var.trace_add("write",refresh)
        refresh()

    def manage_permanent_bowlers(self):
        try: url,key=self._portal_credentials()
        except Exception as exc: messagebox.showerror("Cloud settings",str(exc),parent=self); return
        win=tk.Toplevel(self); win.title("Private Permanent Bowler Database"); win.geometry("930x600"); win.minsize(780,440)
        outer=ttk.Frame(win,padding=14); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Permanent Bowlers",font=("Segoe UI",16,"bold")).pack(anchor="w")
        ttk.Label(outer,text="Select a bowler, choose their Jr. Gold status, and click Apply. This list remains private.",wraplength=850,justify="left").pack(anchor="w",pady=(3,10))
        cols=("id","name","gender","birthdate","division","jg")
        tree=ttk.Treeview(outer,columns=cols,show="headings",selectmode="browse")
        widths=(125,220,75,100,120,85); labels=("Bowler ID","Bowler","Gender","Birthdate","Division","JG Status")
        for c,label,w in zip(cols,labels,widths): tree.heading(c,text=label); tree.column(c,width=w,anchor="w")
        tree.pack(fill="both",expand=True)
        status=tk.StringVar(value="Loading…"); ttk.Label(outer,textvariable=status).pack(anchor="w",pady=(7,4))
        controls=ttk.Frame(outer); controls.pack(fill="x")
        choice=tk.StringVar(value="Blank — Not trying")
        choices=["Blank — Not trying","JG — Trying to qualify","Q — Qualified"]
        ttk.Label(controls,text="Jr. Gold status:").pack(side="left")
        combo=ttk.Combobox(controls,textvariable=choice,values=choices,state="readonly",width=24); combo.pack(side="left",padx=6)
        def refresh(select=None):
            try:
                data=portal_list_bowlers(url,key); tree.delete(*tree.get_children())
                for b in data.get("bowlers",[]):
                    tree.insert("","end",iid=b["bowler_id"],values=(b["bowler_id"],f"{b['first_name']} {b['last_name']}",b["gender"],b["birthdate"],b["division"],b["jr_gold_state"] or "—"))
                if select and tree.exists(select): tree.selection_set(select); tree.see(select)
                status.set(f"{len(data.get('bowlers',[]))} permanent bowlers")
            except Exception as exc: status.set(str(exc)); messagebox.showerror("Could not load bowlers",str(exc),parent=win)
        def on_select(_=None):
            sel=tree.selection()
            if not sel: return
            state=tree.item(sel[0],"values")[5]
            choice.set("JG — Trying to qualify" if state=="JG" else ("Q — Qualified" if state=="Q" else "Blank — Not trying"))
        def apply():
            sel=tree.selection()
            if not sel: messagebox.showinfo("Select bowler","Select a bowler first.",parent=win); return
            state="JG" if choice.get().startswith("JG") else ("Q" if choice.get().startswith("Q") else "")
            try:
                portal_set_jr_gold(url,key,sel[0],state); refresh(sel[0]); status.set("Jr. Gold status updated.")
            except Exception as exc: messagebox.showerror("Could not update status",str(exc),parent=win)
        ttk.Button(controls,text="Apply Status",command=apply,style="Primary.TButton").pack(side="left")
        ttk.Button(controls,text="Refresh",command=refresh,style="Secondary.TButton").pack(side="right")
        tree.bind("<<TreeviewSelect>>",on_select)
        refresh()

    def _portal_roster_ready(self):
        roster=Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file(): raise ValueError("Choose or create tournament_divisions/all_divisions.csv first.")
        manifest=Path(self.lane_manifest_var.get()).expanduser()
        return roster, (manifest if manifest.is_file() else None)

    def _standings_game_mismatches(self):
        """Return bowlers with fewer entered games than peers in their division.

        This is intentionally a warning, not a hard stop: tournament-day data can
        be incomplete on purpose, but the operator should know before publishing
        or printing standings.
        """
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            return []
        from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
        db = TournamentDB(resolve_database_path(roster))
        try:
            if not db.roster_loaded():
                db.import_roster(roster)
            issues = []
            for division in db.divisions():
                rows = db.qualifying_rows(division)
                if not rows:
                    continue
                counts = []
                for row in rows:
                    count = sum(v is not None for v in (row.get("scores") or []))
                    counts.append((row, count))
                most = max((c for _, c in counts), default=0)
                if most <= 0:
                    continue
                for row, count in counts:
                    if count < most:
                        name = f"{proper_name(row.get('first_name'))} {proper_name(row.get('last_name'))}".strip()
                        issues.append((division, name, count, most))
            return issues
        finally:
            db.close()

    def _confirm_standings_completeness(self, action_label):
        issues = self._standings_game_mismatches()
        if not issues:
            return True
        lines = []
        for division, name, count, most in issues[:18]:
            lines.append(f"• {division}: {name} has {count} game(s); others have up to {most}.")
        if len(issues) > 18:
            lines.append(f"• ...and {len(issues) - 18} more bowler(s).")
        message = (
            f"Some bowlers have fewer entered games than other bowlers in their division.\n\n"
            + "\n".join(lines)
            + f"\n\nContinue with {action_label} anyway?"
        )
        return messagebox.askyesno("Incomplete Standings", message, parent=self, icon="warning")

    def _require_active_tournament_for_printing(self):
        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            raise ValueError("Choose or reload the current tournament roster first.")
        workspace = Path(self.workspace_var.get()).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        self._save_workspace_state()
        return roster, workspace

    def _finish_print_job(self, label, path, extra=""):
        sent = send_pdf_to_printer(path)
        msg = ("Sent to the default printer.\n\n" if sent else "Created PDF. Open it to print.\n\n")
        if extra: msg += extra + "\n\n"
        msg += str(path)
        messagebox.showinfo(label, msg, parent=self)

    def print_all_qualifying(self):
        try:
            if not self._confirm_standings_completeness("printing qualifying standings"):
                return
            roster, workspace = self._require_active_tournament_for_printing()
            path = create_qualifying_pdf(roster, workspace, self.event_name_var.get().strip())
            self._finish_print_job("Qualifying Printing", path)
        except Exception as exc:
            messagebox.showerror("Qualifying Printing", str(exc), parent=self)

    def print_all_jr_gold(self):
        try:
            if not self._confirm_standings_completeness("printing Jr. Gold standings"):
                return
            roster, workspace = self._require_active_tournament_for_printing()
            path = create_jr_gold_pdf(roster, workspace, self.event_name_var.get().strip())
            self._finish_print_job("Jr. Gold Printing", path)
        except Exception as exc:
            messagebox.showerror("Jr. Gold Printing", str(exc), parent=self)

    def print_current_match_round(self):
        try:
            roster, workspace = self._require_active_tournament_for_printing()
            path, round_no, divisions = create_current_brackets_pdf(roster, workspace, self.event_name_var.get().strip())
            self._finish_print_job("Bracket Printing", path, f"Tournament round {round_no}\nDivisions: {', '.join(divisions)}")
        except Exception as exc:
            messagebox.showerror("Bracket Printing", str(exc), parent=self)

    def open_jr_gold_settings(self):
        try:
            roster = Path(self.tournament_roster_var.get()).expanduser()
            if not roster.is_file(): raise ValueError("Choose or reload the current tournament roster first.")
            from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
            db = TournamentDB(resolve_database_path(roster))
            if not db.roster_loaded(): db.import_roster(roster)
        except Exception as exc:
            messagebox.showerror("Jr. Gold Settings", str(exc), parent=self); return
        win=tk.Toplevel(self); win.title("Jr. Gold Qualifier Settings"); win.geometry("620x610"); win.resizable(False,False)
        outer=ttk.Frame(win,padding=16); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="Jr. Gold Qualifier",font=("Segoe UI",16,"bold")).pack(anchor="w")
        current=db.jr_gold_settings(); merge_vars={age:tk.BooleanVar(value=current["merges"].get(age,False)) for age in ("U14","U16","U18")}
        draft_cuts={k:str(v) for k,v in current.get("cuts",{}).items()}
        merge_box=ttk.LabelFrame(outer,text="Division grouping",padding=10); merge_box.pack(fill="x",pady=(10,12))
        for age in ("U14","U16","U18"):
            ttk.Checkbutton(merge_box,text=f"Combine {age} Boys + Girls",variable=merge_vars[age]).pack(anchor="w",pady=4)
        cuts_box=ttk.LabelFrame(outer,text="Cut lines",padding=10); cuts_box.pack(fill="both",expand=True)
        cut_vars={}
        def remember():
            for group,var in list(cut_vars.items()): draft_cuts[group]=var.get()
        def rebuild(*_):
            remember()
            for child in cuts_box.winfo_children(): child.destroy()
            cut_vars.clear()
            preview={"merges":{a:v.get() for a,v in merge_vars.items()},"cuts":draft_cuts}
            for r,group in enumerate(db.jr_gold_group_names(preview)):
                ttk.Label(cuts_box,text=group,font=("Segoe UI",10,"bold")).grid(row=r,column=0,sticky="w",padx=(0,14),pady=6)
                var=tk.StringVar(value=draft_cuts.get(group,"0")); cut_vars[group]=var
                ttk.Spinbox(cuts_box,textvariable=var,from_=0,to=200,width=8).grid(row=r,column=1,sticky="w",pady=6)
        for v in merge_vars.values(): v.trace_add("write",rebuild)
        rebuild()
        def close_db():
            try: db.close()
            except Exception: pass
            win.destroy()
        def save():
            try:
                remember(); visible=db.jr_gold_group_names({"merges":{a:v.get() for a,v in merge_vars.items()},"cuts":draft_cuts})
                cuts={g:int(draft_cuts.get(g,"0") or 0) for g in visible}
                db.set_jr_gold_settings({"merges":{a:v.get() for a,v in merge_vars.items()},"cuts":cuts})
                messagebox.showinfo("Jr. Gold Settings","Jr. Gold settings saved.",parent=win); close_db()
            except Exception as exc: messagebox.showerror("Jr. Gold Settings",str(exc),parent=win)
        bottom=ttk.Frame(outer); bottom.pack(fill="x",pady=(12,0))
        ttk.Button(bottom,text="Cancel",command=close_db,style="Secondary.TButton").pack(side="right")
        ttk.Button(bottom,text="Save Settings",command=save,style="Primary.TButton").pack(side="right",padx=8)
        win.protocol("WM_DELETE_WINDOW",close_db)

    def push_public_qualifying(self):
        try:
            if not self._confirm_standings_completeness("publishing qualifying standings"):
                return
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
            if not self._confirm_standings_completeness("publishing Jr. Gold standings"):
                return
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

    def push_public_match_play(self):
        try:
            url,key=self._portal_credentials(); roster,manifest=self._portal_roster_ready()
        except Exception as exc:
            messagebox.showerror("Cannot publish match play",str(exc),parent=self); return
        self.status_var.set("Publishing match-play brackets…")
        def worker():
            try:
                result=portal_publish_match_play(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
                self.after(0,lambda:self._match_play_publish_done(result))
            except Exception as exc:
                self.after(0,lambda e=exc:self._job_failed("Match-play publish",e))
        threading.Thread(target=worker,daemon=True).start()

    def _match_play_publish_done(self,result):
        self.status_var.set("Match-play brackets published")
        messagebox.showinfo("Match Play Published",f"Published brackets for {result.get('divisions',0)} division(s).",parent=self)

    def clear_current_tournament_website(self):
        if not messagebox.askyesno("Clear Current Tournament?","Remove Qualifying, Jr. Gold Qualifying, and Match Play information from the Current Tournament section of the public website? Archived tournaments and Bowler of the Year standings will stay intact.",parent=self): return
        try: url,key=self._portal_credentials()
        except Exception as exc: messagebox.showerror("Cloud settings",str(exc),parent=self); return
        try:
            result=portal_clear_current_tournament(url,key)
            messagebox.showinfo("Current Tournament Cleared",f"Cleared current website data for {result.get('cleared_tournaments',0)} tournament record(s).",parent=self)
        except Exception as exc: messagebox.showerror("Clear Current Tournament",str(exc),parent=self)

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
                portal_publish_match_play(url,key,roster,manifest,self.event_name_var.get(),self.event_date_var.get().strip())
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
            workspace / "lane_scoring", workspace / "tournament_inputs",
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
            self.registration_source_var.set(""); self.transactions_source_var.set("")
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
            self.show_page("files")
        except Exception as exc:
            messagebox.showerror("Reset failed", f"The reset stopped before completion.\n\n{exc}", parent=self)

    def open_public_site(self):
        url=self.cloud_url_var.get().strip().rstrip("/")
        if not url: messagebox.showerror("Cloud URL missing","Enter the Render Cloud scoring URL first.",parent=self); return
        webbrowser.open(url)

    def open_admin_controls(self):
        try:
            url,_=self._cloud_credentials()
            webbrowser.open(url.rstrip("/")+"/admin")
        except Exception as exc:
            messagebox.showerror("Admin Controls",str(exc),parent=self)


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
                self.status_var.set("Saved active tournament found — Open Tournament Manager will resume it")
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
                [sys.executable, str(TOURNAMENT_SCRIPT), str(roster), "--db", str(db_path), "--resume", "--print-title", self.event_name_var.get().strip()],
                **kwargs,
            )
            self.status_var.set(f"Reloaded saved tournament from {workspace.name}")
            self.show_page("manager")
        except Exception as exc:
            messagebox.showerror("Could not reload tournament", str(exc), parent=self)

    def launch_tournament_manager(self):
        """Open a new tournament or automatically resume the saved active DB."""
        self._save_workspace_state()
        workspace, saved_roster, db_path, lane_manifest, lane_pdf = self._workspace_tournament_paths()
        if saved_roster.is_file() and db_path.is_file():
            # Existing scoring state wins. This makes the primary button safe to
            # use after accidentally closing Tournament Manager.
            self.reload_tournament_from_workspace()
            return

        roster = Path(self.tournament_roster_var.get()).expanduser()
        if not roster.is_file():
            messagebox.showerror(
                "Roster not found",
                "Run Setup Tournament first so tournament_divisions/all_divisions.csv exists.",
                parent=self,
            )
            return
        if not self._archive_inputs("tournament_manager", ("tournament_roster", roster)):
            return
        try:
            kwargs = {"cwd": str(BASE_DIR)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                [sys.executable, str(TOURNAMENT_SCRIPT), str(roster), "--print-title", self.event_name_var.get().strip()],
                **kwargs,
            )
            self.status_var.set(f"Tournament Manager opened with {roster.name}")
        except Exception as exc:
            messagebox.showerror("Could not open Tournament Manager", str(exc), parent=self)

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
