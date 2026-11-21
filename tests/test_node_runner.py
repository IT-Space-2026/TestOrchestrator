

def test_build_args_playwright_parallel():
    from testorchestrator.adapters import nodefw
    a = nodefw.build_args("playwright", workers=4)
    assert a[a.index("--workers") + 1] == "4"
    a = nodefw.build_args("playwright", shard="2/4")
    assert a[a.index("--shard") + 1] == "2/4"
    assert "--workers" not in nodefw.build_args("playwright")
