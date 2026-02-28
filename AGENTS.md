# AGENTS.md

## Monorepo Test Rule (Required)
- Never run `pytest` from global PATH.
- Always run tests via the project-local venv interpreter.
- For dialog manager tests use: `py3_dialog_manager\venv\Scripts\python.exe -m pytest ...`
- For script runner tests use: `py3_script_runner\venv\Scripts\python.exe -m pytest ...`
- If tests span both projects, run them as separate commands with each project's venv.
