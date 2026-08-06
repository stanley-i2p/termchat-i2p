from __future__ import annotations

from collections.abc import Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static


class GroupManagerScreen(ModalScreen[None]):
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
    GroupManagerScreen {
        align: center middle;
    }

    #group-manager-modal {
        width: 124;
        height: 40;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #group-manager-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #group-manager-help {
        height: 2;
        color: $text-muted;
    }

    #group-manager-table {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }

    #group-manager-table-gap {
        height: 1;
    }

    .group-action-row {
        height: 3;
    }

    .group-action-row Button {
        width: 18;
    }

    .group-button-gap {
        width: 1;
    }

    .group-row-fill {
        width: 1fr;
    }

    #group-create-name {
        width: 30;
        background: $panel;
    }

    #group-create-display {
        width: 24;
        background: $panel;
    }

    #group-join-invite {
        width: 1fr;
        background: $panel;
    }

    #group-join-display {
        width: 24;
        background: $panel;
    }

    #group-delete-confirm {
        width: 8;
        background: $panel;
    }

    #group-manager-status {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        get_rows: Callable[[], list[dict]],
        open_group: Callable[[str], None],
        create_group: Callable[[str, str], None],
        join_group: Callable[[str, str], None],
        issue_invite: Callable[[str | None], str | None],
        delete_group: Callable[[str], None],
        rename_me: Callable[[str], None] | None = None,
        active_group_key: str | None = None,
        active_group_open: bool = False,
        active_group_owner: bool = False,
        active_display_name: str = "",
    ) -> None:
        super().__init__()
        self.get_rows = get_rows
        self.open_group = open_group
        self.create_group = create_group
        self.join_group = join_group
        self.issue_invite = issue_invite
        self.delete_group = delete_group
        self.rename_me = rename_me
        self.active_group_key = active_group_key
        self.active_group_open = active_group_open
        self.active_group_owner = active_group_owner
        self.active_display_name = active_display_name
        self.row_keys: list[str] = []
        self.row_states: list[str] = []
        self.mode = "idle"
        self.pending_delete_key: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="group-manager-modal"):
            yield Static("Group Manager", id="group-manager-title")
            yield Static("Arrows select group. Tab switches fields. Y/N confirms delete. Esc closes.", id="group-manager-help")
            yield DataTable(
                show_row_labels=False,
                zebra_stripes=True,
                cursor_type="row",
                id="group-manager-table",
            )
            yield Static("", id="group-manager-table-gap")
            with Horizontal(classes="group-action-row"):
                yield Button("Open Selected", id="group-open-button", variant="primary", compact=True)
                yield Static("", classes="group-button-gap")
                yield Button("Invite", id="group-invite-button", variant="success", compact=True)
                yield Static("", classes="group-button-gap")
                yield Button("Delete", id="group-delete-button", variant="error", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(placeholder="Y/N", max_length=1, id="group-delete-confirm", compact=True, disabled=True)
                yield Static("", classes="group-row-fill")
            with Horizontal(classes="group-action-row"):
                yield Button("Create Group", id="group-create-button", variant="primary", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(placeholder="group name", id="group-create-name", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(placeholder="your display name", id="group-create-display", compact=True)
            with Horizontal(classes="group-action-row"):
                yield Button("Join Invite", id="group-join-button", variant="primary", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(placeholder="COMMTOOLS-I2P-GROUP-INVITE-v1:...", id="group-join-invite", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(placeholder="your display name", id="group-join-display", compact=True)
            with Horizontal(classes="group-action-row"):
                yield Button("Rename Me", id="group-rename-button", variant="primary", compact=True)
                yield Static("", classes="group-button-gap")
                yield Input(self.active_display_name, placeholder="your display name", id="group-rename-display", compact=True)
                yield Static("", classes="group-row-fill")
            yield Static("", id="group-manager-status")

    def on_mount(self) -> None:
        table = self.query_one("#group-manager-table", DataTable)
        table.add_columns("#", "State", "Group", "Members", "Owner", "Key")
        self.refresh_table()
        if table.row_count:
            table.focus()
        else:
            self.query_one("#group-create-name", Input).focus()
        self.apply_mode()
        if self.active_group_open:
            self.set_status("Group is open. Only active-group rename and owner invite are enabled.")

    def action_close(self) -> None:
        if self.mode == "confirm_delete":
            self.reset_delete()
            self.set_status("Delete canceled.")
            return
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_cursor_up()
        self.apply_mode()

    def action_scroll_down(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_cursor_down()
        self.apply_mode()

    def action_page_up(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_page_up()
        self.apply_mode()

    def action_page_down(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_page_down()
        self.apply_mode()

    def action_scroll_home(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_scroll_home()
        self.apply_mode()

    def action_scroll_end(self) -> None:
        self.query_one("#group-manager-table", DataTable).action_scroll_end()
        self.apply_mode()

    def set_status(self, text: str) -> None:
        self.query_one("#group-manager-status", Static).update(text)

    def apply_mode(self) -> None:
        table = self.query_one("#group-manager-table", DataTable)
        open_button = self.query_one("#group-open-button", Button)
        invite_button = self.query_one("#group-invite-button", Button)
        delete_button = self.query_one("#group-delete-button", Button)
        delete_confirm = self.query_one("#group-delete-confirm", Input)
        create_button = self.query_one("#group-create-button", Button)
        join_button = self.query_one("#group-join-button", Button)
        create_name = self.query_one("#group-create-name", Input)
        create_display = self.query_one("#group-create-display", Input)
        join_invite = self.query_one("#group-join-invite", Input)
        join_display = self.query_one("#group-join-display", Input)
        rename_button = self.query_one("#group-rename-button", Button)
        rename_display = self.query_one("#group-rename-display", Input)

        locked = self.mode != "idle"
        admin_locked = locked or self.active_group_open
        selected_locked = self.selected_state() == "LOCKED"
        table.disabled = locked
        open_button.disabled = admin_locked or selected_locked
        invite_button.disabled = locked or selected_locked or (self.active_group_open and not self.active_group_owner)
        delete_button.disabled = admin_locked or selected_locked
        create_button.disabled = admin_locked
        join_button.disabled = admin_locked
        create_name.disabled = admin_locked
        create_display.disabled = admin_locked
        join_invite.disabled = admin_locked
        join_display.disabled = admin_locked
        rename_button.disabled = locked or not self.active_group_open
        rename_display.disabled = locked or not self.active_group_open
        delete_confirm.disabled = self.mode != "confirm_delete"

    def refresh_table(self) -> None:
        table = self.query_one("#group-manager-table", DataTable)
        current_row = table.cursor_row
        table.clear()
        self.row_keys = []
        self.row_states = []

        for row in self.get_rows():
            key = row["key"]
            state = row["state"]
            display_key = key[:18] + "..." if len(key) > 21 else key
            self.row_keys.append(key)
            self.row_states.append(state)
            table.add_row(
                str(row["index"]),
                state,
                row["name"],
                str(row["members"]),
                row["owner"],
                display_key,
                key=key,
            )

        if table.row_count:
            table.move_cursor(row=min(max(current_row, 0), table.row_count - 1), animate=False)

    def selected_key(self) -> str | None:
        table = self.query_one("#group-manager-table", DataTable)
        if table.row_count == 0:
            return None
        if 0 <= table.cursor_row < len(self.row_keys):
            return self.row_keys[table.cursor_row]
        return None

    def selected_state(self) -> str | None:
        table = self.query_one("#group-manager-table", DataTable)
        if table.row_count == 0:
            return None
        if 0 <= table.cursor_row < len(self.row_states):
            return self.row_states[table.cursor_row]
        return None

    def submit_open(self) -> None:
        key = self.selected_key()
        if not key:
            self.set_status("No group selected.")
            return
        if self.active_group_open:
            self.set_status("Close the current group before opening another.")
            return
        if self.selected_state() == "LOCKED":
            self.set_status("Group is already open in another instance.")
            return
        self.open_group(key)
        self.dismiss(None)

    def submit_invite(self) -> None:
        key = self.selected_key()
        if self.active_group_open:
            key = self.active_group_key
        elif self.selected_state() == "LOCKED":
            self.set_status("Cannot issue invite while group is open elsewhere.")
            return
        invite = self.issue_invite(key)
        if invite:
            self.set_status("Invite copied to clipboard and written to output.")
            self.refresh_table()
        else:
            self.set_status("Open or select an owner group first.")

    def start_delete(self) -> None:
        key = self.selected_key()
        if not key:
            self.set_status("No group selected.")
            return
        if self.active_group_open:
            self.set_status("Close the current group before deleting groups.")
            return
        if self.selected_state() == "LOCKED":
            self.set_status("Cannot delete group while it is open elsewhere.")
            return
        self.pending_delete_key = key
        self.mode = "confirm_delete"
        confirm = self.query_one("#group-delete-confirm", Input)
        confirm.value = ""
        self.apply_mode()
        self.set_status(f"Delete selected group? Enter Y or N: {key}")
        confirm.focus()

    def reset_delete(self) -> None:
        self.pending_delete_key = None
        self.mode = "idle"
        confirm = self.query_one("#group-delete-confirm", Input)
        confirm.value = ""
        self.apply_mode()
        self.query_one("#group-manager-table", DataTable).focus()

    def confirm_delete(self) -> None:
        if not self.pending_delete_key:
            return
        key = self.pending_delete_key
        self.delete_group(key)
        self.reset_delete()
        self.refresh_table()
        self.set_status("Delete submitted.")

    def submit_create(self) -> None:
        if self.active_group_open:
            self.set_status("Close the current group before creating groups.")
            return
        name = self.query_one("#group-create-name", Input).value.strip()
        display = self.query_one("#group-create-display", Input).value.strip()
        if not name:
            self.set_status("Enter a group name first.")
            self.query_one("#group-create-name", Input).focus()
            return
        self.create_group(name, display)
        self.dismiss(None)

    def submit_join(self) -> None:
        if self.active_group_open:
            self.set_status("Close the current group before joining groups.")
            return
        invite = self.query_one("#group-join-invite", Input).value.strip()
        display = self.query_one("#group-join-display", Input).value.strip()
        if not invite:
            self.set_status("Paste a group invite first.")
            self.query_one("#group-join-invite", Input).focus()
            return
        if not display:
            self.set_status("Enter your display name first.")
            self.query_one("#group-join-display", Input).focus()
            return
        self.join_group(invite, display)
        self.dismiss(None)

    def submit_rename(self) -> None:
        if not self.active_group_open or not self.rename_me:
            self.set_status("Open a group before renaming.")
            return
        display = self.query_one("#group-rename-display", Input).value.strip()
        if not display:
            self.set_status("Enter your display name first.")
            self.query_one("#group-rename-display", Input).focus()
            return
        self.rename_me(display)
        self.set_status("Rename submitted.")

    @on(DataTable.RowSelected, "#group-manager-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.apply_mode()
        self.set_status(f"Selected group: {event.row_key.value}")

    @on(DataTable.CellHighlighted, "#group-manager-table")
    def on_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        event.stop()
        self.apply_mode()

    @on(DataTable.RowHighlighted, "#group-manager-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self.apply_mode()

    @on(Button.Pressed, "#group-open-button")
    def on_open_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_open()

    @on(Button.Pressed, "#group-invite-button")
    def on_invite_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_invite()

    @on(Button.Pressed, "#group-create-button")
    def on_create_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_create()

    @on(Button.Pressed, "#group-join-button")
    def on_join_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_join()

    @on(Button.Pressed, "#group-rename-button")
    def on_rename_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_rename()

    @on(Button.Pressed, "#group-delete-button")
    def on_delete_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.mode != "idle":
            return
        self.start_delete()

    @on(Input.Submitted, "#group-delete-confirm")
    def on_delete_confirm_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.handle_delete_confirmation(event.value, event.input)

    @on(Input.Changed, "#group-delete-confirm")
    def on_delete_confirm_changed(self, event: Input.Changed) -> None:
        self.handle_delete_confirmation(event.value, event.input)

    def handle_delete_confirmation(self, value: str, input_widget: Input) -> None:
        if self.mode != "confirm_delete":
            return
        value = value.strip()
        if not value:
            return
        if value == "Y":
            self.confirm_delete()
            return
        if value == "N":
            self.reset_delete()
            self.set_status("Delete canceled.")
            return
        input_widget.value = ""
        self.set_status("Enter uppercase Y or N.")

    @on(Input.Submitted, "#group-create-name")
    @on(Input.Submitted, "#group-create-display")
    def on_create_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit_create()

    @on(Input.Submitted, "#group-join-invite")
    @on(Input.Submitted, "#group-join-display")
    def on_join_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit_join()

    @on(Input.Submitted, "#group-rename-display")
    def on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit_rename()


class ActiveGroupScreen(ModalScreen[None]):
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
    ActiveGroupScreen {
        align: center middle;
    }

    #active-group-modal {
        width: 124;
        height: 34;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #active-group-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #active-group-help {
        height: 2;
        color: $text-muted;
    }

    #active-group-table {
        height: 1fr;
        border: solid $panel;
        background: $surface;
        color: $text-muted;
    }

    #active-group-table-gap {
        height: 1;
    }

    .active-group-row {
        height: 3;
    }

    .active-group-row Button {
        width: 18;
    }

    .active-group-gap {
        width: 1;
    }

    .active-group-fill {
        width: 1fr;
    }

    #active-group-rename {
        width: 32;
        background: $panel;
    }

    #active-group-remove-confirm {
        width: 8;
        background: $panel;
    }

    #active-group-status {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        group_name: str,
        is_owner: bool,
        get_rows: Callable[[], list[dict]],
        rename_me: Callable[[str], None],
        issue_invite: Callable[[], str | None],
        remove_member: Callable[[str], None],
        display_name: str = "",
    ) -> None:
        super().__init__()
        self.group_name = group_name
        self.is_owner = is_owner
        self.get_rows = get_rows
        self.rename_me = rename_me
        self.issue_invite = issue_invite
        self.remove_member = remove_member
        self.display_name = display_name
        self.row_b32s: list[str] = []
        self.mode = "idle"
        self.pending_remove_b32: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="active-group-modal"):
            yield Static(f"Group: {self.group_name}", id="active-group-title")
            yield Static("Arrows select member. Tab switches fields. Y/N confirms removal. Esc closes.", id="active-group-help")
            yield DataTable(
                show_row_labels=False,
                zebra_stripes=True,
                cursor_type="row",
                id="active-group-table",
            )
            yield Static("", id="active-group-table-gap")
            with Horizontal(classes="active-group-row"):
                yield Button("Invite", id="active-group-invite", variant="success", compact=True)
                yield Static("", classes="active-group-gap")
                yield Button("Remove", id="active-group-remove", variant="error", compact=True)
                yield Static("", classes="active-group-gap")
                yield Input(placeholder="Y/N", max_length=1, id="active-group-remove-confirm", compact=True, disabled=True)
                yield Static("", classes="active-group-fill")
            with Horizontal(classes="active-group-row"):
                yield Button("Rename Me", id="active-group-rename-button", variant="primary", compact=True)
                yield Static("", classes="active-group-gap")
                yield Input(self.display_name, placeholder="your display name", id="active-group-rename", compact=True)
                yield Static("", classes="active-group-fill")
            yield Static("", id="active-group-status")

    def on_mount(self) -> None:
        table = self.query_one("#active-group-table", DataTable)
        table.add_columns("Role", "State", "Name", "Address")
        self.refresh_table()
        self.apply_mode()
        if table.row_count:
            table.focus()

    def action_close(self) -> None:
        if self.mode == "confirm_remove":
            self.reset_remove()
            self.set_status("Removal canceled.")
            return
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#active-group-table", DataTable).action_cursor_up()

    def action_scroll_down(self) -> None:
        self.query_one("#active-group-table", DataTable).action_cursor_down()

    def action_page_up(self) -> None:
        self.query_one("#active-group-table", DataTable).action_page_up()

    def action_page_down(self) -> None:
        self.query_one("#active-group-table", DataTable).action_page_down()

    def action_scroll_home(self) -> None:
        self.query_one("#active-group-table", DataTable).action_scroll_home()

    def action_scroll_end(self) -> None:
        self.query_one("#active-group-table", DataTable).action_scroll_end()

    def set_status(self, text: str) -> None:
        self.query_one("#active-group-status", Static).update(text)

    def apply_mode(self) -> None:
        locked = self.mode != "idle"
        table = self.query_one("#active-group-table", DataTable)
        invite = self.query_one("#active-group-invite", Button)
        remove = self.query_one("#active-group-remove", Button)
        remove_confirm = self.query_one("#active-group-remove-confirm", Input)
        rename_button = self.query_one("#active-group-rename-button", Button)
        rename_input = self.query_one("#active-group-rename", Input)

        table.disabled = locked
        invite.disabled = locked or not self.is_owner
        remove.disabled = locked or not self.is_owner
        remove_confirm.disabled = self.mode != "confirm_remove"
        rename_button.disabled = locked
        rename_input.disabled = locked

    def refresh_table(self) -> None:
        table = self.query_one("#active-group-table", DataTable)
        current_row = table.cursor_row
        table.clear()
        self.row_b32s = []

        for row in self.get_rows():
            b32 = row["b32"]
            self.row_b32s.append(b32)
            display_b32 = b32.replace(".b32.i2p", "")
            if display_b32:
                display_b32 = f"{display_b32[:10]}...{display_b32[-10:]}"
            table.add_row(
                row["role"],
                row["state"],
                row["name"],
                display_b32,
                key=b32,
            )

        if table.row_count:
            table.move_cursor(row=min(max(current_row, 0), table.row_count - 1), animate=False)

    def selected_b32(self) -> str | None:
        table = self.query_one("#active-group-table", DataTable)
        if table.row_count == 0:
            return None
        if 0 <= table.cursor_row < len(self.row_b32s):
            return self.row_b32s[table.cursor_row]
        return None

    def submit_invite(self) -> None:
        invite = self.issue_invite()
        if invite:
            self.set_status("Invite copied to clipboard and written to output.")
        else:
            self.set_status("Only the group owner can issue invites.")

    def submit_rename(self) -> None:
        name = self.query_one("#active-group-rename", Input).value.strip()
        if not name:
            self.set_status("Enter your display name first.")
            self.query_one("#active-group-rename", Input).focus()
            return
        self.rename_me(name)
        self.set_status("Rename submitted.")

    def start_remove(self) -> None:
        b32 = self.selected_b32()
        if not b32:
            self.set_status("No member selected.")
            return
        self.pending_remove_b32 = b32
        self.mode = "confirm_remove"
        confirm = self.query_one("#active-group-remove-confirm", Input)
        confirm.value = ""
        self.apply_mode()
        self.set_status(f"Remove selected member? Enter Y or N: {b32}")
        confirm.focus()

    def reset_remove(self) -> None:
        self.pending_remove_b32 = None
        self.mode = "idle"
        confirm = self.query_one("#active-group-remove-confirm", Input)
        confirm.value = ""
        self.apply_mode()
        self.query_one("#active-group-table", DataTable).focus()

    def confirm_remove(self) -> None:
        if not self.pending_remove_b32:
            return
        self.remove_member(self.pending_remove_b32)
        self.reset_remove()
        self.refresh_table()
        self.set_status("Removal submitted.")

    @on(Button.Pressed, "#active-group-invite")
    def on_invite_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_invite()

    @on(Button.Pressed, "#active-group-rename-button")
    def on_rename_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.submit_rename()

    @on(Input.Submitted, "#active-group-rename")
    def on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit_rename()

    @on(Button.Pressed, "#active-group-remove")
    def on_remove_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.mode != "idle":
            return
        self.start_remove()

    @on(Input.Submitted, "#active-group-remove-confirm")
    def on_remove_confirm_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.handle_remove_confirmation(event.value, event.input)

    @on(Input.Changed, "#active-group-remove-confirm")
    def on_remove_confirm_changed(self, event: Input.Changed) -> None:
        self.handle_remove_confirmation(event.value, event.input)

    def handle_remove_confirmation(self, value: str, input_widget: Input) -> None:
        if self.mode != "confirm_remove":
            return
        value = value.strip()
        if not value:
            return
        if value == "Y":
            self.confirm_remove()
            return
        if value == "N":
            self.reset_remove()
            self.set_status("Removal canceled.")
            return
        input_widget.value = ""
        self.set_status("Enter uppercase Y or N.")
