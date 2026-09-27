"""Shared desktop color themes for the Tough Shots applications."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

THEMES = {
    "Light": {
        "app_bg": "#f4f7fb", "sidebar": "#13233a", "panel": "#ffffff",
        "text": "#152238", "text2": "#475569", "muted": "#718096",
        "sidebar_text": "#cbd5e1", "sidebar_muted": "#aebed2",
        "button": "#d7dbe0", "button_hover": "#c5cbd2", "button_text": "#1f2937",
        "selected": "#64748b", "accent": "#4f6f9f", "accent_text": "#ffffff",
        "input_bg": "#ffffff", "input_text": "#152238", "border": "#cbd5e1",
        "status_bg": "#eaf0f8", "status_text": "#334155", "disabled": "#e5e7eb",
        "disabled_text": "#9ca3af", "selection": "#cddbf0",
    },
    "Slate Dark": {
        "app_bg": "#1E222A", "sidebar": "#171A20", "panel": "#292E38",
        "text": "#F2F4F7", "text2": "#D6DAE1", "muted": "#B8BEC8",
        "sidebar_text": "#E3E7ED", "sidebar_muted": "#9FA8B5",
        "button": "#3A404C", "button_hover": "#4A5260", "button_text": "#F2F4F7",
        "selected": "#506784", "accent": "#6EA8FE", "accent_text": "#08111F",
        "input_bg": "#20242B", "input_text": "#F2F4F7", "border": "#555D6A",
        "status_bg": "#242A33", "status_text": "#D8DEE8", "disabled": "#303641",
        "disabled_text": "#808895", "selection": "#3B5475",
    },
    "Charcoal + Red": {
        "app_bg": "#181818", "sidebar": "#101010", "panel": "#242424",
        "text": "#F5F5F5", "text2": "#D7D7D7", "muted": "#B5B5B5",
        "sidebar_text": "#ECECEC", "sidebar_muted": "#A7A7A7",
        "button": "#353535", "button_hover": "#454545", "button_text": "#F5F5F5",
        "selected": "#8F3838", "accent": "#C94C4C", "accent_text": "#FFFFFF",
        "input_bg": "#1D1D1D", "input_text": "#F5F5F5", "border": "#555555",
        "status_bg": "#202020", "status_text": "#DDDDDD", "disabled": "#2D2D2D",
        "disabled_text": "#777777", "selection": "#713636",
    },
    "Midnight Blue": {
        "app_bg": "#111827", "sidebar": "#0B1220", "panel": "#1F2937",
        "text": "#F9FAFB", "text2": "#E5E7EB", "muted": "#AAB4C3",
        "sidebar_text": "#E5E7EB", "sidebar_muted": "#9CA3AF",
        "button": "#374151", "button_hover": "#4B5563", "button_text": "#F9FAFB",
        "selected": "#1D4ED8", "accent": "#60A5FA", "accent_text": "#08111F",
        "input_bg": "#172033", "input_text": "#F9FAFB", "border": "#4B5563",
        "status_bg": "#172033", "status_text": "#DCE6F3", "disabled": "#273244",
        "disabled_text": "#758196", "selection": "#284D7D",
    },
}

DEFAULT_THEME = "Light"
CONFIG_PATH = Path.home() / ".toughshots_ui.json"


def load_theme_name() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        name = str(data.get("theme", DEFAULT_THEME))
        return name if name in THEMES else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def save_theme_name(name: str) -> None:
    if name not in THEMES:
        return
    try:
        CONFIG_PATH.write_text(json.dumps({"theme": name}, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_theme(root: tk.Misc, name: str, *, suite_styles: bool = False) -> dict:
    """Apply a theme to ttk defaults and Tough Shots custom styles."""
    p = THEMES.get(name, THEMES[DEFAULT_THEME])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    try:
        root.configure(bg=p["app_bg"])
    except tk.TclError:
        pass

    # General ttk controls. These also cover Tournament Manager windows.
    style.configure("TFrame", background=p["app_bg"])
    style.configure("TLabel", background=p["app_bg"], foreground=p["text"])
    style.configure("TLabelframe", background=p["app_bg"], foreground=p["text"], bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["app_bg"], foreground=p["text"], font=("Segoe UI", 10, "bold"))
    style.configure("TButton", background=p["button"], foreground=p["button_text"], padding=(10, 7), font=("Segoe UI", 10, "bold"), bordercolor=p["border"])
    style.map("TButton", background=[("active", p["button_hover"]), ("disabled", p["disabled"])], foreground=[("disabled", p["disabled_text"])])
    style.configure("TEntry", fieldbackground=p["input_bg"], foreground=p["input_text"], insertcolor=p["input_text"], bordercolor=p["border"])
    style.map("TEntry", fieldbackground=[("readonly", p["input_bg"]), ("disabled", p["disabled"])], foreground=[("readonly", p["input_text"]), ("disabled", p["disabled_text"])])
    style.configure("TCombobox", fieldbackground=p["input_bg"], background=p["button"], foreground=p["input_text"], arrowcolor=p["text"], bordercolor=p["border"])
    style.map("TCombobox", fieldbackground=[("readonly", p["input_bg"])], foreground=[("readonly", p["input_text"])], selectbackground=[("readonly", p["input_bg"])], selectforeground=[("readonly", p["input_text"])])
    style.configure("TSpinbox", fieldbackground=p["input_bg"], background=p["button"], foreground=p["input_text"], arrowcolor=p["text"], bordercolor=p["border"])
    style.configure("TCheckbutton", background=p["panel"] if suite_styles else p["app_bg"], foreground=p["text"])
    style.map("TCheckbutton", background=[("active", p["panel"] if suite_styles else p["app_bg"])], foreground=[("disabled", p["disabled_text"])])
    style.configure("TRadiobutton", background=p["app_bg"], foreground=p["text"])
    style.configure("TNotebook", background=p["app_bg"], bordercolor=p["border"])
    style.configure("TNotebook.Tab", background=p["button"], foreground=p["button_text"], padding=(12, 7))
    style.map("TNotebook.Tab", background=[("selected", p["selected"]), ("active", p["button_hover"])], foreground=[("selected", "#ffffff")])
    style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"], foreground=p["text"], bordercolor=p["border"], rowheight=26)
    style.map("Treeview", background=[("selected", p["selected"])], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=p["button"], foreground=p["button_text"], font=("Segoe UI", 9, "bold"), bordercolor=p["border"])
    style.map("Treeview.Heading", background=[("active", p["button_hover"])])
    style.configure("TSeparator", background=p["border"])

    if suite_styles:
        # Most unstyled frames/labels in the suite live inside cards or dialogs.
        style.configure("TFrame", background=p["panel"])
        style.configure("TLabel", background=p["panel"], foreground=p["text"])
        style.configure("TLabelframe", background=p["panel"], foreground=p["text"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["panel"], foreground=p["text"], font=("Segoe UI", 10, "bold"))
        style.configure("App.TFrame", background=p["app_bg"])
        style.configure("Sidebar.TFrame", background=p["sidebar"])
        style.configure("Card.TFrame", background=p["panel"], relief="flat")
        style.configure("Brand.TLabel", background=p["sidebar"], foreground="#ffffff", font=("Segoe UI", 17, "bold"))
        style.configure("BrandSub.TLabel", background=p["sidebar"], foreground=p["sidebar_muted"], font=("Segoe UI", 9))
        style.configure("PageTitle.TLabel", background=p["app_bg"], foreground=p["text"], font=("Segoe UI", 24, "bold"))
        style.configure("PageSubtitle.TLabel", background=p["app_bg"], foreground=p["muted"], font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=p["panel"], foreground=p["text"], font=("Segoe UI", 13, "bold"))
        style.configure("FieldLabel.TLabel", background=p["panel"], foreground=p["text2"], font=("Segoe UI", 10, "bold"))
        style.configure("Hint.TLabel", background=p["panel"], foreground=p["muted"], font=("Segoe UI", 9))
        style.configure("CardText.TLabel", background=p["panel"], foreground=p["text2"], font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=p["status_bg"], foreground=p["status_text"], font=("Segoe UI", 9), padding=(10, 7))
        for button_style in ("Primary.TButton", "Secondary.TButton"):
            style.configure(button_style, font=("Segoe UI", 10, "bold"), padding=(14, 9), background=p["button"], foreground=p["button_text"], bordercolor=p["border"])
            style.map(button_style, background=[("active", p["button_hover"]), ("disabled", p["disabled"])], foreground=[("disabled", p["disabled_text"])])
        style.configure("Nav.TButton", anchor="w", font=("Segoe UI", 10, "bold"), padding=(16, 12), background=p["sidebar"], foreground=p["sidebar_text"], borderwidth=0)
        style.map("Nav.TButton", background=[("active", p["button_hover"])], foreground=[("active", "#ffffff")])
        style.configure("NavSelected.TButton", anchor="w", font=("Segoe UI", 10, "bold"), padding=(16, 11), background=p["selected"], foreground="#ffffff", borderwidth=0)
        style.configure("Card.TCheckbutton", background=p["panel"], foreground=p["text"])
        style.map("Card.TCheckbutton", background=[("active", p["panel"])])

    # Option database helps classic Tk controls created after this call.
    root.option_add("*Listbox.background", p["panel"])
    root.option_add("*Listbox.foreground", p["text"])
    root.option_add("*Listbox.selectBackground", p["selected"])
    root.option_add("*Listbox.selectForeground", "#ffffff")
    root.option_add("*Text.background", p["input_bg"])
    root.option_add("*Text.foreground", p["input_text"])
    root.option_add("*Text.insertBackground", p["input_text"])

    _recolor_classic_widgets(root, p)
    return p


def _recolor_classic_widgets(widget: tk.Misc, p: dict) -> None:
    """Update classic Tk widgets that already exist when a theme is changed."""
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        try:
            if isinstance(child, (tk.Toplevel, tk.Frame)):
                child.configure(bg=p["app_bg"])
            elif isinstance(child, tk.Listbox):
                child.configure(bg=p["panel"], fg=p["text"], selectbackground=p["selected"], selectforeground="#ffffff", highlightbackground=p["border"], highlightcolor=p["accent"])
            elif isinstance(child, tk.Text):
                child.configure(bg=p["input_bg"], fg=p["input_text"], insertbackground=p["input_text"], selectbackground=p["selected"], selectforeground="#ffffff", highlightbackground=p["border"], highlightcolor=p["accent"])
            elif isinstance(child, tk.Entry):
                child.configure(bg=p["input_bg"], fg=p["input_text"], insertbackground=p["input_text"], selectbackground=p["selected"], selectforeground="#ffffff")
            elif isinstance(child, tk.Label):
                child.configure(bg=p["app_bg"], fg=p["text"])
            elif isinstance(child, tk.Button):
                child.configure(bg=p["button"], fg=p["button_text"], activebackground=p["button_hover"], activeforeground=p["button_text"])
        except (tk.TclError, AttributeError):
            pass
        _recolor_classic_widgets(child, p)
