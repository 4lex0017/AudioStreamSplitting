import subprocess
import platform
import pathlib
import os

file = pathlib.Path(__file__)

if platform.system() == "Windows":
    subprocess.call(
        f"sphinx-apidoc -d 2 -f -e -l -P -o .\\docs\\backend .\\src\\backend\\ /*/tests/*"
    )
    make = file.parent.joinpath("backend", "make.bat").resolve().as_posix()
else:
    subprocess.call(
        f"sphinx-apidoc -d 2 -f -e -l -P -o docs/backend src/backend/ /*/tests/*"
    )
    make = "make"

os.remove(file.parent.joinpath("backend", "modules.rst").resolve().as_posix())
subprocess.call(f"{make} clean")
subprocess.call(f"{make} html")
subprocess.call(f"{make} markdown")
