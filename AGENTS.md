# Repository workflow

- When a change affects requirements, configuration, startup or deployment, architecture, directory structure, core processing, or user-facing features, update the relevant README sections in the same change. Keep commands and paths aligned with the code.
- After validating repository changes, commit and push them to `main`. Do not deploy or restart the service after pushing; the user manually runs `/opt/audio-book/reinstall.sh` when ready. Preserve runtime data under `$NOVEL_TTS_HOME`; do not replace the repository directory to deploy.
