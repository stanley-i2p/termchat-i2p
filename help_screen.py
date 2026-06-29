from __future__ import annotations

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


HELP_LINES = [
    ("help_bold", "Command line options:"),
    ("help", ""),
    ("help", "  Start with --pq [<profile>] to enable HybridPQ encryption. Both peers must use this flag to establish a connection."),
    ("help", "  Start with --reset <profile> to recreate a persistent profile from scratch"),
    ("help", "  Start with --delete <profile> to delete a profile completely"),
    ("help", "  Start with --wipe-all to remove all app storage completely"),
    ("help", ""),
    ("help_bold", "Available commands:"),
    ("help", ""),
    ("help_bold", "Connection:"),
    ("help", ""),
    ("help", "  /connect <b32-address>   Connect to peer"),
    ("help", "  /disconnect              Close connection"),
    ("help", "  /accept                  Accept incoming call"),
    ("help", "  /decline                 Decline incoming call"),
    ("help", ""),
    ("help_bold", "Messaging:"),
    ("help", ""),
    ("help", "  Type text and press ENTER to send message"),
    ("help", "  /offline                 Enter offline messaging mode (PERSISTENT locked peer only)"),
    ("help", "  /online                  Exit offline messaging mode (PERSISTENT locked peer only)"),
    ("help", ""),
    ("help_bold", "Identity:"),
    ("help", ""),
    ("help", "  /lock                    Lock persistent profile to current peer (not available in TRANSIENT mode)"),
    ("help", ""),
    ("help_bold", "Files:"),
    ("help", ""),
    ("help", "  /sendfile [path]         Send file (opens picker if path is omitted)"),
    ("help", ""),
    ("help_bold", "Images:"),
    ("help", ""),
    ("help", "  /img [path]              Send image (opens picker if path is omitted)"),
    ("help", "  /img-bw [path]           Send image with block renderer (opens picker if path is omitted)"),
    ("help", ""),
    ("help_bold", "Deaddrops:"),
    ("help", ""),
    ("help", "  /dd                      Open deaddrop manager"),
    ("help", "  /dd-share                Share deaddrop server list with peer"),
    ("help", ""),
    ("help_bold", "Utility:"),
    ("help", ""),
    ("help", "  c                        Copy local b32 address to your clipboard"),
    ("help", "  /logs                    Show system logs"),
    ("help", "  /help                    Show this help"),
    ("help", "  /CTRL+q                  Exit program"),
]


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-modal {
        width: 124;
        height: 34;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #help-title {
        height: 1;
        color: $text;
        text-style: bold;
    }
    
    #help-help {
        height: 2;
        color: $text-muted;
    }

    #help-content {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, lines: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self.lines = lines or HELP_LINES

    def compose(self) -> ComposeResult:
        with Vertical(id="help-modal"):
            yield Static("Help", id="help-title")
            yield Static("Esc cancels", id="help-help")
            yield RichLog(id="help-content", markup=False, highlight=False, wrap=True)

    def on_mount(self) -> None:
        log = self.query_one("#help-content", RichLog)
        for kind, text in self.lines:
            if kind == "help_bold":
                log.write(Text(text, style="bold"))
            else:
                log.write(text)
        log.focus()

    def action_close(self) -> None:
        self.dismiss(None)
