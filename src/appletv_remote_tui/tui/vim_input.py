"""A small Vim-style, single-line editor built on Textual's ``Input``.

The widget deliberately implements a useful subset of Vim instead of trying to
emulate the full editor.  NORMAL mode treats ``cursor_position`` as the index of
the character under the cursor; INSERT mode uses Textual's usual insertion-point
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from textual import events
from textual.message import Message
from textual.widgets import Input


class VimInputMode(StrEnum):
    """Editing modes supported by :class:`VimInput`."""

    NORMAL = "normal"
    INSERT = "insert"


@dataclass(frozen=True)
class _Snapshot:
    value: str
    cursor: int


class VimInput(Input):
    """A Textual input with Vim-like NORMAL and INSERT modes.

    Supported NORMAL commands include character and word motions, common insert
    commands, character deletion, operators with motions, inner-word text
    objects, a character-wise yank register, paste, and undo/redo.
    """

    _OPERATORS: ClassVar[frozenset[str]] = frozenset({"d", "c", "y"})
    _OPERATOR_MOTIONS: ClassVar[frozenset[str]] = frozenset(
        {"h", "l", "w", "b", "e", "0", "^", "$"}
    )

    @dataclass
    class ModeChanged(Message):
        """Posted when the Vim mode or pending operator changes."""

        input: VimInput
        mode: VimInputMode
        pending: str

        @property
        def control(self) -> VimInput:
            """Return the input that changed state."""
            return self.input

    def __init__(
        self,
        value: str | None = None,
        *,
        placeholder: str = "",
        id: str | None = None,
        disabled: bool = False,
    ) -> None:
        self.vim_mode = VimInputMode.NORMAL
        self.pending = ""
        self._yank_register = ""
        self._undo_stack: list[_Snapshot] = []
        self._redo_stack: list[_Snapshot] = []
        self._insert_snapshot: _Snapshot | None = None
        super().__init__(
            value,
            placeholder=placeholder,
            id=id,
            disabled=disabled,
            select_on_focus=False,
        )
        self._clamp_normal_cursor()

    @property
    def yank_register(self) -> str:
        """Return the contents of the unnamed, character-wise register."""
        return self._yank_register

    def _set_editor_state(
        self,
        *,
        mode: VimInputMode | None = None,
        pending: str | None = None,
    ) -> None:
        next_mode = self.vim_mode if mode is None else mode
        next_pending = self.pending if pending is None else pending
        if next_mode is self.vim_mode and next_pending == self.pending:
            return
        self.vim_mode = next_mode
        self.pending = next_pending
        self.post_message(self.ModeChanged(self, self.vim_mode, self.pending))

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(self.value, self.cursor_position)

    def _push_undo(self, snapshot: _Snapshot) -> None:
        if snapshot.value == self.value:
            return
        self._undo_stack.append(snapshot)
        self._redo_stack.clear()

    def _restore(self, snapshot: _Snapshot) -> None:
        self.value = snapshot.value
        self.cursor_position = snapshot.cursor
        self._clamp_normal_cursor()

    def _clamp_normal_cursor(self) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self.cursor_position = max(0, min(self.cursor_position, max(0, len(self.value) - 1)))

    def load_value(self, value: str) -> None:
        """Load a new remote field and start it with fresh local undo history.

        The unnamed register intentionally survives loads, like a Vim register
        surviving a buffer switch, but edits from one tvOS field must never be
        undoable into a later field.
        """
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._insert_snapshot = None
        self.value = value
        self.cursor_position = max(0, len(value) - 1)
        self._set_editor_state(mode=VimInputMode.NORMAL, pending="")
        self._clamp_normal_cursor()

    def enter_insert(self, command: str = "i") -> None:
        """Enter INSERT mode using Vim's ``i``, ``a``, ``I``, or ``A`` position."""
        if command not in {"i", "a", "I", "A"}:
            raise ValueError(f"unsupported insert command: {command!r}")
        if self.vim_mode is VimInputMode.INSERT:
            return

        self._insert_snapshot = self._snapshot()
        if not self.value:
            target = 0
        elif command == "i":
            target = self.cursor_position
        elif command == "a":
            target = min(len(self.value), self.cursor_position + 1)
        elif command == "I":
            target = next(
                (index for index, character in enumerate(self.value) if not character.isspace()),
                len(self.value),
            )
        else:
            target = len(self.value)
        self.cursor_position = target
        self._set_editor_state(mode=VimInputMode.INSERT, pending="")

    def _enter_insert_at(self, position: int, snapshot: _Snapshot) -> None:
        self.cursor_position = position
        self._insert_snapshot = snapshot
        self._set_editor_state(mode=VimInputMode.INSERT, pending="")

    def enter_normal(self) -> None:
        """Leave INSERT mode and place the cursor on the preceding character."""
        if self.vim_mode is VimInputMode.NORMAL:
            self.cancel_pending()
            self._clamp_normal_cursor()
            return

        snapshot = self._insert_snapshot
        self._insert_snapshot = None
        if snapshot is not None:
            self._push_undo(snapshot)
        self.cursor_position = max(0, self.cursor_position - 1)
        self._set_editor_state(mode=VimInputMode.NORMAL, pending="")
        self._clamp_normal_cursor()

    def cancel_pending(self) -> bool:
        """Cancel a pending operator, returning whether anything was cancelled."""
        if not self.pending:
            return False
        self._set_editor_state(pending="")
        return True

    @staticmethod
    def _character_kind(character: str) -> int:
        if character.isspace():
            return 0
        if character.isalnum() or character == "_":
            return 1
        return 2

    def _next_word_start(self, cursor: int) -> int:
        value = self.value
        length = len(value)
        if cursor >= length:
            return length
        index = cursor
        kind = self._character_kind(value[index])
        while index < length and self._character_kind(value[index]) == kind:
            index += 1
        while index < length and value[index].isspace():
            index += 1
        return index

    def _previous_word_start(self, cursor: int) -> int:
        if cursor <= 0 or not self.value:
            return 0
        index = min(cursor - 1, len(self.value) - 1)
        while index > 0 and self.value[index].isspace():
            index -= 1
        kind = self._character_kind(self.value[index])
        while index > 0 and self._character_kind(self.value[index - 1]) == kind:
            index -= 1
        return index

    def _word_end(self, cursor: int) -> int:
        value = self.value
        length = len(value)
        if not value:
            return 0
        index = min(cursor, length - 1)
        kind = self._character_kind(value[index])
        if kind == 0 or (index + 1 < length and self._character_kind(value[index + 1]) != kind):
            index += 1
            while index < length and value[index].isspace():
                index += 1
            if index >= length:
                return length - 1
            kind = self._character_kind(value[index])
        while index + 1 < length and self._character_kind(value[index + 1]) == kind:
            index += 1
        return index

    def _inner_word_range(self) -> tuple[int, int]:
        if not self.value:
            return (0, 0)
        cursor = min(self.cursor_position, len(self.value) - 1)
        kind = self._character_kind(self.value[cursor])
        start = cursor
        end = cursor + 1
        while start > 0 and self._character_kind(self.value[start - 1]) == kind:
            start -= 1
        while end < len(self.value) and self._character_kind(self.value[end]) == kind:
            end += 1
        return (start, end)

    def _first_nonblank(self) -> int:
        """Return the first non-whitespace character or the last blank character."""
        return next(
            (index for index, character in enumerate(self.value) if not character.isspace()),
            max(0, len(self.value) - 1),
        )

    def _operator_range(self, motion: str) -> tuple[int, int]:
        cursor = self.cursor_position
        if motion == "h":
            return (max(0, cursor - 1), cursor)
        if motion == "l":
            return (cursor, min(len(self.value), cursor + 1))
        if motion == "w":
            return (cursor, self._next_word_start(cursor))
        if motion == "b":
            return (self._previous_word_start(cursor), cursor)
        if motion == "e":
            return (cursor, self._word_end(cursor) + 1)
        if motion == "0":
            return (0, cursor)
        if motion == "^":
            return (self._first_nonblank(), cursor)
        return (cursor, len(self.value))

    def _apply_operator(self, operator: str, start: int, end: int) -> None:
        start, end = sorted((max(0, start), min(len(self.value), end)))
        selected = self.value[start:end]
        snapshot = self._snapshot()
        self._set_editor_state(pending="")

        if operator == "y":
            if selected:
                self._yank_register = selected
            self.cursor_position = start
            self._clamp_normal_cursor()
            return
        if selected:
            self._yank_register = selected
            self.replace("", start, end)
        self.cursor_position = start
        if operator == "c":
            self._enter_insert_at(start, snapshot)
        else:
            self._push_undo(snapshot)
            self._clamp_normal_cursor()

    def _apply_line_operator(self, operator: str) -> None:
        cursor = self.cursor_position
        self._apply_operator(operator, 0, len(self.value))
        if operator == "y":
            self.cursor_position = cursor
            self._clamp_normal_cursor()

    def _delete_character(self, *, before: bool = False, insert: bool = False) -> None:
        if not self.value:
            if insert:
                snapshot = self._snapshot()
                self._enter_insert_at(0, snapshot)
            return
        start = self.cursor_position - 1 if before else self.cursor_position
        if start < 0 or start >= len(self.value):
            return
        snapshot = self._snapshot()
        self._yank_register = self.value[start : start + 1]
        self.replace("", start, start + 1)
        self.cursor_position = start
        if insert:
            self._enter_insert_at(start, snapshot)
        else:
            self._push_undo(snapshot)
            self._clamp_normal_cursor()

    def _paste(self, *, before: bool) -> None:
        if not self._yank_register:
            return
        snapshot = self._snapshot()
        index = 0 if not self.value else self.cursor_position + (0 if before else 1)
        self.insert(self._yank_register, index)
        if self.value != snapshot.value:
            self.cursor_position = index + len(self._yank_register) - 1
        self._push_undo(snapshot)
        self._clamp_normal_cursor()

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        previous = self._undo_stack.pop()
        self._redo_stack.append(self._snapshot())
        self._restore(previous)

    def _redo(self) -> None:
        if not self._redo_stack:
            return
        following = self._redo_stack.pop()
        self._undo_stack.append(self._snapshot())
        self._restore(following)

    def _handle_pending(self, character: str) -> None:
        operator = self.pending[0]
        if len(self.pending) == 1:
            if character == operator:
                self._set_editor_state(pending="")
                self._apply_line_operator(operator)
            elif character == "i":
                self._set_editor_state(pending=f"{operator}i")
            elif character in self._OPERATOR_MOTIONS:
                if (
                    operator == "c"
                    and character == "w"
                    and self.value
                    and not self.value[self.cursor_position].isspace()
                ):
                    # Vim's ``cw`` is historically equivalent to ``ce`` while
                    # sitting on a non-blank character, so it preserves the
                    # separator before the next word.
                    selected_range = (
                        self.cursor_position,
                        self._word_end(self.cursor_position) + 1,
                    )
                else:
                    selected_range = self._operator_range(character)
                self._apply_operator(operator, *selected_range)
            else:
                self.cancel_pending()
            return

        if self.pending[1:] == "i" and character == "w":
            self._apply_operator(operator, *self._inner_word_range())
        else:
            self.cancel_pending()

    def _handle_normal_character(self, character: str) -> None:
        # Mouse placement and inherited toolkit actions use an insertion-point
        # cursor and may temporarily place it at ``len(value)``. Restore Vim's
        # character-cursor invariant before indexing or applying a command.
        self._clamp_normal_cursor()
        if self.pending:
            self._handle_pending(character)
            return
        if character in self._OPERATORS:
            self._set_editor_state(pending=character)
        elif character == "h":
            self.cursor_position = max(0, self.cursor_position - 1)
        elif character == "l":
            self.cursor_position = min(max(0, len(self.value) - 1), self.cursor_position + 1)
        elif character == "w":
            self.cursor_position = min(
                max(0, len(self.value) - 1), self._next_word_start(self.cursor_position)
            )
        elif character == "b":
            self.cursor_position = self._previous_word_start(self.cursor_position)
        elif character == "e":
            self.cursor_position = self._word_end(self.cursor_position)
        elif character == "0":
            self.cursor_position = 0
        elif character == "^":
            self.cursor_position = self._first_nonblank()
        elif character == "$":
            self.cursor_position = max(0, len(self.value) - 1)
        elif character in {"i", "a", "I", "A"}:
            self.enter_insert(character)
        elif character == "x":
            self._delete_character()
        elif character == "X":
            self._delete_character(before=True)
        elif character == "D":
            self._apply_operator("d", self.cursor_position, len(self.value))
        elif character == "C":
            self._apply_operator("c", self.cursor_position, len(self.value))
        elif character == "s":
            self._delete_character(insert=True)
        elif character == "S":
            self._apply_line_operator("c")
        elif character == "p":
            self._paste(before=False)
        elif character == "P":
            self._paste(before=True)
        elif character == "u":
            self._undo()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            if self.vim_mode is VimInputMode.INSERT:
                self.enter_normal()
                event.stop()
                event.prevent_default()
            elif self.cancel_pending():
                event.stop()
                event.prevent_default()
            return

        if self.vim_mode is VimInputMode.NORMAL and event.key == "ctrl+r":
            self._redo()
            event.stop()
            event.prevent_default()
            return

        if self.vim_mode is VimInputMode.NORMAL and event.is_printable:
            assert event.character is not None
            self._handle_normal_character(event.character)
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def _on_paste(self, event: events.Paste) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            event.prevent_default()
            event.stop()
        # Textual dispatches the inherited Input handler automatically when the
        # event is not stopped, so INSERT mode deliberately does not call super.

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        await super()._on_mouse_down(event)
        self._clamp_normal_cursor()
        event.prevent_default()

    async def _on_mouse_move(self, event: events.MouseMove) -> None:
        await super()._on_mouse_move(event)
        self._clamp_normal_cursor()
        event.prevent_default()

    async def _on_click(self, event: events.Click) -> None:
        await super()._on_click(event)
        self._clamp_normal_cursor()
        event.prevent_default()

    def action_cursor_left(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self.cursor_position = max(0, self.cursor_position - 1)
            return
        super().action_cursor_left(select)

    def action_cursor_right(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self.cursor_position = min(max(0, len(self.value) - 1), self.cursor_position + 1)
            return
        super().action_cursor_right(select)

    def action_cursor_left_word(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self._clamp_normal_cursor()
            self.cursor_position = self._previous_word_start(self.cursor_position)
            return
        super().action_cursor_left_word(select)

    def action_cursor_right_word(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self._clamp_normal_cursor()
            self.cursor_position = min(
                max(0, len(self.value) - 1), self._next_word_start(self.cursor_position)
            )
            return
        super().action_cursor_right_word(select)

    def action_home(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self.cursor_position = 0
            return
        super().action_home(select)

    def action_end(self, select: bool = False) -> None:
        if self.vim_mode is VimInputMode.NORMAL:
            self.cursor_position = max(0, len(self.value) - 1)
            return
        super().action_end(select)

    def action_select_all(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_select_all()

    def action_delete_left(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_left()

    def action_delete_right(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_right()

    def action_delete_left_word(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_left_word()

    def action_delete_right_word(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_right_word()

    def action_delete_left_all(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_left_all()

    def action_delete_right_all(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_delete_right_all()

    def action_cut(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_cut()

    def action_paste(self) -> None:
        if self.vim_mode is VimInputMode.INSERT:
            super().action_paste()
