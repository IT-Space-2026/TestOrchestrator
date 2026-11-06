"""Bounded publishing write-probe: create ONE marker ref, read it back,
delete it. Reports cleanup honestly — never 'verified' while a probe
ref may remain. Executes only via an explicit authorized action."""
import uuid

PROBE_PREFIX = "refs/testorchestrator/probe-"


def probe_write_access(repo) -> dict:
    """repo: duck-typed {write()->bool, read()->str|None, delete()->bool}.
    Real implementation binds the git backend + credential scope; the
    contract is mock-tested here."""
    ref = PROBE_PREFIX + uuid.uuid4().hex[:12]
    token = uuid.uuid4().hex
    try:
        if not repo.write(ref, token):
            return {"status": "failed", "detail": "write rejected",
                    "cleanup": "nothing written"}
        if repo.read(ref) != token:
            # created but unreadable/mismatched -> still delete
            repo.delete(ref)
            return {"status": "failed",
                    "detail": "marker content mismatch after write",
                    "cleanup": "delete attempted"}
        if not repo.delete(ref):
            return {"status": "failed",
                    "detail": "probe ref deletion failed",
                    "cleanup": "probe ref may remain"}
        return {"status": "verified",
                "detail": "write + read-back + cleanup all passed",
                "cleanup": "complete"}
    except Exception as e:
        return {"status": "failed", "detail": f"probe error: {e}",
                "cleanup": "state unknown — inspect repo"}


def _test_iface(repo):  # test shim: FakeRepo.write() takes no ref arg
    class W:
        def write(self, ref, token): return repo.write(token)
        def read(self, ref): return repo.read()
        def delete(self, ref): return repo.delete()
    return W()
