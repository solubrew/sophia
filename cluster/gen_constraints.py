"""Generate a pip constraints file pinning the SB-stack deps to the
versions installed in /install. Used during the sophia image build.

Why: pip would otherwise try to fetch `kahndor` (and friends) from
PyPI, where the public stub is version 0.0.0 and is the wrong
package entirely. With these constraints, the . install step
silently uses the SB wheels we put in /install earlier in the
Dockerfile.
"""
import importlib.metadata as md
from pathlib import Path

PACKAGES = (
    "kahndor",
    "subtrix",
    "pycurity",
    "ladyol",
    "goalt",
    "mindot",
    "glain",
    "thingery",
    "squirl",
    "nchantrs",
    "worldbridgeserver",
    "nchantdoffice",
    "nchantdaxn",
    "pyffice",
    "shieldface",
    "tradebot",
    "pyside6pandas",
    "falcon",
    "micromole",
)


def main():
    out = []
    for pkg in PACKAGES:
        try:
            v = md.version(pkg)
            out.append(f"{pkg}=={v}")
        except md.PackageNotFoundError:
            pass
    Path("/install/sb-constraints.txt").write_text("\n".join(out))
    print(f"wrote {len(out)} constraints to /install/sb-constraints.txt")


if __name__ == "__main__":
    main()