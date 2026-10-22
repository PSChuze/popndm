"""Build the pop'n Server Setup program: python packaging/build_setup.py [--version v1.2.3]

One file made with PyInstaller (pip install pyinstaller==6.11.1), written to
dist/. It carries this repository's committed files as server.zip and unpacks
them into the host's server folder on its first run, so a host needs only the
program and Docker. Build each platform's program on that platform.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"win32": "Popn-Server-Setup", "darwin": "Popn-Server-Setup", "linux": "Popn-Server-Setup-linux-x86_64"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="dev", help="the release tag the program reports and updates from")
    args = ap.parse_args()
    work = os.path.join(HERE, "build", "setup")
    os.makedirs(work, exist_ok=True)
    archive = os.path.join(work, "server.zip")
    subprocess.run(["git", "-C", HERE, "archive", "--format=zip", "--prefix=popndm/", "-o", archive, "HEAD"],
                   check=True)
    version = os.path.join(work, "server-version.txt")
    with open(version, "w", encoding="utf-8") as f:
        f.write(args.version + "\n")
    name = NAMES.get(sys.platform, NAMES["linux"])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                    "--name", name, "--distpath", os.path.join(HERE, "dist"), "--workpath", os.path.join(work, "work"),
                    "--specpath", work, "--add-data", archive + os.pathsep + ".",
                    "--add-data", version + os.pathsep + ".", "--paths", HERE,
                    os.path.join(HERE, "setup.py")], check=True, cwd=HERE)
    print(os.path.join(HERE, "dist", name + (".exe" if sys.platform == "win32" else "")))


if __name__ == "__main__":
    main()
