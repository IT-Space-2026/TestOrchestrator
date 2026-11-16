"""Assembled-package gate: offline deps + vendored reporting-labs on the
extracted artifact."""
import subprocess,sys,tempfile
from pathlib import Path
def main():
    pkg=Path(sys.argv[1]);py=pkg/"3rdparty/python-runtime/python/bin/python3"
    with tempfile.TemporaryDirectory() as td:
        v=Path(td)/"v"
        subprocess.run([str(py),"-m","venv",str(v)],check=True)
        subprocess.run([str(v/"bin/pip"),"install","--no-index","--no-cache-dir",
            "--find-links",str(pkg/"3rdparty/python-wheels"),
            "-r",str(pkg/"requirements.txt")],check=True)
        subprocess.run([str(v/"bin/pip"),"check"],check=True)
        # reporting-labs is vendored at the package root (./reporting_labs) —
        # import it with cwd=pkg exactly as the running app does.
        r=subprocess.run([str(v/"bin/python"),"-c",
            "import reporting_labs,os;"
            "assert 'site-packages' not in reporting_labs.__file__, reporting_labs.__file__;"
            "print(reporting_labs.__version__)"],
            capture_output=True,text=True,cwd=pkg)
        assert r.returncode==0,f"reporting_labs import failed: {r.stderr}"
        assert r.stdout.strip()=="0.1.14",r.stdout
        print("GATE OK")
if __name__=="__main__":
    main()
