from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static


class ProfilesScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("up", "scroll_up", "Scroll up", show=False, priority=True),
        Binding("down", "scroll_down", "Scroll down", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    CSS = """
    ProfilesScreen {
        align: center middle;
    }

    #profiles-modal {
        width: 124;
        height: 34;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #profiles-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #profiles-help {
        height: 2;
        color: $text-muted;
    }

    #profiles-table {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }

    #profiles-status {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="profiles-modal"):
            yield Static("1:1 Accounts", id="profiles-title")
            yield Static("Read-only list. Esc closes.", id="profiles-help")
            yield DataTable(
                show_row_labels=False,
                zebra_stripes=True,
                cursor_type="row",
                id="profiles-table",
            )
            yield Static("", id="profiles-status")

    def on_mount(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.add_columns("#", "Profile", "State", "Locked Peer B32")
        for row in self.rows:
            table.add_row(
                str(row["index"]),
                row["profile"],
                row["state"],
                row["peer_b32"],
            )

        status = self.query_one("#profiles-status", Static)
        if table.row_count:
            status.update(f"{table.row_count} persistent profile(s)")
            table.focus()
        else:
            status.update("No persistent profiles found.")

    def action_scroll_up(self) -> None:
        self.query_one("#profiles-table", DataTable).action_cursor_up()

    def action_scroll_down(self) -> None:
        self.query_one("#profiles-table", DataTable).action_cursor_down()

    def action_page_up(self) -> None:
        self.query_one("#profiles-table", DataTable).action_page_up()

    def action_page_down(self) -> None:
        self.query_one("#profiles-table", DataTable).action_page_down()

    def action_scroll_home(self) -> None:
        self.query_one("#profiles-table", DataTable).action_scroll_home()

    def action_scroll_end(self) -> None:
        self.query_one("#profiles-table", DataTable).action_scroll_end()

    def action_close(self) -> None:
        self.dismiss(None)
