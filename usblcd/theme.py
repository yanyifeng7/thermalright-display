"""Dark theme for the AIO LCD player GUI.

A cohesive modern dark palette (no more win95-era gray). All colors
live here so the look can be tuned in one place. ttk styles are
registered under a "Dark." prefix; raw tk widgets get explicit colors
in the app (they ignore ttk styles).
"""

# ---------- Palette ----------
BG = "#141419"            # window background (near-black navy)
PANEL = "#1c1c24"         # frame / panel background
PANEL_ALT = "#24242e"     # hover / inner panel
BORDER = "#32323e"        # borders, separators
INPUT = "#101014"         # combobox / entry field background
TEXT = "#e8e8ec"          # primary text
TEXT_DIM = "#9a9aa5"      # secondary text
ACCENT = "#4f8cff"        # primary accent (blue)
ACCENT_DARK = "#3a6fd8"   # accent pressed/hover
ACCENT_TEXT = "#ffffff"   # text on accent
OK = "#2ecc71"            # success green
WARN = "#f1c40f"          # warning amber
ERR = "#e74c3c"           # error red
INFO = "#4fa3e8"          # info blue
PREVIEW_BG = "#000000"    # preview canvas (letterbox area)

FONT = "Segoe UI"
FONT_BOLD = "Segoe UI"


def setup_style(style: "ttk.Style") -> None:
    """Register all Dark.* ttk styles.

    Uses the 'clam' theme: the Windows 'vista' theme ignores custom
    background colors on ttk buttons/comboboxes (they stay light gray),
    which made light text unreadable. 'clam' honors every color and
    still looks modern on Windows.
    """
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # ---------- Frames ----------
    style.configure("Dark.TFrame", background=BG)
    style.configure("Dark.TLabelframe", background=PANEL, bordercolor=BORDER,
                    relief="flat", borderwidth=1, padding=6)
    style.configure("Dark.TLabelframe.Label", background=PANEL,
                    foreground=TEXT, font=(FONT, 9, "bold"))

    # ---------- Labels ----------
    style.configure("Dark.TLabel", background=BG, foreground=TEXT,
                    font=(FONT, 9))
    style.configure("Dark.Dim.TLabel", background=BG, foreground=TEXT_DIM,
                    font=(FONT, 9))
    style.configure("Dark.H2.TLabel", background=BG, foreground=TEXT,
                    font=(FONT, 13, "bold"))

    # ---------- Buttons ----------
    style.configure("Dark.TButton", background=PANEL_ALT, foreground=TEXT,
                    bordercolor=BORDER, focuscolor=PANEL_ALT, padding=(10, 5),
                    font=(FONT, 9), borderwidth=1, relief="flat")
    style.map("Dark.TButton",
              background=[("pressed", ACCENT_DARK), ("active", "#33333f")],
              foreground=[("disabled", TEXT_DIM)],
              bordercolor=[("focus", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT,
                    bordercolor=ACCENT, focuscolor=ACCENT, padding=(14, 6),
                    font=(FONT, 10, "bold"))
    style.map("Accent.TButton",
              background=[("pressed", ACCENT_DARK), ("active", ACCENT_DARK),
                          ("disabled", "#3a3a46")],
              foreground=[("disabled", "#8a8a95")])

    # ---------- Combobox ----------
    style.configure("Dark.TCombobox", fieldbackground=INPUT,
                    background=PANEL_ALT, foreground=TEXT, arrowcolor=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=4, font=(FONT, 9))
    style.map("Dark.TCombobox",
              fieldbackground=[("readonly", INPUT)],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", INPUT)],
              selectforeground=[("readonly", TEXT)])
    style.map("Dark.TCombobox", bordercolor=[("focus", ACCENT)])

    # ---------- Checkbutton ----------
    style.configure("Dark.TCheckbutton", background=BG, foreground=TEXT,
                    focuscolor=BG, font=(FONT, 9))
    style.map("Dark.TCheckbutton",
              background=[("active", BG)],
              foreground=[("disabled", TEXT_DIM)])

    # ---------- Progressbar ----------
    style.configure("Dark.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=INPUT, bordercolor=BORDER, lightcolor=ACCENT,
                    darkcolor=ACCENT)

    # ---------- Listbox (dropdown popup of combobox) ----------
    style.map("Dark.TCombobox", fieldbackground=[("readonly", INPUT)])
