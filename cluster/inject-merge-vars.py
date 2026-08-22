#!/usr/bin/env python3
"""Helper: make a temp copy of docker-compose.cluster.yml with the
sophia container's SOPHIA_MERGE_* env vars injected, so the seed-keys
flow can pass vars into the container despite v1 docker-compose's
limitations:

  1) v1 doesn't auto-load .env
  2) v1 doesn't inherit the parent shell's env
  3) v1 doesn't understand the v2 ${VAR:?error} syntax — which the
     open-webui block uses. We downgrade it to plain ${VAR} so the
     v1 parser is happy.

The shell wrapper (cluster/seed-keys.sh) does the heredoc-free
spawn of this script: `./inject-merge-vars.py <src_yaml> <dst_yaml>
<vault_path> <passphrase>`.
"""
import sys


def main():
    if len(sys.argv) not in (5, 6):
        print(
            "usage: inject-merge-vars.py <src_yaml> <dst_yaml> <vault> "
            "<pass> [<allowlist_csv>]",
            file=sys.stderr,
        )
        sys.exit(64)
    src_yaml, dst_yaml, vault, passphrase = sys.argv[1:5]
    allowlist = sys.argv[5] if len(sys.argv) == 6 else ""

    with open(src_yaml) as f:
        text = f.read()

    # Downgrade v2's ${VAR:?error} syntax to v1-compatible ${VAR}.
    # We use a placeholder string so we don't accidentally rewrite
    # anything else.
    placeholder = "___OPENWEBUI_REQUESTOR_KEY___"
    v2_pattern = (
        "${SOPHIA_REQUESTOR_KEY:?set SOPHIA_REQUESTOR_KEY in .env "
        "to the requestor key you create}"
    )
    new_text = text.replace(v2_pattern, placeholder).replace(
        placeholder, "${SOPHIA_REQUESTOR_KEY}"
    )

    # Inject SOPHIA_MERGE_* into the sophia service's environment,
    # right after the PORT: "8765" line. The first occurrence of
    # PORT in the file is on the sophia service (sophia is defined
    # first in the YAML).
    merge_vars = (
        f'      SOPHIA_MERGE_FROM_PYCURITY_PATH: "{vault}"\n'
        f'      SOPHIA_MERGE_PASSPHRASE: "{passphrase}"\n'
    )
    if allowlist:
        # Allowlist is a CSV of vault service names. We wrap in YAML
        # double-quotes; backslash-escape any embedded backslashes
        # and double-quotes so the YAML stays valid.
        escaped = allowlist.replace("\\", "\\\\").replace('"', '\\"')
        merge_vars += f'      SOPHIA_MERGE_ALLOWLIST: "{escaped}"\n'
    new_text = new_text.replace(
        '      PORT: "8765"',
        '      PORT: "8765"\n' + merge_vars,
        1,
    )

    with open(dst_yaml, "w") as f:
        f.write(new_text)


if __name__ == "__main__":
    main()