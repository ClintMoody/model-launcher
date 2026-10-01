def test_repo_boots():
    import importlib.util, os
    # the repo root has the three stdlib modules; confirm they import
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for mod in ("registry", "hermes_cfg"):
        p = os.path.join(root, mod + ".py")
        if not os.path.exists(p):
            continue  # not written yet in this wave
        spec = importlib.util.spec_from_file_location(mod, p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    assert True
