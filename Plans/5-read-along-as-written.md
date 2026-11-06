# Read-along shows the message as written

The reading card shows each message the way Claude Code shows it in the terminal, not the cleaned text that gets spoken. Words still highlight as they're spoken. Text that isn't spoken (code blocks, paths, URLs, markdown markers) is shown but never highlighted, or highlights together with the next spoken word.

## Before code
Pick a transcript message with a table, a list, bold text, inline code, a heading and a code block. I paste a screenshot of it in the terminal. Compare it with the raw text and write down below what Claude Code hides or redraws.

## Shown text
- The card shows the whole raw message. Its paragraphs are the chunks' ranges in it, so clicking one still seeks there.
- Markdown renders as in the terminal: `**`, backticks and leading `#` are hidden with an elided Tk tag, and bold, inline code and headings are styled. Code blocks, lists and tables show as written.
- Elided markers keep their character offsets, so a position in the raw message is the same position in the Text widget.

## Word positions
- `extract_speakable_chunks` returns each chunk as a range of the raw message instead of a copied string.
- A new `speak_engine` function maps each cleaned word to its range in the raw chunk by aligning the two word sequences. Raw words split at the same `_`, `/` and `.` boundaries that `clean_text` splits at. A spoken word with no raw match ("and" for `&`, "to" for `→`) gets the raw text between its matched neighbours. Several unmatched words in one gap share it.
- The synth worker attaches that raw range to each WordBoundary word, and the UI highlights those ranges directly. `_map_spans` goes away.

## Events
- `play` carries the raw message, the chunk ranges and the playing chunk's index instead of the cleaned chunk list. `msg_clean` goes away.

## Clipboard hotkey
- `speak_engine` sends the selection only unwrapped, and the daemon cleans it like a transcript message, so the card shows what was selected.
- `--keep-code`, `--keep-paths` and `--raw` go along with the `speak` command, and the daemon cleans with them.

## Test
- Code blocks, paths, lists, tables and bold text look as in the terminal.
- The highlight follows the voice through paths, `snake_case` names, `→` arrows and numbered lists.
- Clicking a paragraph seeks to it. Previous / Next / Repeat land on the right paragraph.
