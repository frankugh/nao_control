# AGENTS.md

## Preferences
- Avoid PowerShell commands where possible; prefer Python one-liners or file edits via apply_patch.
- If a shell command is necessary, keep it minimal and explain why.
- Prefer `rg` for search and `rg --files` for file listing.
- Ask before running any command that could be noisy or touch many files.

## Editing
- Default to ASCII unless the file already contains non-ASCII.
- Keep changes small and focused; avoid reformatting unrelated content.
- When adding new config files, follow existing naming patterns in `configs/`.

## Communication
- Be concise; summarize changes and point to file paths only.
- If you need a global preference updated, ask to edit this file.
