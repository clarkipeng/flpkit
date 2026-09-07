# Repository instructions

Read [TASTE.md](TASTE.md) for how we make decisions before starting work.
This file gives the repository's commands, workflow, and safety rules; linked docs explain the details.
Apply TASTE within these rules. Fix conflicting instructions where they are written instead of adding another copy.

- [README.md](README.md) owns the project overview and setup instructions.

## Delivery

Open a draft pull request after the relevant local checks pass.
Review the changes before marking it ready; merge as one commit (squash) once required checks pass.

## Shell commands

Keep descriptions and other text from being run as commands.
For example, put a pull request description in `description.md`, then use `gh pr create --body-file description.md`.
That reads the file as text; it does not run code written inside it.
