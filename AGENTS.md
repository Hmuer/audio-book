# Repository workflow

- When a change affects requirements, configuration, startup or deployment, architecture, directory structure, core processing, or user-facing features, update the relevant README sections in the same change. Keep commands and paths aligned with the code.
- After validating repository changes, commit and push them to `main`, then run `./deploy.sh` and verify the service is healthy. Preserve runtime data under `$NOVEL_TTS_HOME`; do not replace the repository directory to deploy.
