---
name: aya-install
description: Reinstall aya globally via uv tool after pulling updates from main
triggers:
  - "reinstall aya"
  - "refresh aya"
  - "update aya globally"
  - "aya refresh"
---

# Reinstall aya globally

Uninstall the current aya installation and reinstall the latest version from GitHub, with verification.

Run these commands in sequence:

1. Uninstall current version:
```bash
uv tool uninstall aya-ai-assist
```

2. Reinstall latest from GitHub:
```bash
uv tool install --from git+https://github.com/shawnoster/aya aya-ai-assist --force
```

3. Verify installation:
```bash
which aya && aya status -f json | jq '.systems.ok'
```

If the final command returns `true`, installation succeeded. If it returns `false` or errors, the installation failed — report the error to the user.

Do not continue if any step fails.
