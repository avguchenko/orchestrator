import os

# The orchestrator spawns headless Claude Code sessions via the SDK.
# When running inside an existing Claude Code session, the CLAUDECODE env var
# blocks nested launches. Unset it so SDK calls work from any context.
os.environ.pop("CLAUDECODE", None)
