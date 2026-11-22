"""Robustness test suite for the Blast-Radius test selector.

Constructs synthetic unified diffs against the pinned sqlglot commit (using
only map.pkl.gz — no dependency on sample/) covering all file classes and
adversarial edge cases. Asserts:
- Selector never crashes
- Output always passes SCORING.md validation (JSON shape, valid IDs, no dupes, <=8 workers)
- Risky / broad classes always return non-empty conservative selections
- Docs-only patches return empty workers []
- Comment-like traps (inside string literals, docstrings, coding cookies,
  line-continuation breaks, indentation shifts) are NOT treated as harmless.
"""

from __future__ import annotations

import difflib
import json
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import packer  # noqa: E402
import patch_parser as pp  # noqa: E402
import score  # noqa: E402
import selector as sel  # noqa: E402


def _make_unified_diff(rel_path: str, old_text: str, new_text: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )
    header = f"diff --git a/{rel_path} b/{rel_path}\n"
    return header + "".join(diff_lines)


class TestRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.map_data = sel.load_map(ROOT / "map.pkl.gz")
        cls.test_ids = cls.map_data["test_ids"]
        cls.tests_list = [
            {
                "number": i,
                "id": tid,
                "file": tid.split("::", 1)[0],
                "seconds": 0.050 if i % 50 != 0 else 2.500,
            }
            for i, tid in enumerate(cls.test_ids)
        ]
        cls.selector = sel.BlastRadiusSelector(cls.map_data, cls.tests_list)
        cls.conservative_selector = sel.BlastRadiusSelector(
            cls.map_data, cls.tests_list, config={"p_ast_identical": 0.002}
        )

    def _base_text(self, rel: str) -> str:
        return zlib.decompress(self.map_data["base_sources"][rel]).decode("utf-8").replace("\r\n", "\n")

    def _validate_line(self, commit_id: str, res: dict) -> list[int]:
        line = (json.dumps(res, separators=(",", ":")) + "\n").encode("utf-8")
        ev = score.evaluate_run(line, self.tests_list, {commit_id: {"failing": []}})
        self.assertTrue(ev["ok"], f"Invalid output for {commit_id}: {ev}")
        self.assertLessEqual(len(res["workers"]), 8)
        flat = [n for w in res["workers"] for n in w]
        self.assertEqual(len(flat), len(set(flat)))
        return flat

    # --- 1. Docs-only -> empty ---
    def test_readme_docs_only(self) -> None:
        patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,3 +1,4 @@\n"
            " # SQLGlot\n"
            "+Updated documentation line.\n"
        )
        res = self.selector.select_and_pack("docs_readme", patch)
        flat = self._validate_line("docs_readme", res)
        self.assertEqual(flat, [])

    # --- 2. Comment-only edit (and comment-like traps!) ---
    def test_pure_comment_edit_and_conservative_mode(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        new = "# Benign top comment\n" + old
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("pure_comment", patch)
        self._validate_line("pure_comment", res)
        # Under conservative AST-identical config, selects non-empty module tests
        res_cons = self.conservative_selector.select_and_pack("pure_comment_cons", patch)
        flat_cons = self._validate_line("pure_comment_cons", res_cons)
        self.assertGreater(len(flat_cons), 0)

    def test_comment_inside_string_and_docstring_safety(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        # 1. Comment-like text inside an assigned string literal is NEVER ignored
        new_str = old.replace('CLASS = "c"', 'CLASS = "# not a comment"', 1)
        patch_str = _make_unified_diff(rel, old, new_str)
        res_str = self.selector.select_and_pack("string_comment_trap", patch_str)
        flat_str = self._validate_line("string_comment_trap", res_str)
        self.assertGreater(len(flat_str), 0)

        # 2. Docstring edits are NOT ast_identical and ALWAYS keep tests/test_docs.py when present
        idx = old.find('"""')
        self.assertNotEqual(idx, -1)
        new_doc = old[: idx + 3] + "\n# changed docstring text\n" + old[idx + 3 :]
        ana = pp.analyze_python_change(old.encode("utf-8"), new_doc.encode("utf-8"))
        self.assertFalse(ana.ast_identical)
        self.assertTrue(pp.is_docstring_only_change(ana.old_tree, ana.new_tree))
        docs_num = len(self.tests_list)
        tests_with_docs = [
            *self.tests_list,
            {"number": docs_num, "id": "tests/test_docs.py::test_doctests", "file": "tests/test_docs.py", "seconds": 0.5},
        ]
        sel_docs = sel.BlastRadiusSelector(self.map_data, tests_with_docs)
        patch_doc = _make_unified_diff(rel, old, new_doc)
        nums_doc = sel_docs.select_test_numbers(patch_doc)
        self.assertIn(docs_num, nums_doc)

    def test_coding_cookie_comment_triggers_broad(self) -> None:
        rel = "sqlglot/dialects/dialect.py"
        old = self._base_text(rel)
        new = "# -*- coding: ascii -*-\n" + old
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("coding_cookie", patch)
        flat = self._validate_line("coding_cookie", res)
        self.assertGreater(len(flat), 50)

    # --- 3. New function / removed function / constant / decorator / import ---
    def test_new_public_function(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        new = old + "\ndef brand_new_helper(x):\n    return x + 1\n"
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("new_func", patch)
        flat = self._validate_line("new_func", res)
        self.assertGreater(len(flat), 0)

    def test_removed_public_function(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        new = old.replace("def load(", "def _renamed_load(", 1)
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("removed_func", patch)
        flat = self._validate_line("removed_func", res)
        self.assertGreater(len(flat), 5)

    def test_constant_change(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        new = old.replace('CLASS = "c"', 'CLASS = "cls"', 1)
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("const_change", patch)
        flat = self._validate_line("const_change", res)
        self.assertGreater(len(flat), 0)

    def test_decorator_change(self) -> None:
        rel = "sqlglot/executor/context.py"
        old = self._base_text(rel)
        new = old.replace("@property\n    def table", "@staticmethod\n    def table", 1)
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("decorator_change", patch)
        flat = self._validate_line("decorator_change", res)
        self.assertGreater(len(flat), 10)

    def test_import_added(self) -> None:
        rel = "sqlglot/serde.py"
        old = self._base_text(rel)
        new = "import nonexistent_module_xyz\n" + old
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("import_added", patch)
        flat = self._validate_line("import_added", res)
        self.assertGreater(len(flat), 5)

    # --- 4. Syntax-breaking edit ---
    def test_syntax_breaking_edit(self) -> None:
        rel = "sqlglot/generators/clickhouse.py"
        old = self._base_text(rel)
        new = old.replace("class ClickHouseGenerator", "class ClickHouseGenerator(((", 1)
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("syntax_err", patch)
        flat = self._validate_line("syntax_err", res)
        self.assertGreater(len(flat), 50)

    # --- 5. New file, deleted file, rename ---
    def test_new_file_in_sqlglot(self) -> None:
        patch = (
            "diff --git a/sqlglot/dialects/newdialect.py b/sqlglot/dialects/newdialect.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/sqlglot/dialects/newdialect.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+class NewDialect:\n"
            "+    pass\n"
        )
        res = self.selector.select_and_pack("new_file", patch)
        flat = self._validate_line("new_file", res)
        self.assertGreater(len(flat), 0)

    def test_deleted_file(self) -> None:
        patch = (
            "diff --git a/sqlglot/serde.py b/sqlglot/serde.py\n"
            "deleted file mode 100644\n"
            "--- a/sqlglot/serde.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-from __future__ import annotations\n"
        )
        res = self.selector.select_and_pack("deleted_file", patch)
        flat = self._validate_line("deleted_file", res)
        self.assertGreater(len(flat), 5)

    def test_rename_file(self) -> None:
        patch = (
            "diff --git a/sqlglot/serde.py b/sqlglot/serde_v2.py\n"
            "similarity index 100%\n"
            "rename from sqlglot/serde.py\n"
            "rename to sqlglot/serde_v2.py\n"
        )
        res = self.selector.select_and_pack("rename_file", patch)
        flat = self._validate_line("rename_file", res)
        self.assertGreater(len(flat), 5)

    # --- 6. Test-file edit & fixture edit ---
    def test_test_file_edit(self) -> None:
        rel = "tests/test_serde.py"
        old = self._base_text(rel)
        new = old.replace("self.assertEqual(", "self.assertNotEqual(", 1)
        patch = _make_unified_diff(rel, old, new)
        res = self.selector.select_and_pack("test_edit", patch)
        flat = self._validate_line("test_edit", res)
        self.assertGreater(len(flat), 0)
        self.assertLessEqual(len(flat), 10)

    def test_fixture_edit_known_and_unknown(self) -> None:
        patch_known = (
            "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\n"
            "--- a/tests/fixtures/optimizer/simplify.sql\n"
            "+++ b/tests/fixtures/optimizer/simplify.sql\n"
            "@@ -1,3 +1,3 @@\n"
            "-1 + 1;\n"
            "+1 + 2;\n"
        )
        res1 = self.selector.select_and_pack("fixture_known", patch_known)
        flat1 = self._validate_line("fixture_known", res1)
        self.assertEqual(len(flat1), 1)

        patch_unknown = (
            "diff --git a/tests/fixtures/optimizer/brand_new_fixture.sql b/tests/fixtures/optimizer/brand_new_fixture.sql\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/fixtures/optimizer/brand_new_fixture.sql\n"
            "@@ -0,0 +1,2 @@\n"
            "+SELECT 1;\n"
        )
        res2 = self.selector.select_and_pack("fixture_unknown", patch_unknown)
        flat2 = self._validate_line("fixture_unknown", res2)
        self.assertGreater(len(flat2), 0)

    # --- 7. Config, conftest, root __init__, Rust/tokenizer, unknown file ---
    def test_pyproject_and_conftest_and_rust_and_root_init(self) -> None:
        for cid, path in [
            ("pyproject", "pyproject.toml"),
            ("conftest", "tests/conftest.py"),
            ("rust_tok", "sqlglotrs/src/lib.rs"),
            ("tok_core", "sqlglot/tokenizer_core.py"),
            ("unknown_ext", "mystery_config.xyz"),
        ]:
            patch = (
                f"diff --git a/{path} b/{path}\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                "@@ -1,2 +1,2 @@\n"
                "-old\n"
                "+new\n"
            )
            res = self.selector.select_and_pack(cid, patch)
            flat = self._validate_line(cid, res)
            self.assertEqual(len(flat), len(self.tests_list), f"Expected broad fallback for {cid}")

        rel = "sqlglot/__init__.py"
        old = self._base_text(rel)
        new = old + "\nEXTRA_EXPORT = 1\n"
        res_init = self.selector.select_and_pack("root_init", _make_unified_diff(rel, old, new))
        flat_init = self._validate_line("root_init", res_init)
        self.assertEqual(len(flat_init), len(self.tests_list))

    # --- 8. Binary patch, empty patch, malformed patch ---
    def test_binary_empty_and_malformed_patches(self) -> None:
        binary_patch = (
            "diff --git a/sqlglot/data.bin b/sqlglot/data.bin\n"
            "index 1234567..89abcdef 100644\n"
            "Binary files a/sqlglot/data.bin and b/sqlglot/data.bin differ\n"
        )
        res_bin = self.selector.select_and_pack("bin_patch", binary_patch)
        flat_bin = self._validate_line("bin_patch", res_bin)
        self.assertGreater(len(flat_bin), 0)

        res_empty = self.selector.select_and_pack("empty_patch", "")
        flat_empty = self._validate_line("empty_patch", res_empty)
        self.assertEqual(flat_empty, [])

        for cid, bad in [
            ("malformed_text", "not a diff at all!!!\nrandom garbage\n"),
            ("malformed_hunk", "diff --git a/sqlglot/serde.py b/sqlglot/serde.py\n@@ BAD HUNK @@\n+x\n"),
        ]:
            res_bad = self.selector.select_and_pack(cid, bad)
            flat_bad = self._validate_line(cid, res_bad)
            self.assertEqual(len(flat_bad), len(self.tests_list))

    # --- 9. Packer invariants ---
    def test_packer_invariants(self) -> None:
        durs = {i: (i + 1) * 100 for i in range(25)}
        ws = packer.pack_tests(list(durs.keys()), durs)
        self.assertLessEqual(len(ws), 8)
        self.assertTrue(all(len(w) > 0 for w in ws))
        flat = [n for w in ws for n in w]
        self.assertEqual(sorted(flat), list(range(25)))

    # --- 10. Phase 3 commit-level equivalence unit & trap tests ---
    def test_phase3_local_rename_and_traps(self) -> None:
        import ast

        # Safe local rename
        self.assertTrue(
            pp.is_safe_local_rename(
                ast.parse("def f(a):\n    x = a + 1\n    return x * 2\n"),
                ast.parse("def f(a):\n    y = a + 1\n    return y * 2\n"),
            )
        )
        # Parameter rename trap -> MUST reject
        self.assertFalse(
            pp.is_safe_local_rename(
                ast.parse("def f(a):\n    return a + 1\n"),
                ast.parse("def f(b):\n    return b + 1\n"),
            )
        )
        # f-string debug trap `f"{x=}"` -> MUST reject
        self.assertFalse(
            pp.is_safe_local_rename(
                ast.parse('def f(a):\n    x = a\n    return f"{x=}"\n'),
                ast.parse('def f(a):\n    y = a\n    return f"{y=}"\n'),
            )
        )
        # locals() trap -> MUST reject
        self.assertFalse(
            pp.is_safe_local_rename(
                ast.parse("def f(a):\n    x = a\n    return locals()\n"),
                ast.parse("def f(a):\n    y = a\n    return locals()\n"),
            )
        )
        # Shadowing a free/builtin name trap -> MUST reject
        self.assertFalse(
            pp.is_safe_local_rename(
                ast.parse("def f(a):\n    x = len(a)\n    return x\n"),
                ast.parse("def f(a):\n    len = len(a)\n    return len\n"),
            )
        )
        # Non-bijection collision trap -> MUST reject
        self.assertFalse(
            pp.is_safe_local_rename(
                ast.parse("def f(a):\n    x = a\n    y = x + 1\n    return x + y\n"),
                ast.parse("def f(a):\n    y = a\n    y = y + 1\n    return y + y\n"),
            )
        )
        # End-to-end: sqlglot/typing/ local rename MUST NOT drop test_typing.py
        rel = "sqlglot/typing/snowflake.py"
        old = self._base_text(rel)
        new = old.replace("last_type = None", "final_type = None", 1).replace("last_type", "final_type")
        res = self.selector.select_and_pack("typing_rename", _make_unified_diff(rel, old, new))
        flat = self._validate_line("typing_rename", res)
        self.assertGreater(len(flat), 0)

    def test_phase3_added_public_func_narrowing_and_traps(self) -> None:
        rel = "sqlglot/helper.py"
        old = self._base_text(rel)
        # Unreferenced pure added public func -> narrowed to meta tests only (<= 15 tests)
        new_safe = old + "\ndef zzz_brand_new_unreferenced_func(x: int = 1) -> int:\n    return x + 1\n"
        flat_safe = self._validate_line(
            "add_safe_func",
            self.selector.select_and_pack("add_safe_func", _make_unified_diff(rel, old, new_safe)),
        )
        self.assertGreater(len(flat_safe), 0)
        self.assertLessEqual(len(flat_safe), 15)

        # Decorated added public func -> broad importers (> 500 tests)
        new_dec = old + "\n@some_decorator\ndef zzz_decorated_func():\n    return 1\n"
        flat_dec = self._validate_line(
            "add_dec_func",
            self.selector.select_and_pack("add_dec_func", _make_unified_diff(rel, old, new_dec)),
        )
        self.assertGreater(len(flat_dec), 500)

        # Added public class -> broad importers (subclasses() / inspect.getmembers safety!)
        new_cls = old + "\nclass ZzzNewPublicClass:\n    pass\n"
        flat_cls = self._validate_line(
            "add_pub_cls",
            self.selector.select_and_pack("add_pub_cls", _make_unified_diff(rel, old, new_cls)),
        )
        self.assertGreater(len(flat_cls), 500)

    # --- 11. Unseen edge cases: cross-category renames, hunk boundaries, CRLF/tabs/EOF, sqlglotc/dotfiles ---
    def test_cross_category_renames_never_drop_source_or_fixture(self) -> None:
        renames = [
            ("ren_sg_docs", "sqlglot/anonymize.py", "docs/anonymize.md", 20),
            ("ren_sg_bench", "sqlglot/lineage.py", "benchmarks/lineage.py", 50),
            ("ren_sg_sgc", "sqlglot/anonymize.py", "sqlglotc/anonymize.py", len(self.tests_list)),
            ("ren_fix_docs", "tests/fixtures/jsonpath/cts.json", "docs/cts.json", 1),
            ("ren_test_docs", "tests/test_anonymize.py", "docs/test_anonymize.md", 20),
            ("ren_docs_sg", "README.md", "sqlglot/from_readme.py", len(self.tests_list)),
        ]
        for cid, old_p, new_p, min_expected in renames:
            patch = (
                f"diff --git a/{old_p} b/{new_p}\n"
                "similarity index 100%\n"
                f"rename from {old_p}\n"
                f"rename to {new_p}\n"
            )
            flat = self._validate_line(cid, self.selector.select_and_pack(cid, patch))
            self.assertGreaterEqual(len(flat), min_expected, f"Too few tests for {cid}: {len(flat)}")

    def test_hunk_boundary_and_formatting_edge_cases(self) -> None:
        # 1. Deleted `-- ...` (`--- ...`) followed by added `++ ...` (`+++ ...`) inside active hunk
        triple_dash = (
            "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\n"
            "--- a/tests/fixtures/optimizer/simplify.sql\n"
            "+++ b/tests/fixtures/optimizer/simplify.sql\n"
            "@@ -1,5 +1,5 @@\n"
            " --------------------------------------\n"
            "--- Conditions\n"
            "+++ BROKEN SQL LINE\n"
            " --------------------------------------\n"
            " x AND x;\n"
            " x AND TRUE;\n"
        )
        parsed = pp.parse_unified_diff(triple_dash)
        self.assertFalse(parsed.parse_error)
        self.assertEqual(len(parsed.files), 1)
        self.assertEqual(parsed.files[0].path, "tests/fixtures/optimizer/simplify.sql")
        flat_td = self._validate_line("triple_dash", self.selector.select_and_pack("triple_dash", triple_dash))
        self.assertEqual(len(flat_td), 1)

        # 2. CRLF patch, tabs in header timestamps, mode change + code, no trailing newline
        rel = "sqlglot/anonymize.py"
        old = self._base_text(rel)
        new_bug = old.replace("dialect = Dialect.get_or_raise(dialect)", "raise RuntimeError('bug')", 1)
        base_diff = _make_unified_diff(rel, old, new_bug)

        # CRLF endings
        flat_crlf = self._validate_line("crlf", self.selector.select_and_pack("crlf", base_diff.replace("\n", "\r\n")))
        self.assertGreaterEqual(len(flat_crlf), 20)

        # Mode change + code hunk
        mode_diff = base_diff.replace(f"--- a/{rel}\n", f"old mode 100644\nnew mode 100755\n--- a/{rel}\n", 1)
        flat_mode = self._validate_line("mode_code", self.selector.select_and_pack("mode_code", mode_diff))
        self.assertEqual(flat_crlf, flat_mode)

        # Tabbed timestamps in --- / +++ headers
        tab_diff = base_diff.replace(f"--- a/{rel}\n", f"--- a/{rel}\t2026-01-01\n", 1).replace(
            f"+++ b/{rel}\n", f"+++ b/{rel}\t2026-01-01\n", 1
        )
        flat_tab = self._validate_line("tab_hdr", self.selector.select_and_pack("tab_hdr", tab_diff))
        self.assertEqual(flat_crlf, flat_tab)

        # No trailing newline at EOF
        new_nonl = old.replace('return "".join(result)', 'return "BAD"', 1).rstrip("\n")
        diff_nonl = _make_unified_diff(rel, old, new_nonl)
        if not diff_nonl.endswith("\\ No newline at end of file\n"):
            diff_nonl += "\\ No newline at end of file\n"
        flat_nonl = self._validate_line("nonl", self.selector.select_and_pack("nonl", diff_nonl))
        self.assertGreaterEqual(len(flat_nonl), 10)

    def test_sqlglotc_strict_whitelist_and_dotfiles(self) -> None:
        # Allowed packaging files -> test_sqlglotc.py only
        for rel in ("sqlglotc/setup.py", "sqlglotc/pyproject.toml", "sqlglotc/MANIFEST.in"):
            p = f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            flat = self._validate_line(rel, self.selector.select_and_pack(rel, p))
            self.assertEqual(len(flat), 3, f"Expected only 3 test_sqlglotc tests for {rel}")

        # Any other sqlglotc/ file, deletion, dotfile CI, integration-tests, new subpkg, non-fixture tests/ -> ALL tests
        broad_patches = [
            ("sgc_new_py", "diff --git a/sqlglotc/extra.py b/sqlglotc/extra.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglotc/extra.py\n@@ -0,0 +1,1 @@\n+x = 1\n"),
            ("sgc_del", "diff --git a/sqlglotc/setup.py b/sqlglotc/setup.py\ndeleted file mode 100644\n--- a/sqlglotc/setup.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"),
            ("sgc_hdr", "diff --git a/sqlglotc/ext.h b/sqlglotc/ext.h\n--- a/sqlglotc/ext.h\n+++ b/sqlglotc/ext.h\n@@ -1,1 +1,1 @@\n-a\n+b\n"),
            ("gh_wf", "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1,1 +1,1 @@\n-a\n+b\n"),
            ("pre_commit", "diff --git a/.pre-commit-config.yaml b/.pre-commit-config.yaml\n--- a/.pre-commit-config.yaml\n+++ b/.pre-commit-config.yaml\n@@ -1,1 +1,1 @@\n-a\n+b\n"),
            ("integ_md", "diff --git a/sqlglot-integration-tests/README.md b/sqlglot-integration-tests/README.md\n--- a/sqlglot-integration-tests/README.md\n+++ b/sqlglot-integration-tests/README.md\n@@ -1,1 +1,1 @@\n-a\n+b\n"),
            ("new_subpkg", "diff --git a/sqlglot/newsub/__init__.py b/sqlglot/newsub/__init__.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/newsub/__init__.py\n@@ -0,0 +1,1 @@\n+X = 1\n"),
            ("tests_txt", "diff --git a/tests/raw_data.txt b/tests/raw_data.txt\nnew file mode 100644\n--- /dev/null\n+++ b/tests/raw_data.txt\n@@ -0,0 +1,1 @@\n+hi\n"),
        ]
        for cid, p in broad_patches:
            flat = self._validate_line(cid, self.selector.select_and_pack(cid, p))
            self.assertEqual(len(flat), len(self.tests_list), f"Expected all tests for {cid}")

        self.assertEqual(pp.classify_file(".github/workflows/ci.yml"), "config")
        self.assertEqual(pp.classify_file(".pre-commit-config.yaml"), "config")
        self.assertEqual(pp.classify_file(".github/ISSUE_TEMPLATE/bug_report.md"), "docs")

    def test_docs_dirs_empty_and_mixed_commit_union(self) -> None:
        for rel in ("benchmarks/parse.py", "pdoc/cli.py", "posts/ast_primer.md", "README.md", "docs/guide.md"):
            p = f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            flat = self._validate_line(rel, self.selector.select_and_pack(rel, p))
            self.assertEqual(flat, [], f"Expected [] for docs path {rel}")


if __name__ == "__main__":
    unittest.main()
