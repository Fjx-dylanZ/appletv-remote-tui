"""Headless tests for the single-line Vim editor."""

from __future__ import annotations

from textual import events
from textual.app import App, ComposeResult

from appletv_remote_tui.tui import VimInput, VimInputMode


class VimInputApp(App[None]):
    def __init__(self, value: str = "") -> None:
        super().__init__()
        self.initial_value = value
        self.changes: list[str] = []
        self.mode_changes: list[tuple[VimInputMode, str]] = []

    def compose(self) -> ComposeResult:
        yield VimInput(self.initial_value, id="editor")

    def on_mount(self) -> None:
        self.query_one(VimInput).focus()

    def on_input_changed(self, event: VimInput.Changed) -> None:
        self.changes.append(event.value)

    def on_vim_input_mode_changed(self, event: VimInput.ModeChanged) -> None:
        self.mode_changes.append((event.mode, event.pending))


async def test_normal_motions_use_character_cursor_semantics() -> None:
    app = VimInputApp("  one two.three")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0

        await pilot.press("circumflex_accent")
        assert editor.cursor_position == 2
        await pilot.press("w")
        assert editor.cursor_position == 6
        await pilot.press("e")
        assert editor.cursor_position == 8
        await pilot.press("w")
        assert editor.cursor_position == 9
        await pilot.press("w", "b")
        assert editor.cursor_position == 9
        await pilot.press("dollar_sign", "0")
        assert editor.cursor_position == 0


async def test_operator_supports_character_and_first_nonblank_motions() -> None:
    app = VimInputApp("  alpha")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 4

        await pilot.press("d", "h")
        assert editor.value == "  apha"
        assert editor.cursor_position == 3

        await pilot.press("d", "l")
        assert editor.value == "  aha"
        assert editor.cursor_position == 3

        editor.cursor_position = len(editor.value) - 1
        await pilot.press("d", "circumflex_accent")
        assert editor.value == "  a"


async def test_word_cursor_alias_cannot_leave_normal_cursor_at_virtual_eol() -> None:
    app = VimInputApp("one two")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0

        await pilot.press("ctrl+right", "ctrl+right")
        assert editor.cursor_position == len(editor.value) - 1

        await pilot.press("c", "w", "X", "escape")
        assert editor.value == "one twX"

        await pilot.click(editor, offset=(editor.size.width - 2, 1))
        assert editor.cursor_position == len(editor.value) - 1


async def test_loading_a_new_remote_value_resets_undo_history() -> None:
    app = VimInputApp("one")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0

        await pilot.press("x")
        assert editor.value == "ne"
        editor.load_value("two")
        await pilot.press("u")

        assert editor.value == "two"
        assert editor.cursor_position == 2


async def test_substitute_on_empty_input_enters_insert_mode() -> None:
    app = VimInputApp("")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)

        await pilot.press("s", "X")

        assert editor.vim_mode is VimInputMode.INSERT
        assert editor.value == "X"


async def test_first_nonblank_and_big_i_handle_whitespace_only_input() -> None:
    app = VimInputApp("   ")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 1

        await pilot.press("circumflex_accent")
        assert editor.cursor_position == 2

        await pilot.press("I", "X", "escape")
        assert editor.value == "   X"


async def test_backward_yank_moves_cursor_but_line_yank_preserves_it() -> None:
    app = VimInputApp("alpha beta")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 8

        await pilot.press("y", "i", "w")
        assert editor.yank_register == "beta"
        assert editor.cursor_position == 6

        editor.cursor_position = 8
        await pilot.press("y", "y")
        assert editor.yank_register == "alpha beta"
        assert editor.cursor_position == 8


async def test_insert_commands_and_literal_vim_keys() -> None:
    app = VimInputApp("find ")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0

        await pilot.press("A", "h", "j", "k", "l")
        assert editor.vim_mode is VimInputMode.INSERT
        assert editor.value == "find hjkl"
        assert editor.cursor_position == len(editor.value)

        await pilot.press("escape")
        assert editor.vim_mode is VimInputMode.NORMAL
        assert editor.cursor_position == len(editor.value) - 1
        assert app.mode_changes[0] == (VimInputMode.INSERT, "")
        assert app.mode_changes[-1] == (VimInputMode.NORMAL, "")


async def test_paste_is_exactly_once_in_insert_and_inert_in_normal() -> None:
    app = VimInputApp("x")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0
        editor.enter_insert("A")

        editor.post_message(events.Paste("界🙂"))
        await pilot.pause()
        assert editor.value == "x界🙂"

        editor.enter_normal()
        editor.post_message(events.Paste("ignored"))
        await pilot.pause()
        assert editor.value == "x界🙂"


async def test_diw_deletes_word_without_surrounding_space() -> None:
    app = VimInputApp("alpha beta gamma")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 8

        await pilot.press("d", "i", "w")

        assert editor.value == "alpha  gamma"
        assert editor.cursor_position == 6
        assert editor.yank_register == "beta"
        assert app.changes[-1] == "alpha  gamma"


async def test_ciw_enters_insert_and_escape_commits_one_undo_step() -> None:
    app = VimInputApp("alpha beta gamma")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 8

        await pilot.press("c", "i", "w", "n", "e", "w", "escape")
        assert editor.value == "alpha new gamma"
        assert editor.vim_mode is VimInputMode.NORMAL
        assert editor.cursor_position == 8

        await pilot.press("u")
        assert editor.value == "alpha beta gamma"
        assert editor.cursor_position == 8
        await pilot.press("ctrl+r")
        assert editor.value == "alpha new gamma"


async def test_dw_cw_and_end_operators() -> None:
    app = VimInputApp("one two three")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 0

        await pilot.press("d", "w")
        assert editor.value == "two three"
        assert editor.yank_register == "one "

        await pilot.press("c", "w", "T", "W", "O", "escape")
        assert editor.value == "TWO three"

        editor.cursor_position = 4
        await pilot.press("D")
        assert editor.value == "TWO "
        await pilot.press("u", "C", "x", "escape")
        assert editor.value == "TWO x"


async def test_pending_operator_can_be_cancelled_without_editing() -> None:
    app = VimInputApp("alpha beta")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 2

        await pilot.press("d")
        assert editor.pending == "d"
        assert editor.cancel_pending()
        assert not editor.cancel_pending()
        assert editor.value == "alpha beta"
        await pilot.pause()
        assert app.mode_changes[-1] == (VimInputMode.NORMAL, "")

        await pilot.press("c", "i", "escape")
        assert editor.pending == ""
        assert editor.vim_mode is VimInputMode.NORMAL
        assert editor.value == "alpha beta"


async def test_normal_keys_never_insert_and_x_is_undoable() -> None:
    app = VimInputApp("abcd")
    async with app.run_test() as pilot:
        editor = app.query_one(VimInput)
        editor.cursor_position = 1

        await pilot.press("j", "k", "q", "z")
        assert editor.value == "abcd"
        await pilot.press("x")
        assert editor.value == "acd"
        assert editor.cursor_position == 1
        await pilot.press("u")
        assert editor.value == "abcd"
        assert editor.cursor_position == 1
