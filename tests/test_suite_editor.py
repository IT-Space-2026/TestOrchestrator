"""Suite editor: binding validation, ordering, preservation."""

from testorchestrator import dispatch, gitsync
from testorchestrator.extensions import db
from testorchestrator.execution import (ExecutionState, GitBinding,
                                        Project, Suite, SuiteEntry)
from tests.test_discovery import _base_files, _fixture


def _setup(app, tmp_path, n_bindings=1):
    wt = _fixture(tmp_path / "wt", _base_files())
    p = Project(name="P"); db.session.add(p); db.session.flush()
    bindings = []
    for i in range(n_bindings):
        w = _fixture(tmp_path / f"wt{i}", _base_files())
        b = GitBinding(project_id=p.id, name=f"b{i}", url="x",
                       worktree_path=str(w), status="synced")
        db.session.add(b); db.session.flush()
        bindings.append(b)
    db.session.commit()
    return p, bindings


def _post_create(client, project_id, name, refs, csrf):
    from werkzeug.datastructures import MultiDict
    data = MultiDict([("name", name), ("description", "d"),
                      ("csrf_token", csrf)] +
                     [("case_refs", r) for r in refs])
    return client.post(f"/projects/{project_id}/suites", data=data,
                       follow_redirects=True)


def _csrf(client, url):
    import re
    html = client.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def _case_paths(binding):
    return set(gitsync.list_test_cases(binding))


def test_create_preserves_order(client, app, tmp_path, login_as):
    login_as("admin")
    with app.app_context():
        p, bs = _setup(app, tmp_path)
        cases = sorted(_case_paths(bs[0]))[:3]
        # order deliberately reversed
        refs = list(reversed(cases))
        tok = _csrf(client, f"/projects/{p.id}/suites/new")
        _post_create(client, p.id, "S", refs, tok)
        s = Suite.query.filter_by(name="S").first()
        assert s is not None
        assert [e.case_ref for e in
                sorted(s.entries, key=lambda e: e.order)] == refs


def test_foreign_case_ref_rejected(client, app, tmp_path, login_as):
    login_as("admin")
    with app.app_context():
        p, bs = _setup(app, tmp_path)
        tok = _csrf(client, f"/projects/{p.id}/suites/new")
        r = _post_create(client, p.id, "Bad",
                         ["Test Cases/FAKE/x.tc"], tok)
        assert Suite.query.filter_by(name="Bad").first() is None
        html = r.get_data(as_text=True)
        assert "FAKE" in html or "not a valid choice" in html \
            or "not in the selected binding" in html


def test_multi_binding_validates_per_binding(client, app, tmp_path, login_as):
    login_as("admin")
    """Cases from binding A must not validate against binding B —
    a ref that exists in A but is posted under B rejects."""
    with app.app_context():
        p, bs = _setup(app, tmp_path, n_bindings=2)
        # wt0 and wt1 have the same content — give wt1 a UNIQUE case
        import pathlib
        extra = pathlib.Path(bs[1].worktree_path) / \
            "Test Cases/API/only-in-b1.tc"
        extra.write_text("<TestCaseEntity/>")
        tok = _csrf(client, f"/projects/{p.id}/suites/new")
        # post a case only in b1 with no binding_id → defaults to b0 →
        # reject
        _post_create(client, p.id, "Cross",
                     ["Test Cases/API/only-in-b1.tc"], tok)
        assert Suite.query.filter_by(name="Cross").first() is None


def test_edit_preserves_and_reorders(client, app, tmp_path, login_as):
    login_as("admin")
    with app.app_context():
        p, bs = _setup(app, tmp_path)
        cases = sorted(_case_paths(bs[0]))[:2]
        tok = _csrf(client, f"/projects/{p.id}/suites/new")
        _post_create(client, p.id, "E", cases, tok)
        s = Suite.query.filter_by(name="E").first()
        # edit: swap order via save
        swapped = list(reversed(cases))
        tok = _csrf(client, f"/suites/{s.id}/edit")
        from werkzeug.datastructures import MultiDict
        client.post(f"/suites/{s.id}/save",
                    data=MultiDict(
                        [("name", "E"), ("description", ""),
                         ("csrf_token", tok)] +
                        [("case_refs", c) for c in swapped]),
                    follow_redirects=True)
        db.session.refresh(s)
        assert [e.case_ref for e in
                sorted(s.entries, key=lambda e: e.order)] == swapped
        assert s.version == 2


def test_validation_failure_preserves_selection(client, app, tmp_path, login_as):
    login_as("admin")
    with app.app_context():
        p, bs = _setup(app, tmp_path)
        cases = sorted(_case_paths(bs[0]))[:1]
        tok = _csrf(client, f"/projects/{p.id}/suites/new")
        _post_create(client, p.id, "V", cases, tok)
        s = Suite.query.filter_by(name="V").first()
        tok = _csrf(client, f"/suites/{s.id}/edit")
        r = client.post(f"/suites/{s.id}/save",
                        data={"name": "V", "csrf_token": tok,
                              "case_refs": "Test Cases/FAKE/x.tc"},
                        follow_redirects=False)
        # re-rendered editor, not a redirect that drops input
        html = r.get_data(as_text=True)
        assert "not in the binding" in html
        assert "FAKE" in html   # user's selection retained
