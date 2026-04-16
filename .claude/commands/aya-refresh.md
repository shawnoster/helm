---
name: aya-refresh
description: >
  Refresh aya to the latest version after pulling updates. Uninstalls the current
  installation and reinstalls from GitHub, with verification. Invoke when the user
  says "refresh aya", "reinstall aya", "update aya globally", or "aya refresh".
---

# Refresh aya

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

3. Re-install hooks (picks up any format changes):
```bash
aya schedule install
```

4. Verify installation:
```bash
which aya && aya status -f json | jq '.systems.ok'
```

If the final command returns `true`, installation succeeded. If it returns `false` or errors, the installation failed — report the error to the user.

Do not continue if any step fails.
