from __future__ import annotations

from pathlib import Path
from typing import Iterable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Input, Static


class FilteredDirectoryTree(DirectoryTree):
    ICON_NODE_EXPANDED = "[-] "
    ICON_NODE = "[+] "
    ICON_FILE = "    "

    def __init__(
        self,
        path: str | Path,
        allowed_extensions: set[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(path, **kwargs)
        self.allowed_extensions = allowed_extensions

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        if self.allowed_extensions is None:
            return paths

        return (
            path
            for path in paths
            if path.is_dir() or path.suffix.lower().lstrip(".") in self.allowed_extensions
        )


class FilePickerScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Cursor up", show=False, priority=True),
        Binding("down", "cursor_down", "Cursor down", show=False, priority=True),
        Binding("left", "parent_directory", "Parent directory", show=False, priority=True),
        Binding("enter", "select_cursor", "Select", show=False, priority=True),
    ]

    CSS = """
    FilePickerScreen {
        align: center middle;
    }

    #file-picker {
        width: 82;
        height: 32;
        max-width: 95%;
        max-height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }

    #file-picker-title {
        height: 1;
        color: $text;
        text-style: bold;
    }

    #file-picker-help {
        height: 2;
        color: $text-muted;
    }

    #file-picker-path {
        height: 3;
    }

    #file-picker-tree {
        height: 1fr;
        border: solid $panel;
    }

    """

    def __init__(
        self,
        title: str,
        start_path: str | Path | None = None,
        allowed_extensions: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.start_path = Path(start_path or Path.home()).expanduser()
        self.allowed_extensions = allowed_extensions

    def compose(self) -> ComposeResult:
        start = self.start_path
        if start.is_file():
            start = start.parent
        if not start.exists() or not start.is_dir():
            start = Path.home()

        with Vertical(id="file-picker"):
            yield Static(self.title, id="file-picker-title")
            yield Static(
                "Enter selects a file. Type a path and press Enter. Esc cancels.",
                id="file-picker-help",
            )
            yield Input(str(start), placeholder="Path...", id="file-picker-path")
            yield FilteredDirectoryTree(
                start,
                allowed_extensions=self.allowed_extensions,
                id="file-picker-tree",
            )

    def on_mount(self) -> None:
        self.focus_tree()
        self.call_after_refresh(self.focus_tree)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def focus_tree(self) -> None:
        self.query_one("#file-picker-tree", FilteredDirectoryTree).focus()

    def action_cursor_up(self) -> None:
        self.query_one("#file-picker-tree", FilteredDirectoryTree).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#file-picker-tree", FilteredDirectoryTree).action_cursor_down()

    def action_select_cursor(self) -> None:
        path_input = self.query_one("#file-picker-path", Input)
        if path_input.has_focus:
            self.select_path(path_input.value)
            return

        self.query_one("#file-picker-tree", FilteredDirectoryTree).action_select_cursor()

    def action_parent_directory(self) -> None:
        path_input = self.query_one("#file-picker-path", Input)
        if path_input.has_focus:
            path_input.action_cursor_left()
            return

        tree = self.query_one("#file-picker-tree", FilteredDirectoryTree)
        parent = Path(tree.path).expanduser().parent
        if parent == Path(tree.path).expanduser():
            return

        tree.path = parent
        path_input.value = str(parent)
        self.focus_tree()

    def path_allowed(self, path: Path) -> bool:
        if self.allowed_extensions is None:
            return True
        return path.suffix.lower().lstrip(".") in self.allowed_extensions

    def select_path(self, raw_path: str | Path) -> None:
        path = Path(raw_path).expanduser()

        if path.is_dir():
            tree = self.query_one("#file-picker-tree", FilteredDirectoryTree)
            path_input = self.query_one("#file-picker-path", Input)
            tree.path = path
            path_input.value = str(path)
            return

        if path.is_file() and self.path_allowed(path):
            self.dismiss(str(path))
            return

        path_input = self.query_one("#file-picker-path", Input)
        path_input.value = str(path)

    @on(DirectoryTree.FileSelected)
    def on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.select_path(event.path)

    @on(DirectoryTree.DirectorySelected)
    def on_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self.select_path(event.path)

    @on(Input.Submitted, "#file-picker-path")
    def on_path_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.select_path(event.value)
