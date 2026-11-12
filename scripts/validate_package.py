"""Assembled-package gate: offline deps + bundled Allure on extracted artifact."""
import json,subprocess,sys,tempfile
from pathlib import Path
def main():
    pkg=Path(sys.argv[1]);py=pkg/"3rdparty/python-runtime/python/bin/python3"
    node=next(pkg.glob("3rdparty/nodejs/node-*/bin/node"))
    allure=next(pkg.glob("3rdparty/allure3/node_modules/allure/cli.js"))
    with tempfile.TemporaryDirectory() as td:
        v=Path(td)/"v"
        subprocess.run([str(py),"-m","venv",str(v)],check=True)
        subprocess.run([str(v/"bin/pip"),"install","--no-index","--no-cache-dir",
            "--find-links",str(pkg/"3rdparty/python-wheels"),
            "-r",str(pkg/"requirements.txt")],check=True)
        subprocess.run([str(v/"bin/pip"),"check"],check=True)
        r=subprocess.run([str(node),str(allure),"--version"],
            capture_output=True,text=True)
        assert r.returncode==0,f"allure --version failed: {r.stderr}"
        res=Path(td)/"res";res.mkdir()
        (res/"t.json").write_text(json.dumps({"name":"T","fullName":"T",
            "status":"passed","labels":[{"name":"suite","value":"S"}]}))
        g=subprocess.run([str(node),str(allure),"generate",str(res),
            "-o",str(Path(td)/"report"),"--clean"],
            capture_output=True,text=True)
        assert g.returncode==0,f"generate failed: {g.stderr[:300]}"
        assert (Path(td)/"report").exists(),"no report dir"
        print("GATE OK")
if __name__=="__main__":
    main()
