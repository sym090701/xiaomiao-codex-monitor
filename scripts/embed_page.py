from pathlib import Path
Import("env")
root = Path(env.subst("$PROJECT_DIR"))
page = (root / "data/index.html").read_text()
assert ')XMPAGE"' not in page
(root / "include/xiaomiao_page.h").write_text(
    '#pragma once\nstatic const char XM_PAGE[] PROGMEM = R"XMPAGE(' + page + ')XMPAGE";\n'
)
