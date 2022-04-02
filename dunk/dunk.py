import functools
import sys
from pathlib import Path
from typing import Dict, List, cast, Iterable, Tuple, TypeVar

import rich
from rich.color import blend_rgb, Color
from rich.color_triplet import ColorTriplet
from rich.console import Console
from rich.segment import Segment, SegmentLines
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from unidiff import PatchSet
from unidiff.patch import PatchedFile, Hunk, Line

console = Console(force_terminal=True)
T = TypeVar("T")
MONOKAI_BACKGROUND = Color.from_triplet(ColorTriplet(red=39, green=40, blue=34))


# TODO: Use rich pager here?

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
    diff = "".join(sys.stdin.readlines())
    patch_set: List[PatchedFile] = PatchSet(diff)

    for is_first, patch in loop_first(patch_set):
        if patch.is_binary_file:
            # TODO - show something here
            continue

        source_lineno = 1
        target_lineno = 1

        target_code = Path(patch.path).read_text()
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

        additional_context = ""
        if patch.is_added_file:
            additional_context += "[green]file was added[/]"
        elif patch.is_removed_file:
            additional_context += "[red]file was deleted[/]"

        if is_first:
            console.print()
        else:
            console.rule(characters="▔", style=Style.from_color(color=MONOKAI_BACKGROUND))
        console.print(
            f"  [b]{patch.path}[/] ([green]{patch.added} additions[/], [red]{patch.removed} removals[/]) {additional_context}")

        # TODO - keep track of source_lineno -> diff_lineno and same for target,
        #  so that we can reconstruct unified diff

        source_code = "".join(source_reconstructed)
        lexer = Syntax.guess_lexer(patch.path)

        for hunk in patch:
            # Use difflib to examine differences between each link of the hunk
            # Target essentially means the additions/green text in the diff
            target_line_range = (hunk.target_start, hunk.target_length + hunk.target_start - 1)
            source_line_range = (hunk.source_start, hunk.source_length + hunk.source_start - 1)

            source_syntax = Syntax(source_code, lexer=lexer, line_range=source_line_range, line_numbers=True,
                                   indent_guides=True)
            target_syntax = Syntax(target_code, lexer=lexer, line_range=target_line_range, line_numbers=True,
                                   indent_guides=True)

            # Gather info on source which lines were added/removed, so we can highlight them
            source_removed_linenos = set()
            target_added_linenos = set()
            for line in hunk:
                line = cast(Line, line)
                if line.source_line_no and line.is_removed:
                    rich.inspect(line)
                    source_removed_linenos.add(line.source_line_no)
                elif line.target_line_no and line.is_added:
                    target_added_linenos.add(line.target_line_no)
                elif line.is_context:
                    pass
                    # rich.inspect(line)

            # For inline diffing
            # if you have a contiguous streak of removal lines, followed by a contiguous streak of addition lines,
            # you can collect the removals into a string, collect the additions into a string, and diff two strings,
            # to find the locations in the line where things differ

            source_syntax_lines: List[List[Segment]] = console.render_lines(source_syntax)
            target_syntax_lines = console.render_lines(target_syntax)

            highlighted_source_lines = highlight_lines_in_hunk(hunk.source_start, source_removed_linenos,
                                                               source_syntax_lines, ColorTriplet(255, 0, 0))
            highlighted_target_lines = highlight_lines_in_hunk(hunk.target_start, target_added_linenos,
                                                               target_syntax_lines, ColorTriplet(0, 255, 0))

            table = Table.grid()
            table.add_column()
            table.add_column()
            table.add_row(
                SegmentLines(highlighted_source_lines, new_lines=True),
                SegmentLines(highlighted_target_lines, new_lines=True),
            )

            console.print(table)


def highlight_lines_in_hunk(start_lineno, highlight_linenos, syntax_lines, blend_colour):
    highlighted_lines = []
    for index, line in enumerate(syntax_lines):
        lineno = index + start_lineno
        if lineno in highlight_linenos:
            new_line = []
            for segment in line:
                style: Style
                text, style, control = segment
                if style and style.bgcolor:
                    bgcolor_triplet = style.bgcolor.triplet
                    new_bgcolour_triplet = blend_rgb_cached(blend_colour, bgcolor_triplet, cross_fade=.85)
                    new_bgcolor = Color.from_triplet(new_bgcolour_triplet)
                    updated_style = style + Style.from_color(bgcolor=new_bgcolor)
                    new_line.append(Segment(text, updated_style, control))
                else:
                    new_line.append(segment)
        else:
            new_line = line[:]

        highlighted_lines.append(new_line)
    return highlighted_lines


@functools.lru_cache(maxsize=128)
def blend_rgb_cached(colour1, colour2, cross_fade=0.85):
    return blend_rgb(colour1, colour2, cross_fade=cross_fade)


if __name__ == '__main__':
    main()
