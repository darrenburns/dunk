import functools
import os
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, cast, Iterable, Tuple, TypeVar, Optional, Set, NamedTuple

from rich.align import Align
from rich.color import blend_rgb, Color
from rich.color_triplet import ColorTriplet
from rich.console import Console
from rich.segment import Segment, SegmentLines
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from unidiff import PatchSet
from unidiff.patch import Hunk, Line, PatchedFile

import dunk
from dunk.renderables import (
    PatchSetHeader,
    RemovedFileBody,
    BinaryFileBody,
    PatchedFileHeader,
    OnlyRenamedFileBody,
)

MONOKAI_LIGHT_ACCENT = Color.from_rgb(62, 64, 54).triplet.hex
MONOKAI_BACKGROUND = Color.from_rgb(red=39, green=40, blue=34)
DUNK_BG_HEX = "#0d0f0b"
MONOKAI_BG_HEX = MONOKAI_BACKGROUND.triplet.hex

T = TypeVar("T")

theme = Theme(
    {
        "hatched": f"{MONOKAI_BG_HEX} on {DUNK_BG_HEX}",
        "renamed": f"cyan",
        "border": MONOKAI_LIGHT_ACCENT,
    }
)
force_width, _ = os.get_terminal_size(2)
console = Console(force_terminal=True, width=force_width, theme=theme)


def find_git_root() -> Path:
    cwd = Path.cwd()
    if (cwd / ".git").exists():
        return Path.cwd()

    for directory in cwd.parents:
        if (directory / ".git").exists():
            return directory

    return cwd


#
class ContiguousStreak(NamedTuple):
    """A single hunk can have multiple streaks of additions/removals of different length"""

    streak_row_start: int
    streak_length: int


def loop_first(values: Iterable[T]) -> Iterable[Tuple[bool, T]]:
    """Iterate and generate a tuple with a flag for first value."""
    iter_values = iter(values)
    try:
        value = next(iter_values)
    except StopIteration:
        return
    yield True, value
    for value in iter_values:
        yield False, value


def main():
    try:
        _run_dunk()
    except BrokenPipeError:
        # Python flushes standard streams on exit; redirect remaining output
        # to devnull to avoid another BrokenPipeError at shutdown
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)


def _run_dunk():
    input = sys.stdin.readlines()
    diff = "".join(input)
    patch_set: PatchSet = PatchSet(diff)

    project_root: Path = find_git_root()

    console.print(
        PatchSetHeader(
            file_modifications=len(patch_set.modified_files),
            file_additions=len(patch_set.added_files),
            file_removals=len(patch_set.removed_files),
            line_additions=patch_set.added,
            line_removals=patch_set.removed,
        )
    )

    for is_first, patch in loop_first(patch_set):
        patch = cast(PatchedFile, patch)
        console.print(PatchedFileHeader(patch))

        if patch.is_removed_file:
            console.print(RemovedFileBody())
            continue

        # The file wasn't removed, so we can open it.
        target_file = project_root / patch.path

        if patch.is_binary_file:
            console.print(BinaryFileBody(size_in_bytes=target_file.stat().st_size))
            continue

        if patch.is_rename and not patch.added and not patch.removed:
            console.print(OnlyRenamedFileBody(patch))

        source_lineno = 1
        target_lineno = 1

        target_code = target_file.read_text()
        target_lines = target_code.splitlines(keepends=True)
        source_lineno_max = len(target_lines) - patch.added + patch.removed

        source_hunk_cache: Dict[int, Hunk] = {hunk.source_start: hunk for hunk in patch}
        source_reconstructed: List[str] = []

        while source_lineno <= source_lineno_max:
            hunk = source_hunk_cache.get(source_lineno)
            if hunk:
                # This line can be reconstructed in source from the hunk
                lines = [line.value for line in hunk.source_lines()]
                source_reconstructed.extend(lines)
                source_lineno += hunk.source_length
                target_lineno += hunk.target_length
            else:
                # The line isn't in the diff, pull over current target lines
                target_line_index = target_lineno - 1

                line = target_lines[target_line_index]
                source_reconstructed.append(line)

                source_lineno += 1
                target_lineno += 1

        source_code = "".join(source_reconstructed)
        lexer = Syntax.guess_lexer(patch.path)

        for is_first_hunk, hunk in loop_first(patch):
            # Use difflib to examine differences between each line of the hunk
            # Target essentially means the additions/green text in diff
            target_line_range = (
                hunk.target_start,
                hunk.target_length + hunk.target_start - 1,
            )
            source_line_range = (
                hunk.source_start,
                hunk.source_length + hunk.source_start - 1,
            )

            source_syntax = Syntax(
                source_code,
                lexer=lexer,
                line_range=source_line_range,
                line_numbers=True,
                indent_guides=True,
            )
            target_syntax = Syntax(
                target_code,
                lexer=lexer,
                line_range=target_line_range,
                line_numbers=True,
                indent_guides=True,
            )
            source_removed_linenos = set()
            target_added_linenos = set()

            context_linenos = []
            for line in hunk:
                line = cast(Line, line)
                if line.source_line_no and line.is_removed:
                    source_removed_linenos.add(line.source_line_no)
                elif line.target_line_no and line.is_added:
                    target_added_linenos.add(line.target_line_no)
                elif line.is_context:
                    context_linenos.append((line.source_line_no, line.target_line_no))

            # To ensure that lines are aligned on the left and right in the split
            # diff, we need to add some padding above the lines the amount of padding
            # can be calculated by *changes* in the difference in offset between the
            # source and target context line numbers. When a change occurs, we note
            # how much the change was, and that's how much padding we need to add. If
            # the change in source - target context line numbers is positive,
            # we pad above the target. If it's negative, we pad above the source line.
            source_lineno_to_padding = {}
            target_lineno_to_padding = {}

            first_source_context, first_target_context = next(
                iter(context_linenos), (0, 0)
            )
            current_delta = first_source_context - first_target_context
            for source_lineno, target_lineno in context_linenos:
                delta = source_lineno - target_lineno
                change_in_delta = current_delta - delta
                pad_amount = abs(change_in_delta)
                if change_in_delta > 0:
                    source_lineno_to_padding[source_lineno] = pad_amount
                elif change_in_delta < 0:
                    target_lineno_to_padding[target_lineno] = pad_amount
                current_delta = delta

            # Track which source and target lines are aligned and should be intraline
            # diffed Work out row number of lines in each side of the diff. Row
            # number is how far from the top of the syntax snippet we are. A line in
            # the source and target with the same row numbers will be aligned in the
            # diff (their line numbers in the source code may be different, though).
            # There can be gaps in row numbers too, since sometimes we add padding
            # above rows to ensure the source and target diffs are aligned with each
            # other.

            # Map row numbers to lines
            source_lines_by_row_index: Dict[int, Line] = {}
            target_lines_by_row_index: Dict[int, Line] = {}

            # We have to track the length of contiguous streaks of altered lines, as
            # we can only provide intraline diffing to aligned streaks of identical
            # length. If they are different lengths it is almost impossible to align
            # the contiguous streaks without falling back to an expensive heuristic.
            # If a source line and a target line map to equivalent ContiguousStreaks,
            # then we can safely apply intraline highlighting to them.
            source_row_to_contiguous_streak_length: Dict[int, ContiguousStreak] = {}

            accumulated_source_padding = 0

            contiguous_streak_row_start = 0
            contiguous_streak_length = 0
            for i, line in enumerate(hunk.source_lines()):
                if line.is_removed:
                    if contiguous_streak_length == 0:
                        contiguous_streak_row_start = i
                    contiguous_streak_length += 1
                else:
                    # We've reached the end of the streak, so we'll associate all the
                    # lines in the streak with it for later lookup.
                    for row_index in range(
                        contiguous_streak_row_start,
                        contiguous_streak_row_start + contiguous_streak_length,
                    ):
                        source_row_to_contiguous_streak_length[row_index] = (
                            ContiguousStreak(
                                streak_row_start=contiguous_streak_row_start,
                                streak_length=contiguous_streak_length,
                            )
                        )
                    contiguous_streak_length = 0

                lineno = hunk.source_start + i
                this_line_padding = source_lineno_to_padding.get(lineno, 0)
                accumulated_source_padding += this_line_padding
                row_number = i + accumulated_source_padding
                source_lines_by_row_index[row_number] = line

            # TODO: Factor out this code into a function, we're doing the same thing
            #  for all lines in both source and target hunks.
            target_row_to_contiguous_streak_length: Dict[int, ContiguousStreak] = {}

            accumulated_target_padding = 0

            target_streak_row_start = 0
            target_streak_length = 0
            for i, line in enumerate(hunk.target_lines()):
                if line.is_added:
                    if target_streak_length == 0:
                        target_streak_row_start = i
                    target_streak_length += 1
                else:
                    for row_index in range(
                        target_streak_row_start,
                        target_streak_row_start + target_streak_length,
                    ):
                        target_row_to_contiguous_streak_length[row_index] = (
                            ContiguousStreak(
                                streak_row_start=target_streak_row_start,
                                streak_length=target_streak_length,
                            )
                        )
                    target_streak_length = 0

                lineno = hunk.target_start + i
                this_line_padding = target_lineno_to_padding.get(lineno, 0)
                accumulated_target_padding += this_line_padding
                row_number = i + accumulated_target_padding
                target_lines_by_row_index[row_number] = line

            row_number_to_deletion_ranges = defaultdict(list)
            row_number_to_insertion_ranges = defaultdict(list)

            # Collect intraline diff info for highlighting
            for row_number, source_line in source_lines_by_row_index.items():
                source_streak = source_row_to_contiguous_streak_length.get(row_number)
                target_streak = target_row_to_contiguous_streak_length.get(row_number)

                # TODO: We need to work out the offsets to ensure that we look up
                #  the correct target and source row streaks to compare. Will probably
                #  need to append accumulated padding to row numbers

                # print(padded_source_row, padded_target_row, source_line.value)
                # if source_streak:
                #     print(f"sourcestreak {row_number}", source_streak)
                # if target_streak:
                #     print(f"targetstreak {row_number}", target_streak)

                intraline_enabled = (
                    source_streak is not None
                    and target_streak is not None
                    and source_streak.streak_length == target_streak.streak_length
                )
                if not intraline_enabled:
                    # print(f"skipping row {row_number}")
                    continue

                target_line = target_lines_by_row_index.get(row_number)

                are_diffable = (
                    source_line
                    and target_line
                    and source_line.is_removed
                    and target_line.is_added
                )
                if target_line and are_diffable:
                    matcher = SequenceMatcher(
                        None, source_line.value, target_line.value
                    )
                    opcodes = matcher.get_opcodes()
                    ratio = matcher.ratio()
                    if ratio > 0.5:
                        for tag, i1, i2, j1, j2 in opcodes:
                            if tag == "delete":
                                row_number_to_deletion_ranges[row_number].append(
                                    (i1, i2)
                                )
                            elif tag == "insert":
                                row_number_to_insertion_ranges[row_number].append(
                                    (j1, j2)
                                )
                            elif tag == "replace":
                                row_number_to_deletion_ranges[row_number].append(
                                    (i1, i2)
                                )
                                row_number_to_insertion_ranges[row_number].append(
                                    (j1, j2)
                                )

            source_syntax_lines: List[List[Segment]] = console.render_lines(
                source_syntax
            )
            target_syntax_lines = console.render_lines(target_syntax)

            highlighted_source_lines = highlight_and_align_lines_in_hunk(
                hunk.source_start,
                source_removed_linenos,
                source_syntax_lines,
                ColorTriplet(255, 0, 0),
                source_lineno_to_padding,
                dict(row_number_to_deletion_ranges),
                gutter_size=len(str(source_lineno_max)) + 2,
            )
            highlighted_target_lines = highlight_and_align_lines_in_hunk(
                hunk.target_start,
                target_added_linenos,
                target_syntax_lines,
                ColorTriplet(0, 255, 0),
                target_lineno_to_padding,
                dict(row_number_to_insertion_ranges),
                gutter_size=len(str(len(target_lines) + 1)) + 2,
            )

            table = Table.grid()
            table.add_column(style="on #0d0f0b")
            table.add_column(style="on #0d0f0b")
            table.add_row(
                SegmentLines(highlighted_source_lines, new_lines=True),
                SegmentLines(highlighted_target_lines, new_lines=True),
            )

            hunk_header_style = f"{MONOKAI_BACKGROUND.triplet.hex} on #0d0f0b"
            hunk_header = (
                f"[on #0d0f0b dim]@@ [red]-{hunk.source_start},{hunk.source_length}[/] "
                f"[green]+{hunk.target_start},{hunk.target_length}[/] "
                f"[dim]@@ {hunk.section_header or ''}[/]"
            )
            console.rule(hunk_header, characters="╲", style=hunk_header_style)
            console.print(table)

        # TODO: File name indicator at bottom of file, if diff is larger than terminal height.
        console.rule(style="border", characters="▔")

    console.print(
        Align.right(
            f"[blue]/[/][red]/[/][green]/[/] [dim]dunk {dunk.__version__}[/]   "
        )
    )
    # console.save_svg("dunk.svg", title="Diff output generated using Dunk")


def highlight_and_align_lines_in_hunk(
    start_lineno: int,
    highlight_linenos: Set[Optional[int]],
    syntax_hunk_lines: List[List[Segment]],
    blend_colour: ColorTriplet,
    lines_to_pad_above: Dict[int, int],
    highlight_ranges: Dict[int, Tuple[int, int]],
    gutter_size: int,
):
    highlighted_lines = []

    # Apply diff-related highlighting to lines
    for index, line in enumerate(syntax_hunk_lines):
        lineno = index + start_lineno

        if lineno in highlight_linenos:
            new_line = []
            segment_number = 0
            for segment in line:
                style: Style
                text, style, control = segment

                if style:
                    if style.bgcolor:
                        bgcolor_triplet = style.bgcolor.triplet
                        cross_fade = 0.85
                        new_bgcolour_triplet = blend_rgb_cached(
                            blend_colour, bgcolor_triplet, cross_fade=cross_fade
                        )
                        new_bgcolor = Color.from_triplet(new_bgcolour_triplet)
                    else:
                        new_bgcolor = None

                    if style.color and segment_number == 1:
                        new_triplet = blend_rgb_cached(
                            blend_rgb_cached(
                                blend_colour, style.color.triplet, cross_fade=0.5
                            ),
                            ColorTriplet(255, 255, 255),
                            cross_fade=0.4,
                        )
                        new_color = Color.from_triplet(new_triplet)
                    else:
                        new_color = None

                    overlay_style = Style.from_color(
                        color=new_color, bgcolor=new_bgcolor
                    )
                    updated_style = style + overlay_style
                    new_line.append(Segment(text, updated_style, control))
                else:
                    new_line.append(segment)
                segment_number += 1
        else:
            new_line = line[:]

        # Pad above the line if required
        pad = lines_to_pad_above.get(lineno, 0)
        for i in range(pad):
            highlighted_lines.append(
                [
                    Segment(
                        "╲" * console.width, Style.from_color(color=MONOKAI_BACKGROUND)
                    )
                ]
            )

        # Finally, apply the intraline diff highlighting for this line if possible
        if index in highlight_ranges:
            line_as_text = Text.assemble(
                *((text, style) for text, style, control in new_line), end=""
            )
            intraline_bgcolor = Color.from_triplet(
                blend_rgb_cached(
                    blend_colour, MONOKAI_BACKGROUND.triplet, cross_fade=0.6
                )
            )
            intraline_color = Color.from_triplet(
                blend_rgb_cached(
                    intraline_bgcolor.triplet,
                    Color.from_rgb(255, 255, 255).triplet,
                    cross_fade=0.8,
                )
            )
            for start, end in highlight_ranges.get(index):
                line_as_text.stylize(
                    Style.from_color(color=intraline_color, bgcolor=intraline_bgcolor),
                    start=start + gutter_size + 1,
                    end=end + gutter_size + 1,
                )
            new_line = list(console.render(line_as_text))
        highlighted_lines.append(new_line)
    return highlighted_lines


@functools.lru_cache(maxsize=128)
def blend_rgb_cached(
    colour1: ColorTriplet, colour2: ColorTriplet, cross_fade: float = 0.6
) -> ColorTriplet:
    return blend_rgb(colour1, colour2, cross_fade=cross_fade)


if __name__ == "__main__":
    main()
