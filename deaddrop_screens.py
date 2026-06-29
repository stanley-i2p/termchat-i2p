from __future__ import annotations

from collections.abc import Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static


class DeadDropManagerScreen(ModalScreen[None]):
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
    DeadDropManagerScreen {
        align: center middle;
    }

    #dd-manager-modal {
        width: 124;
        height: 38;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #dd-manager-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #dd-manager-help {
        height: 2;
        color: $text-muted;
    }

    #dd-manager-table {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }

    #dd-manager-add {
        width: 64;
        background: $panel;
    }

    #dd-manager-table-gap {
        height: 2;
    }

    .dd-action-row {
        height: 3;
    }

    .dd-action-row Button {
        width: 18;
    }

    .dd-confirm {
        width: 8;
    }

    .dd-row-fill {
        width: 1fr;
    }

    .dd-button-gap {
        width: 1;
    }

    #dd-manager-delete {
        width: 18;
    }

    #dd-manager-status {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        get_rows: Callable[[], list[dict]],
        add_server: Callable[[str], None],
        delete_server: Callable[[int], None],
        get_server: Callable[[int], str | None],
        add_value: str = "",
        delete_index: int | None = None,
    ) -> None:
        super().__init__()
        self.get_rows = get_rows
        self.add_server = add_server
        self.delete_server = delete_server
        self.get_server = get_server
        self.add_value = add_value
        self.delete_index = delete_index
        self.pending_delete_index: int | None = None
        self.pending_delete_server: str | None = None
        self.pending_add_server: str | None = None
        self.mode = "idle"

    def compose(self) -> ComposeResult:
        with Vertical(id="dd-manager-modal"):
            yield Static("Deaddrop Manager", id="dd-manager-title")
            yield Static("Arrows select server. Tab switches fields. Y/N confirms. Esc closes.", id="dd-manager-help")
            yield DataTable(
                show_row_labels=False,
                zebra_stripes=True,
                cursor_type="row",
                id="dd-manager-table",
            )
            yield Static("", id="dd-manager-table-gap")
            with Horizontal(classes="dd-action-row"):
                yield Button("Delete Selected", id="dd-manager-delete", variant="error", compact=True)
                yield Static("", classes="dd-row-fill")
                yield Input(
                    placeholder="Y/N",
                    max_length=1,
                    id="dd-manager-delete-confirm",
                    classes="dd-confirm",
                    compact=True,
                    disabled=True,
                )
            with Horizontal(classes="dd-action-row"):
                yield Button("Add Server", id="dd-manager-add-button", variant="primary", compact=True)
                yield Static("", classes="dd-button-gap")
                yield Input(self.add_value, placeholder="b32 server address", id="dd-manager-add", compact=True)
                yield Static("", classes="dd-row-fill")
                yield Input(
                    placeholder="Y/N",
                    max_length=1,
                    id="dd-manager-add-confirm",
                    classes="dd-confirm",
                    compact=True,
                    disabled=True,
                )
            yield Static("", id="dd-manager-status")

    def on_mount(self) -> None:
        table = self.query_one("#dd-manager-table", DataTable)
        table.add_columns("#", "Active", "Server", "Put", "Get", "Latency")
        self.refresh_table()

        if self.delete_index is not None:
            if table.row_count:
                table.move_cursor(row=min(max(self.delete_index - 1, 0), table.row_count - 1), animate=False)
            self.start_delete(self.delete_index)
            self.query_one("#dd-manager-delete-confirm", Input).focus()
        elif self.add_value:
            self.query_one("#dd-manager-add", Input).focus()
        else:
            table.focus()

    def action_close(self) -> None:
        if self.mode == "confirm_delete":
            self.reset_pending_delete()
            self.set_status("Removal canceled.")
            return

        if self.mode == "confirm_add":
            self.reset_pending_add()
            self.set_status("Add canceled.")
            return

        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_cursor_up()

    def action_scroll_down(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_cursor_down()

    def action_page_up(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_page_up()

    def action_page_down(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_page_down()

    def action_scroll_home(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_scroll_home()

    def action_scroll_end(self) -> None:
        self.query_one("#dd-manager-table", DataTable).action_scroll_end()

    def set_status(self, text: str) -> None:
        self.query_one("#dd-manager-status", Static).update(text)

    def refresh_table(self) -> None:
        table = self.query_one("#dd-manager-table", DataTable)
        current_row = table.cursor_row
        table.clear()

        for row in self.get_rows():
            table.add_row(
                str(row["index"]),
                "*" if row["active"] else "",
                row["server"],
                f'{row["put_ok"]}/{row["put_fail"]}',
                f'{row["get_ok"]}/{row["get_fail"]}',
                f'{row["latency"]:.1f}ms',
                key=str(row["index"]),
            )

        if table.row_count:
            table.move_cursor(row=min(max(current_row, 0), table.row_count - 1), animate=False)

    def reset_pending_delete(self) -> None:
        self.pending_delete_index = None
        self.pending_delete_server = None
        self.mode = "idle"
        delete_button = self.query_one("#dd-manager-delete", Button)
        delete_button.label = "Delete Selected"
        confirm_input = self.query_one("#dd-manager-delete-confirm", Input)
        confirm_input.value = ""
        self.apply_mode()
        self.query_one("#dd-manager-table", DataTable).focus()
        self.set_status("")

    def reset_pending_add(self) -> None:
        self.pending_add_server = None
        self.mode = "idle"
        confirm_input = self.query_one("#dd-manager-add-confirm", Input)
        confirm_input.value = ""
        self.apply_mode()
        self.query_one("#dd-manager-add", Input).focus()

    def apply_mode(self) -> None:
        table = self.query_one("#dd-manager-table", DataTable)
        delete_button = self.query_one("#dd-manager-delete", Button)
        delete_confirm = self.query_one("#dd-manager-delete-confirm", Input)
        add_button = self.query_one("#dd-manager-add-button", Button)
        add_input = self.query_one("#dd-manager-add", Input)
        add_confirm = self.query_one("#dd-manager-add-confirm", Input)

        table.disabled = self.mode != "idle"
        delete_button.disabled = self.mode != "idle"
        add_button.disabled = self.mode != "idle"
        add_input.disabled = self.mode != "idle"
        delete_confirm.disabled = self.mode != "confirm_delete"
        add_confirm.disabled = self.mode != "confirm_add"

    def selected_index(self) -> int | None:
        table = self.query_one("#dd-manager-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row = table.get_row_at(table.cursor_row)
        except Exception:
            return None
        try:
            return int(str(row[0]))
        except (TypeError, ValueError):
            return None

    def start_delete(self, index: int) -> None:
        server = self.get_server(index)
        if server is None:
            self.reset_pending_delete()
            self.set_status("Invalid server number.")
            return

        self.pending_delete_index = index
        self.pending_delete_server = server
        self.mode = "confirm_delete"
        delete_button = self.query_one("#dd-manager-delete", Button)
        delete_button.label = "Delete Pending"
        confirm_input = self.query_one("#dd-manager-delete-confirm", Input)
        confirm_input.value = ""
        self.apply_mode()
        self.set_status(f"Delete selected server? Enter Y or N: {server}")
        confirm_input.focus()

    def confirm_pending_delete(self) -> None:
        if self.pending_delete_index is None:
            return

        server = self.get_server(self.pending_delete_index)
        if server is None or server != self.pending_delete_server:
            self.reset_pending_delete()
            self.refresh_table()
            self.set_status("Server list changed. Removal canceled.")
            return

        self.delete_server(self.pending_delete_index)
        self.reset_pending_delete()
        self.refresh_table()
        self.set_status("Removal submitted.")

    def start_add(self) -> None:
        server = self.query_one("#dd-manager-add", Input).value.strip()
        if not server:
            self.set_status("Enter a deaddrop server address first.")
            return

        self.pending_add_server = server
        self.mode = "confirm_add"
        confirm_input = self.query_one("#dd-manager-add-confirm", Input)
        confirm_input.value = ""
        self.apply_mode()
        self.set_status(f"Add server? Enter Y or N: {server}")
        confirm_input.focus()

    def confirm_pending_add(self) -> None:
        if not self.pending_add_server:
            return

        add_input = self.query_one("#dd-manager-add", Input)
        if add_input.value.strip() != self.pending_add_server:
            self.reset_pending_add()
            self.set_status("Address changed. Add canceled.")
            return

        self.add_server(self.pending_add_server)
        add_input.value = ""
        self.reset_pending_add()
        self.reset_pending_delete()
        self.refresh_table()
        self.set_status("Add submitted.")

    @on(DataTable.RowSelected, "#dd-manager-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.mode != "idle":
            return

        try:
            index = int(str(event.row_key.value))
        except (AttributeError, TypeError, ValueError):
            index = self.selected_index()

        if index is None:
            self.set_status("No deaddrop server selected.")
            return

        self.reset_pending_delete()
        self.reset_pending_add()
        self.set_status(f"Selected: {self.get_server(index)}")

    @on(Input.Submitted, "#dd-manager-add")
    def on_add_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.mode != "idle":
            return
        self.start_add()

    @on(Button.Pressed, "#dd-manager-add-button")
    def on_add_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.mode != "idle":
            return
        self.start_add()

    @on(Input.Submitted, "#dd-manager-add-confirm")
    def on_add_confirm_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.handle_add_confirmation(event.value, event.input)

    @on(Input.Changed, "#dd-manager-add-confirm")
    def on_add_confirm_changed(self, event: Input.Changed) -> None:
        self.handle_add_confirmation(event.value, event.input)

    def handle_add_confirmation(self, value: str, input_widget: Input) -> None:
        if self.mode != "confirm_add":
            return
        value = value.strip()
        if value == "Y":
            self.confirm_pending_add()
            return

        if value == "N":
            self.reset_pending_add()
            self.set_status("Add canceled.")
            return

        if value:
            input_widget.value = ""
            self.set_status("Use uppercase Y or N.")

    @on(Button.Pressed, "#dd-manager-delete")
    def on_delete_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.mode != "idle":
            return

        index = self.selected_index()
        if index is None:
            self.set_status("No deaddrop server selected.")
            return

        self.start_delete(index)

    @on(Input.Submitted, "#dd-manager-delete-confirm")
    def on_delete_confirm_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.handle_delete_confirmation(event.value, event.input)

    @on(Input.Changed, "#dd-manager-delete-confirm")
    def on_delete_confirm_changed(self, event: Input.Changed) -> None:
        self.handle_delete_confirmation(event.value, event.input)

    def handle_delete_confirmation(self, value: str, input_widget: Input) -> None:
        if self.mode != "confirm_delete":
            return
        value = value.strip()
        if value == "Y":
            self.confirm_pending_delete()
            return

        if value == "N":
            self.reset_pending_delete()
            self.set_status("Removal canceled.")
            return

        if value:
            input_widget.value = ""
            self.set_status("Use uppercase Y or N.")
