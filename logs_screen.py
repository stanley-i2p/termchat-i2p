from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


class LogsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    CSS = """
    LogsScreen {
        align: center middle;
    }

    #logs-modal {
        width: 124;
        height: 34;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #logs-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #logs-help {
        height: 2;
        color: $text-muted;
    }

    #logs-content {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, entries: list[str]) -> None:
        super().__init__()
        self.entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="logs-modal"):
            yield Static("Logs", id="logs-title")
            yield Static("Esc cancels", id="logs-help")
            yield RichLog(id="logs-content", markup=True, highlight=False, wrap=True)

    def on_mount(self) -> None:
        log = self.query_one("#logs-content", RichLog)
        for entry in self.entries:
            log.write(entry)
        log.focus()

    def action_close(self) -> None:
        self.dismiss(None)
