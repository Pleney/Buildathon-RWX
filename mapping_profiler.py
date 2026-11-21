"""Build-time per-test mapping-key lookup profiler and runtime soundness prover
(Phase 2 / Proposal 2: Dialect, Tokenizer, Parser, Generator class dict tables).

Pure Python standard library only.
- At build time (inside build_profiles.py), wraps class-level dict tables on
  Dialect, Tokenizer, Parser, and Generator hierarchies (and hooks
  `sqlglot.generator._build_dispatch` so `_dispatch` lookups map back to
  `cls.TRANSFORMS`) to record per-test:
    * `val_keys`: keys read via `__getitem__` / `get` (value-sensitive)
    * `has_keys`: keys checked via `__contains__` (membership-only; unchanged by
                  `dict_values_only=True` edits!)
    * `len_bits`: tests that called `__len__` (unchanged if key set unchanged)
    * `bool_bits`: tests that called `__bool__` (unchanged if empty/non-empty unchanged)
    * `wholesale_bits`: tests that iterated/copied/compared/mutated the dict
    * `import_val_keys` / `import_has_keys`: keys read at module-import / non-test time
    * `any_access_bits` on `INVERSE_TIME_MAPPING` and `INVERSE_TIME_TRIE` for
      sound `TIME_MAPPING` value-edit proving.
- At runtime (inside selector.py), proves that a class-level dict edit cannot
  affect a specific test T based strictly on T's own recorded access profile
  across the defining class, all transitive subclasses, and any shared-reference
  aliases. Any missing/unstable table, dynamic/unrecognized key, non-pure
  import-time value expression, or derived-table violation conservatively keeps
  the test.
"""

from __future__ import annotations

import ast
import enum
from pathlib import Path
from typing import Any

# Flag bits for per-test table-level accesses
FLAG_LEN = 1
FLAG_BOOL = 2
FLAG_WHOLESALE = 4

# Maximum miss keys retained per Category A table before degrading that table to
# values-and-deletions-only (keeps profiles.pkl.gz compact while remaining 100% sound).
MAX_MISS_KEYS_PER_TABLE = 400

# Category A: Pure lookup tables (no import-time tries, derived sets, or value inverses).
# Safe for value edits, key deletions, AND (when not miss_overflow) key additions.
PURE_LOOKUP_ATTRS: frozenset[str] = frozenset(
    {
        # Parser tables
        "FUNCTIONS",
        "FUNCTION_PARSERS",
        "STATEMENT_PARSERS",
        "PROPERTY_PARSERS",
        "CONSTRAINT_PARSERS",
        "ALTER_PARSERS",
        "ALTER_ALTER_PARSERS",
        "NO_PAREN_FUNCTION_PARSERS",
        "RANGE_PARSERS",
        "COLUMN_OPERATORS",
        "PLACEHOLDER_PARSERS",
        "CONJUNCTION",
        "DISJUNCTION",
        "EXPONENT",
        "UNARY_PARSERS",
        "JSON_OPERATORS",
        "LAMBDAS",
        "ODBC_DATETIME_LITERALS",
        "TYPE_CONVERTERS",
        "TYPE_LITERAL_PARSERS",
        "VERSION_PHRASES",
        "PRIMARY_PARSERS",
        "PROCEDURE_OPTIONS",
        "AGG_FUNC_MAPPING",
        "ANALYZE_EXPRESSION_PARSERS",
        "ARRAY_CONSTRUCTORS",
        "ASSIGNMENT",
        "BRACKET_OFFSETS",
        "CAST_ACTIONS",
        "COMPARISON",
        "CONCAT_OPERATORS",
        "CONFLICT_ACTIONS",
        "EXPRESSION_PARSERS",
        # Generator tables
        "TRANSFORMS",
        "TYPE_MAPPING",
        "TYPE_PARAM_SETTINGS",
        "CAST_MAPPING",
        "TOKEN_MAPPING",
        "CHAR_CAST_MAPPING",
        "EXTRACT_EPOCH_MAPPINGS",
        "EXTRACT_STRFTIME_MAPPINGS",
        "SIGNED_CAST_MAPPING",
        "TIMESTAMP_TYPE_MAPPING",
        "TIME_PART_SINGULARS",
        "UNSIGNED_TYPE_MAPPING",
        # Dialect tables
        "DATE_PART_MAPPING",
        "DEFAULT_FUNCTIONS_COLUMN_NAMES",
        "COLUMN_ALIAS_PRECEDENCE",
        "SET_OP_DISTINCT_BY_DEFAULT",
        "CONVERT_FORMAT_MAPPING",
        "FORMAT_TIME_MAPPING",
        # Tokenizer tables
        "NUMERIC_LITERALS",
    }
)

# Category B: Tables whose KEYS (only!) feed import-time tries/sets/control-char checks.
# Safe ONLY when `dict_values_only=True` (identical non-empty key set => derived
# tries, sets, and key-membership checks are 100% bit-identical).
KEYS_DERIVED_ATTRS: frozenset[str] = frozenset(
    {
        "SHOW_PARSERS",           # -> SHOW_TRIE (keys only)
        "SET_PARSERS",            # -> SET_TRIE (keys only)
        "NUMERIC_ESCAPES",        # -> _Dialect.__new__ checks "x"/"0"/"u" in d (keys only)
        "QUERY_MODIFIER_PARSERS", # -> QUERY_MODIFIER_TOKENS = set(...) (keys only)
        "NO_PAREN_FUNCTIONS",     # -> ID_VAR_TOKENS includes keys (keys only)
        "SUBQUERY_PREDICATES",    # -> ID_VAR_TOKENS / FUNC_TOKENS (keys only)
        "BITWISE",                # -> SQLiteParser.ARITHMETIC_TOKENS (keys only)
        "TERM",                   # -> SQLiteParser.ARITHMETIC_TOKENS (keys only)
        "PROPERTIES_LOCATION",    # -> DuckDB/SQLite iterate keys (keys only)
        "KEYWORDS",               # -> _KEYWORD_TRIE + HINT_START membership (keys only)
    }
)

# Category C: TIME_MAPPING (values feed INVERSE_TIME_MAPPING & INVERSE_TIME_TRIE).
# Safe ONLY when `dict_values_only=True` AND test never touched INVERSE_TIME_MAPPING
# or INVERSE_TIME_TRIE on the defining class, subclasses, or aliases.
TIME_MAPPING_ATTR = "TIME_MAPPING"
INVERSE_TIME_ATTRS: frozenset[str] = frozenset({"INVERSE_TIME_MAPPING", "INVERSE_TIME_TRIE"})

TRACKED_CLASS_DICT_ATTRS: frozenset[str] = PURE_LOOKUP_ATTRS | KEYS_DERIVED_ATTRS | {TIME_MAPPING_ATTR}

_MISSING = object()


def canonicalize_runtime_key(key: Any) -> tuple[Any, ...] | None:
    """Convert a runtime dict lookup key to a compact, collision-free typed tuple."""
    t = type(key)
    if t is str:
        return ("s", key)
    if t is int:
        return ("i", key)
    if t is bool:
        return ("b", key)
    if isinstance(key, enum.Enum):
        en = type(key).__name__
        if en in ("TokenType", "DType"):
            return ("e", en, key.name)
        return None
    if isinstance(key, type):
        mod = getattr(key, "__module__", "")
        if mod.startswith("sqlglot.expressions"):
            return ("t", key.__name__)
        return None
    if t is tuple and all(type(x) is str for x in key):
        return ("u", key)
    return None


def _fast_raw_key(key: Any, token_type_cls: Any) -> Any:
    """Fast collision-free key for per-test raw sets without allocating on str/type/TokenType."""
    t = type(key)
    if t is str or t is type or t is token_type_cls:
        return key
    return (t, key)


def _canonicalize_fast_key(rk: Any, token_type_cls: Any) -> tuple[Any, ...] | None:
    t = type(rk)
    if t is str:
        return ("s", rk)
    if t is token_type_cls:
        return ("e", "TokenType", rk.name)
    if isinstance(rk, type):
        mod = getattr(rk, "__module__", "")
        if mod.startswith("sqlglot.expressions"):
            return ("t", rk.__name__)
        return None
    if t is tuple and len(rk) == 2 and isinstance(rk[0], type):
        return canonicalize_runtime_key(rk[1])
    return canonicalize_runtime_key(rk)


class MappingProfileRecorder:
    """Fast per-test mapping-key access recorder for build_profiles.py."""

    def __init__(self, expr_recorder: Any = None) -> None:
        self.expr_recorder = expr_recorder
        self.active: bool = False
        self.in_import: int = 0
        self.cur_key: Any = None
        self._token_type_cls: Any = None

        # tid -> (cls_id, attr_name)
        self.tables: list[tuple[str, str]] = []
        self.table_to_tid: dict[tuple[str, str], int] = {}
        self.table_base_keys: list[frozenset[Any]] = []
        self.table_track_misses: list[bool] = []
        # tid -> set of alias (cls_id, attr_name) sharing the exact same dict object
        self.table_aliases: dict[int, set[tuple[str, str]]] = {}

        # Per-test scratch buffers (flushed on switch_target)
        self.cur_val_keys: list[set[Any]] = []
        self.cur_has_keys: list[set[Any]] = []
        self.cur_flags: bytearray = bytearray()
        self.touched_val_tids: list[int] = []
        self.touched_has_tids: list[int] = []
        self.touched_flag_tids: list[int] = []

        # Accumulated per-test records (keyed by test_nodeid or ("class", cls_key))
        self.val_records: dict[Any, dict[int, set[Any]]] = {}
        self.has_records: dict[Any, dict[int, set[Any]]] = {}
        self.flag_records: dict[Any, dict[int, int]] = {}

        # Import-time / outside-test accesses
        self.import_val_keys: dict[int, set[Any]] = {}
        self.import_has_keys: dict[int, set[Any]] = {}
        self.import_wholesale: set[int] = set()

    def set_token_type_cls(self, cls: Any) -> None:
        self._token_type_cls = cls

    def register_table(
        self,
        cls_id: str,
        attr_name: str,
        base_dict: dict[Any, Any],
        track_misses: bool,
    ) -> int:
        key = (cls_id, attr_name)
        existing = self.table_to_tid.get(key)
        if existing is not None:
            return existing
        tid = len(self.tables)
        self.tables.append(key)
        self.table_to_tid[key] = tid
        self.table_track_misses.append(track_misses)
        # Canonicalize base keys for distinguishing hits vs misses later
        bkeys: set[Any] = set()
        for k in dict.keys(base_dict):
            ck = canonicalize_runtime_key(k)
            if ck is not None:
                bkeys.add(ck)
        self.table_base_keys.append(frozenset(bkeys))
        self.cur_val_keys.append(set())
        self.cur_has_keys.append(set())
        self.cur_flags.append(0)
        return tid

    def register_alias(self, owner_tid: int, alias_cls_id: str, attr_name: str) -> None:
        alias_key = (alias_cls_id, attr_name)
        if alias_key != self.tables[owner_tid]:
            self.table_aliases.setdefault(owner_tid, set()).add(alias_key)

    def _is_in_probe(self) -> bool:
        er = self.expr_recorder
        return bool(er is not None and er.in_probe)

    def record_val_key(self, tid: int, key: Any) -> None:
        if self._is_in_probe() or self.in_import > 0:
            return
        if self.active:
            try:
                rk = _fast_raw_key(key, self._token_type_cls)
                s = self.cur_val_keys[tid]
                if rk not in s:
                    if not s:
                        self.touched_val_tids.append(tid)
                    s.add(rk)
            except TypeError:
                self.record_flag(tid, FLAG_WHOLESALE)
        else:
            try:
                ck = canonicalize_runtime_key(key)
                if ck is not None:
                    self.import_val_keys.setdefault(tid, set()).add(ck)
                else:
                    self.import_wholesale.add(tid)
            except TypeError:
                self.import_wholesale.add(tid)

    def record_has_key(self, tid: int, key: Any) -> None:
        if self._is_in_probe() or self.in_import > 0:
            return
        if self.active:
            try:
                rk = _fast_raw_key(key, self._token_type_cls)
                s = self.cur_has_keys[tid]
                if rk not in s:
                    if not s:
                        self.touched_has_tids.append(tid)
                    s.add(rk)
            except TypeError:
                self.record_flag(tid, FLAG_WHOLESALE)
        else:
            try:
                ck = canonicalize_runtime_key(key)
                if ck is not None:
                    self.import_has_keys.setdefault(tid, set()).add(ck)
                else:
                    self.import_wholesale.add(tid)
            except TypeError:
                self.import_wholesale.add(tid)

    def record_flag(self, tid: int, flag: int) -> None:
        if self._is_in_probe():
            return
        if self.active and self.in_import == 0:
            prev = self.cur_flags[tid]
            if not (prev & flag):
                if prev == 0:
                    self.touched_flag_tids.append(tid)
                self.cur_flags[tid] = prev | flag
        elif self.in_import == 0 and (flag & FLAG_WHOLESALE):
            # Wholesale read outside any active test AND outside module import
            self.import_wholesale.add(tid)

    def switch_target(self, key: Any) -> None:
        if self.cur_key is not None:
            ck = self.cur_key
            tt = self._token_type_cls
            if self.touched_val_tids:
                dest = self.val_records.get(ck)
                if dest is None:
                    dest = {}
                    self.val_records[ck] = dest
                for tid in self.touched_val_tids:
                    raw_set = self.cur_val_keys[tid]
                    dset = dest.get(tid)
                    if dset is None:
                        dset = set()
                        dest[tid] = dset
                    unknown_seen = False
                    for rk in raw_set:
                        c = _canonicalize_fast_key(rk, tt)
                        if c is not None:
                            dset.add(c)
                        else:
                            unknown_seen = True
                    raw_set.clear()
                    if unknown_seen:
                        # Unrecognized key type -> conservatively mark wholesale for this test
                        fdest = self.flag_records.setdefault(ck, {})
                        fdest[tid] = fdest.get(tid, 0) | FLAG_WHOLESALE
                self.touched_val_tids.clear()

            if self.touched_has_tids:
                dest = self.has_records.get(ck)
                if dest is None:
                    dest = {}
                    self.has_records[ck] = dest
                for tid in self.touched_has_tids:
                    raw_set = self.cur_has_keys[tid]
                    dset = dest.get(tid)
                    if dset is None:
                        dset = set()
                        dest[tid] = dset
                    unknown_seen = False
                    for rk in raw_set:
                        c = _canonicalize_fast_key(rk, tt)
                        if c is not None:
                            dset.add(c)
                        else:
                            unknown_seen = True
                    raw_set.clear()
                    if unknown_seen:
                        fdest = self.flag_records.setdefault(ck, {})
                        fdest[tid] = fdest.get(tid, 0) | FLAG_WHOLESALE
                self.touched_has_tids.clear()

            if self.touched_flag_tids:
                fdest = self.flag_records.get(ck)
                if fdest is None:
                    fdest = {}
                    self.flag_records[ck] = fdest
                flags = self.cur_flags
                for tid in self.touched_flag_tids:
                    fdest[tid] = fdest.get(tid, 0) | flags[tid]
                    flags[tid] = 0
                self.touched_flag_tids.clear()

        self.cur_key = key
        self.active = key is not None

    def stop(self) -> None:
        self.switch_target(None)


class _TrackedTableDict(dict):
    """Lightweight dict subclass tracking per-test key lookups, membership,
    len, bool, and wholesale accesses."""

    __slots__ = ("_rec", "_tid", "_track_misses")

    def __init__(self, data: dict[Any, Any], rec: MappingProfileRecorder, tid: int, track_misses: bool) -> None:
        super().__init__(data)
        self._rec = rec
        self._tid = tid
        self._track_misses = track_misses

    def __getitem__(self, key: Any) -> Any:
        rec = self._rec
        if self._track_misses:
            rec.record_val_key(self._tid, key)
            return dict.__getitem__(self, key)
        val = dict.get(self, key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        rec.record_val_key(self._tid, key)
        return val

    def get(self, key: Any, default: Any = None) -> Any:
        val = dict.get(self, key, _MISSING)
        if val is _MISSING:
            if self._track_misses:
                self._rec.record_val_key(self._tid, key)
            return default
        self._rec.record_val_key(self._tid, key)
        return val

    def __contains__(self, key: object) -> bool:
        res = dict.__contains__(self, key)
        if res or self._track_misses or self._rec.in_import > 0 or not self._rec.active:
            self._rec.record_has_key(self._tid, key)
        return res

    def __len__(self) -> int:
        self._rec.record_flag(self._tid, FLAG_LEN)
        return dict.__len__(self)

    def __bool__(self) -> bool:
        self._rec.record_flag(self._tid, FLAG_BOOL)
        return dict.__len__(self) > 0

    def __iter__(self):
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__iter__(self)

    def keys(self):
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.keys(self)

    def values(self):
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.values(self)

    def items(self):
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.items(self)

    def copy(self) -> dict[Any, Any]:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.copy(self)

    def __eq__(self, other: object) -> bool:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__ne__(self, other)

    def __repr__(self) -> str:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__repr__(self)

    def __str__(self) -> str:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__str__(self)

    def __or__(self, other: Any) -> Any:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__or__(self, other)

    def __ror__(self, other: Any) -> Any:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.__ror__(self, other)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: Any) -> None:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        dict.__delitem__(self, key)

    def pop(self, *args: Any) -> Any:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.pop(self, *args)

    def popitem(self) -> tuple[Any, Any]:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.popitem(self)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        return dict.setdefault(self, key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        dict.update(self, *args, **kwargs)

    def clear(self) -> None:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)
        dict.clear(self)


class _AnyAccessTableDict(dict):
    """Minimal-overhead dict subclass for derived tables (INVERSE_TIME_MAPPING,
    INVERSE_TIME_TRIE) that records a single any-access flag per test."""

    __slots__ = ("_rec", "_tid")

    def __init__(self, data: dict[Any, Any], rec: MappingProfileRecorder, tid: int) -> None:
        super().__init__(data)
        self._rec = rec
        self._tid = tid

    def _hit(self) -> None:
        self._rec.record_flag(self._tid, FLAG_WHOLESALE)

    def __getitem__(self, key: Any) -> Any:
        self._hit()
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._hit()
        return dict.get(self, key, default)

    def __contains__(self, key: object) -> bool:
        self._hit()
        return dict.__contains__(self, key)

    def __len__(self) -> int:
        self._hit()
        return dict.__len__(self)

    def __bool__(self) -> bool:
        self._hit()
        return dict.__len__(self) > 0

    def __iter__(self):
        self._hit()
        return dict.__iter__(self)

    def keys(self):
        self._hit()
        return dict.keys(self)

    def values(self):
        self._hit()
        return dict.values(self)

    def items(self):
        self._hit()
        return dict.items(self)

    def copy(self) -> dict[Any, Any]:
        self._hit()
        return dict.copy(self)

    def __eq__(self, other: object) -> bool:
        self._hit()
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        self._hit()
        return dict.__ne__(self, other)

    def __or__(self, other: Any) -> Any:
        self._hit()
        return dict.__or__(self, other)

    def __ror__(self, other: Any) -> Any:
        self._hit()
        return dict.__ror__(self, other)


def _is_target_hierarchy(cls: type) -> bool:
    for base in cls.__mro__:
        mod = getattr(base, "__module__", "")
        nm = base.__name__
        if (
            (mod == "sqlglot.dialects.dialect" and nm == "Dialect")
            or (mod == "sqlglot.tokens" and nm in ("Tokenizer", "_TokenizerBase"))
            or (mod == "sqlglot.parser" and nm == "Parser")
            or (mod == "sqlglot.generator" and nm == "Generator")
        ):
            return True
    return False


def _walk_module_classes(obj: Any, mod_name: str, seen: set[int]) -> list[type]:
    out: list[type] = []
    if isinstance(obj, type):
        if id(obj) in seen or getattr(obj, "__module__", None) != mod_name:
            return out
        seen.add(id(obj))
        out.append(obj)
        for v in obj.__dict__.values():
            if isinstance(v, type):
                out.extend(_walk_module_classes(v, mod_name, seen))
    return out


def install_mapping_hooks(
    module: Any,
    rel: str,
    recorder: MappingProfileRecorder,
    *,
    track_keywords: bool = True,
) -> None:
    """Wrap tracked class-level dict tables defined in `module` and hook
    `sqlglot.generator._build_dispatch` so `_dispatch` lookups record on TRANSFORMS."""
    if rel == "sqlglot/tokenizer_core.py":
        tt = getattr(module, "TokenType", None)
        if tt is not None:
            recorder.set_token_type_cls(tt)
        return

    if not (
        rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/"))
        or rel in ("sqlglot/tokens.py", "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/jsonpath.py")
    ):
        return

    mod_name = module.__name__
    seen: set[int] = set()
    classes: list[type] = []
    for v in list(module.__dict__.values()):
        if isinstance(v, type) and getattr(v, "__module__", None) == mod_name:
            classes.extend(_walk_module_classes(v, mod_name, seen))

    for cls in classes:
        if not _is_target_hierarchy(cls):
            continue
        cls_id = f"{rel}:{cls.__qualname__}"
        for attr_name, val in list(cls.__dict__.items()):
            if not isinstance(val, dict):
                continue
            if attr_name == "KEYWORDS" and not track_keywords:
                continue
            if attr_name in INVERSE_TIME_ATTRS:
                if isinstance(val, _AnyAccessTableDict):
                    recorder.register_alias(val._tid, cls_id, attr_name)
                elif type(val) is dict:
                    tid = recorder.register_table(cls_id, attr_name, val, track_misses=False)
                    setattr(cls, attr_name, _AnyAccessTableDict(val, recorder, tid))
                continue
            if attr_name not in TRACKED_CLASS_DICT_ATTRS:
                continue
            if isinstance(val, _TrackedTableDict):
                recorder.register_alias(val._tid, cls_id, attr_name)
                continue
            if type(val) is not dict:
                # Skip defaultdict or custom dict subclasses conservatively
                continue
            track_misses = attr_name in PURE_LOOKUP_ATTRS
            tid = recorder.register_table(cls_id, attr_name, val, track_misses=track_misses)
            setattr(cls, attr_name, _TrackedTableDict(val, recorder, tid, track_misses=track_misses))

    # Hook sqlglot.generator._build_dispatch so self._dispatch.get(expr_cls)
    # attributes lookups directly to cls.TRANSFORMS's tid!
    if rel == "sqlglot/generator.py":
        orig_build_dispatch = getattr(module, "_build_dispatch", None)
        if orig_build_dispatch is not None and not getattr(orig_build_dispatch, "_blast_wrapped", False):
            def _wrapped_build_dispatch(cls: type) -> dict[Any, Any]:
                recorder.in_import += 1
                try:
                    dispatch = orig_build_dispatch(cls)
                finally:
                    recorder.in_import -= 1
                tr = getattr(cls, "TRANSFORMS", None)
                if isinstance(tr, _TrackedTableDict):
                    return _TrackedTableDict(dispatch, recorder, tr._tid, track_misses=True)
                return dispatch

            _wrapped_build_dispatch._blast_wrapped = True  # type: ignore[attr-defined]
            module._build_dispatch = _wrapped_build_dispatch


def scan_defined_dict_tables(sqlglot_dir: Path) -> set[tuple[str, str]]:
    """Static AST scan of all tracked class-level dict assignments across sqlglot/
    so runtime proving never silently skips an unimported or unstable subclass override."""
    defined: set[tuple[str, str]] = set()
    root = sqlglot_dir.resolve()
    sg = root / "sqlglot"
    for p in sorted(sg.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if not (
            rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/"))
            or rel in ("sqlglot/tokens.py", "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/jsonpath.py")
        ):
            continue
        try:
            tree = ast.parse(p.read_bytes())
        except Exception:
            continue

        def walk_cls(body: list[ast.stmt], prefix: str) -> None:
            for st in body:
                if isinstance(st, ast.ClassDef):
                    qn = f"{prefix}.{st.name}" if prefix else st.name
                    cls_id = f"{rel}:{qn}"
                    for bst in st.body:
                        tname = None
                        val = None
                        if isinstance(bst, ast.Assign) and len(bst.targets) == 1 and isinstance(bst.targets[0], ast.Name):
                            tname = bst.targets[0].id
                            val = bst.value
                        elif isinstance(bst, ast.AnnAssign) and isinstance(bst.target, ast.Name):
                            tname = bst.target.id
                            val = bst.value
                        if tname and val is not None and tname in TRACKED_CLASS_DICT_ATTRS:
                            defined.add((cls_id, tname))
                    walk_cls(st.body, qn)

        walk_cls(tree.body, "")
    return defined


def scan_import_time_dict_subscripts(sqlglot_dir: Path) -> dict[str, set[tuple[Any, ...] | None]]:
    """Static AST scan of non-function (module/class-body) subscripts or `.get()` calls
    on tracked class dict attrs (e.g. `parser.Parser.FUNCTION_PARSERS['TRY_CAST']`).
    Returns `{attr_name: set_of_typed_keys_or_None}`."""
    reads: dict[str, set[tuple[Any, ...] | None]] = {}
    root = sqlglot_dir.resolve()
    sg = root / "sqlglot"
    for p in sorted(sg.rglob("*.py")):
        try:
            tree = ast.parse(p.read_bytes())
        except Exception:
            continue

        def scan(body: list[ast.stmt]) -> None:
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(st, ast.ClassDef):
                    scan(st.body)
                    continue
                for sub in ast.walk(st):
                    if isinstance(sub, ast.Subscript):
                        v = sub.value
                        nm = v.attr if isinstance(v, ast.Attribute) else (v.id if isinstance(v, ast.Name) else None)
                        if nm in TRACKED_CLASS_DICT_ATTRS:
                            tk = _ast_dict_typed_key(sub.slice)
                            reads.setdefault(nm, set()).add(tk)
                    elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get":
                        v = sub.func.value
                        nm = v.attr if isinstance(v, ast.Attribute) else (v.id if isinstance(v, ast.Name) else None)
                        if nm in TRACKED_CLASS_DICT_ATTRS:
                            tk = _ast_dict_typed_key(sub.args[0]) if sub.args else None
                            reads.setdefault(nm, set()).add(tk)

        scan(tree.body)
    return reads


# ---------------------------------------------------------------------------
# Runtime Formal Soundness Prover (used inside selector.py)
# ---------------------------------------------------------------------------
def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _ast_dict_typed_key(node: ast.AST | None) -> tuple[Any, ...] | None:
    """Parse an AST dict key literal/symbol into the exact canonical typed key
    recorded at build time. Returns None on any unrecognized/dynamic key."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        v = node.value
        t = type(v)
        if t is str:
            return ("s", v)
        if t is int:
            return ("i", v)
        if t is bool:
            return ("b", v)
        return None
    if isinstance(node, ast.Attribute):
        # TokenType.FOO or tokens.TokenType.FOO
        if isinstance(node.value, ast.Name) and node.value.id == "TokenType":
            return ("e", "TokenType", node.attr)
        if isinstance(node.value, ast.Attribute) and node.value.attr == "TokenType":
            return ("e", "TokenType", node.attr)
        # exp.DType.FOO or exp.DataType.Type.FOO or DType.FOO
        if isinstance(node.value, ast.Name) and node.value.id == "DType":
            return ("e", "DType", node.attr)
        if isinstance(node.value, ast.Attribute):
            if node.value.attr == "DType" and isinstance(node.value.value, ast.Name) and node.value.value.id == "exp":
                return ("e", "DType", node.attr)
            if (
                node.value.attr == "Type"
                and isinstance(node.value.value, ast.Attribute)
                and node.value.value.attr == "DataType"
            ):
                return ("e", "DType", node.attr)
        # exp.ExprSubclass
        if isinstance(node.value, ast.Name) and node.value.id == "exp":
            # Exclude non-Expr namespaces inside exp
            if node.attr not in ("DType", "Type"):
                return ("t", node.attr)
        return None
    if isinstance(node, ast.Tuple):
        elts: list[str] = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and type(e.value) is str:
                elts.append(e.value)
            else:
                return None
        return ("u", tuple(elts))
    return None


def _is_pure_closure_factory_def(fn: ast.AST) -> bool:
    """Return True if `fn` is an undecorated function whose ENTIRE body only
    creates and returns a nested function/lambda with import-time pure defaults."""
    if not isinstance(fn, ast.FunctionDef) or fn.decorator_list:
        return False
    body = fn.body
    i = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        i = 1
    rem = body[i:]
    if len(rem) == 1 and isinstance(rem[0], ast.Return) and isinstance(rem[0].value, ast.Lambda):
        lam = rem[0].value
        return all(_is_import_time_pure_value(d) for d in lam.args.defaults) and all(
            d is None or _is_import_time_pure_value(d) for d in lam.args.kw_defaults
        )
    if (
        len(rem) == 2
        and isinstance(rem[0], ast.FunctionDef)
        and not rem[0].decorator_list
        and isinstance(rem[1], ast.Return)
        and isinstance(rem[1].value, ast.Name)
        and rem[1].value.id == rem[0].name
    ):
        inner = rem[0]
        return all(_is_import_time_pure_value(d) for d in inner.args.defaults) and all(
            d is None or _is_import_time_pure_value(d) for d in inner.args.kw_defaults
        )
    return False


def _call_binds_cleanly(call: ast.Call, fargs: ast.arguments) -> bool:
    """Return True only if `call` validly binds to `fargs` without TypeError."""
    for a in call.args:
        if isinstance(a, ast.Starred):
            return False
    kw_names: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None or kw.arg in kw_names:
            return False
        kw_names.add(kw.arg)

    pos_params = list(fargs.posonlyargs) + list(fargs.args)
    n_pos_params = len(pos_params)
    n_pos_defaults = len(fargs.defaults)
    n_req_pos = n_pos_params - n_pos_defaults

    n_pos_given = len(call.args)
    if n_pos_given > n_pos_params and fargs.vararg is None:
        return False

    bound_pos: set[str] = set()
    for i in range(min(n_pos_given, n_pos_params)):
        bound_pos.add(pos_params[i].arg)

    posonly_names = {a.arg for a in fargs.posonlyargs}
    regular_names = {a.arg for a in fargs.args}
    kwonly_names = {a.arg for a in fargs.kwonlyargs}

    for k in kw_names:
        if k in bound_pos:
            return False
        if k in regular_names:
            bound_pos.add(k)
        elif k in kwonly_names:
            pass
        elif fargs.kwarg is None or k in posonly_names:
            return False

    # Check all required positional params are bound
    for i in range(n_req_pos):
        if pos_params[i].arg not in bound_pos:
            return False

    # Check all required keyword-only params (kw_default is None) are supplied
    for a, d in zip(fargs.kwonlyargs, fargs.kw_defaults):
        if d is None and a.arg not in kw_names:
            return False

    return True


def _resolve_factory_def(
    fn_name: str,
    old_tree: ast.AST,
    resolve_module_tree: Any = None,
) -> ast.FunctionDef | None:
    """Resolve top-level function `fn_name` in `old_tree` or imported from a sqlglot module."""
    for st in getattr(old_tree, "body", ()):
        if isinstance(st, ast.FunctionDef) and st.name == fn_name:
            return st
    if resolve_module_tree is None:
        return None
    for st in getattr(old_tree, "body", ()):
        if isinstance(st, ast.ImportFrom) and st.module and st.level == 0:
            for alias in st.names:
                local_name = alias.asname or alias.name
                if local_name == fn_name:
                    mod_tree = resolve_module_tree(st.module)
                    if mod_tree is not None:
                        for mst in getattr(mod_tree, "body", ()):
                            if isinstance(mst, ast.FunctionDef) and mst.name == alias.name:
                                return mst
                    return None
    return None


def _is_import_time_pure_value(
    node: ast.AST,
    existing_sym_dumps: set[str] | None = None,
    old_tree: ast.AST | None = None,
    resolve_module_tree: Any = None,
) -> bool:
    """Return True only if evaluating `node` during class body / module import
    cannot execute arbitrary code, mutate state, or raise NameError/AttributeError."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        return isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float, bool))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_import_time_pure_value(e, existing_sym_dumps, old_tree, resolve_module_tree) for e in node.elts)
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None:
                return False
            if not _is_import_time_pure_value(k, existing_sym_dumps, old_tree, resolve_module_tree):
                return False
            if not _is_import_time_pure_value(v, existing_sym_dumps, old_tree, resolve_module_tree):
                return False
        return True
    if isinstance(node, ast.Lambda):
        # Creating a lambda never runs its body; only check default argument expressions!
        for d in node.args.defaults:
            if not _is_import_time_pure_value(d, existing_sym_dumps, old_tree, resolve_module_tree):
                return False
        for d in node.args.kw_defaults:
            if d is not None and not _is_import_time_pure_value(d, existing_sym_dumps, old_tree, resolve_module_tree):
                return False
        return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        # Only safe if this exact symbol/attribute already appeared in old_tree
        # (so it is guaranteed to exist & evaluate without side effects at import time).
        if existing_sym_dumps is not None and _dump(node) in existing_sym_dumps:
            return True
        return False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and old_tree is not None:
        # Pure closure-factory call (e.g., _show_parser(...), rename_func(...), build_formatted_time(...))
        fn_def = _resolve_factory_def(node.func.id, old_tree, resolve_module_tree)
        if fn_def is None or not _is_pure_closure_factory_def(fn_def):
            return False
        if not _call_binds_cleanly(node, fn_def.args):
            return False
        if not all(_is_import_time_pure_value(a, existing_sym_dumps, old_tree, resolve_module_tree) for a in node.args):
            return False
        if not all(_is_import_time_pure_value(kw.value, existing_sym_dumps, old_tree, resolve_module_tree) for kw in node.keywords):
            return False
        return True
    return False


def _find_class_attr_assign(tree: ast.AST, cls_qname: str, attr_name: str) -> ast.AST | None:
    """Locate the unique single assignment RHS of `attr_name` in class `cls_qname`."""
    parts = cls_qname.split(".")
    cur_body: list[ast.stmt] = getattr(tree, "body", [])
    target_cls: ast.ClassDef | None = None
    for idx, part in enumerate(parts):
        found = None
        for st in cur_body:
            if isinstance(st, ast.ClassDef) and st.name == part:
                found = st
                break
        if found is None:
            return None
        if idx == len(parts) - 1:
            target_cls = found
        else:
            cur_body = found.body
    if target_cls is None:
        return None
    matches: list[ast.AST] = []
    for st in target_cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            if st.targets[0].id == attr_name:
                matches.append(st.value)
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            if st.target.id == attr_name and st.value is not None:
                matches.append(st.value)
    if len(matches) != 1:
        return None
    return matches[0]


def _collect_existing_sym_dumps(old_tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(old_tree):
        if isinstance(node, (ast.Name, ast.Attribute)):
            out.add(_dump(node))
    return out


def diff_class_dict_typed(
    old_tree: ast.AST,
    new_tree: ast.AST,
    cls_qname: str,
    attr_name: str,
    *,
    resolve_module_tree: Any = None,
) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]], set[tuple[Any, ...]], bool, bool] | None:
    """Compare old and new ast.Dict definitions of `cls_qname.attr_name`.
    Returns `(changed_val_keys, added_keys, removed_keys, len_unchanged, bool_unchanged)`
    or `None` if not a clean, pure, spread-preserving typed dict edit.
    """
    old_val = _find_class_attr_assign(old_tree, cls_qname, attr_name)
    new_val = _find_class_attr_assign(new_tree, cls_qname, attr_name)
    if not isinstance(old_val, ast.Dict) or not isinstance(new_val, ast.Dict):
        return None

    # Spreads (**Base.TABLE, etc.) must be 100% identical in order and content
    old_spreads = [_dump(v) for k, v in zip(old_val.keys, old_val.values) if k is None]
    new_spreads = [_dump(v) for k, v in zip(new_val.keys, new_val.values) if k is None]
    if old_spreads != new_spreads:
        return None

    old_items: dict[tuple[Any, ...], tuple[str, ast.AST]] = {}
    for k, v in zip(old_val.keys, old_val.values):
        if k is None:
            continue
        tk = _ast_dict_typed_key(k)
        if tk is None:
            return None
        # Reject duplicate literal keys conservatively
        if tk in old_items:
            return None
        old_items[tk] = (_dump(v), v)

    new_items: dict[tuple[Any, ...], tuple[str, ast.AST]] = {}
    for k, v in zip(new_val.keys, new_val.values):
        if k is None:
            continue
        tk = _ast_dict_typed_key(k)
        if tk is None:
            return None
        if tk in new_items:
            return None
        new_items[tk] = (_dump(v), v)

    old_keys = set(old_items.keys())
    new_keys = set(new_items.keys())
    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    common_keys = old_keys & new_keys
    changed_val_keys = {k for k in common_keys if old_items[k][0] != new_items[k][0]}

    if not changed_val_keys and not added_keys and not removed_keys:
        return None

    # Verify EVERY modified or added value expression is import-time pure/safe
    # (cannot execute side effects or raise NameError/AttributeError during import!)
    sym_dumps: set[str] | None = None
    for k in changed_val_keys | added_keys:
        new_node = new_items[k][1]
        if not _is_import_time_pure_value(new_node):
            if sym_dumps is None:
                sym_dumps = _collect_existing_sym_dumps(old_tree)
            if not _is_import_time_pure_value(new_node, sym_dumps, old_tree, resolve_module_tree):
                return None

    # Check whether len/bool could change (accounting conservatively for spreads)
    has_spreads = bool(old_spreads)
    same_keys = (not added_keys) and (not removed_keys)
    len_unchanged = same_keys
    if same_keys:
        bool_unchanged = True
    elif has_spreads:
        # Spreads might or might not be empty; only sure bool unchanged if both have >=1 explicit key
        bool_unchanged = bool(old_keys) and bool(new_keys)
    else:
        bool_unchanged = bool(old_keys) == bool(new_keys)

    return changed_val_keys, added_keys, removed_keys, len_unchanged, bool_unchanged


def prove_unchanged_for_class_dict(
    rel: str,
    cls_qname: str,
    attr_name: str,
    old_tree: ast.AST,
    new_tree: ast.AST,
    candidate_bits: int,
    *,
    class_subclasses: dict[str, list[str]],
    mapping_profiles: dict[tuple[str, str], dict[str, Any]],
    defined_dict_tables: set[tuple[str, str]] | frozenset[tuple[str, str]],
    unprofiled_test_bits: int = 0,
    resolve_module_tree: Any = None,
) -> int:
    """Return bitset of tests in `candidate_bits` formally proven unable to observe
    the class-level dict edit to `rel:cls_qname.attr_name`. Returns 0 on any uncertainty."""
    if not candidate_bits or not mapping_profiles or attr_name not in TRACKED_CLASS_DICT_ATTRS:
        return 0

    diff = diff_class_dict_typed(
        old_tree,
        new_tree,
        cls_qname,
        attr_name,
        resolve_module_tree=resolve_module_tree,
    )
    if diff is None:
        return 0
    changed_val_keys, added_keys, removed_keys, len_unchanged, bool_unchanged = diff
    values_only = (not added_keys) and (not removed_keys)

    # Category B & TIME_MAPPING require identical key sets (tries / derived sets unchanged)
    if (attr_name in KEYS_DERIVED_ATTRS or attr_name == TIME_MAPPING_ATTR) and not values_only:
        return 0

    cls_id = f"{rel}:{cls_qname}"
    root_key = (cls_id, attr_name)
    if root_key not in mapping_profiles:
        return 0

    # Collect all affected tables: defining class + all transitive subclasses + shared aliases
    affected_cls_ids: set[str] = {cls_id}
    for sub_id in class_subclasses.get(cls_id, ()):
        affected_cls_ids.add(sub_id)

    # Expand aliases transitively (e.g., Presto.TIME_MAPPING aliasing MySQL.TIME_MAPPING)
    queue = list(affected_cls_ids)
    while queue:
        cid = queue.pop()
        prof = mapping_profiles.get((cid, attr_name))
        if prof is not None:
            for alias_cid, alias_attr in prof.get("aliases", ()):
                if alias_attr == attr_name and alias_cid not in affected_cls_ids:
                    affected_cls_ids.add(alias_cid)
                    queue.append(alias_cid)
                    for sub_id in class_subclasses.get(alias_cid, ()):
                        if sub_id not in affected_cls_ids:
                            affected_cls_ids.add(sub_id)
                            queue.append(sub_id)

    all_changed_keys = changed_val_keys | added_keys | removed_keys
    membership_changed_keys = added_keys | removed_keys

    unsafe_bits = unprofiled_test_bits

    for cid in affected_cls_ids:
        # Special sound rule for TIME_MAPPING: check INVERSE_TIME_MAPPING & INVERSE_TIME_TRIE
        # on EVERY affected class/subclass/alias BEFORE skipping inherited/aliased TIME_MAPPING.
        if attr_name == TIME_MAPPING_ATTR:
            for inv_attr in INVERSE_TIME_ATTRS:
                inv_prof = mapping_profiles.get((cid, inv_attr))
                if inv_prof is None:
                    # Missing/unstable inverse time table profile -> refuse to prove!
                    return 0
                if inv_prof.get("import_wholesale", False):
                    return 0
                unsafe_bits |= inv_prof.get("wholesale", 0)
                for a_cid, a_attr in inv_prof.get("aliases", ()):
                    ap = mapping_profiles.get((a_cid, a_attr))
                    if ap is not None:
                        unsafe_bits |= ap.get("wholesale", 0)

        tkey = (cid, attr_name)
        prof = mapping_profiles.get(tkey)
        if prof is None:
            # If this subclass explicitly defines its own dict in source, yet has no
            # stable profile (missing or unstable across 2x build runs), refuse to prove!
            if tkey in defined_dict_tables:
                return 0
            # Otherwise it inherits the parent's dict directly (already checked via parent!)
            continue

        # Shared-reference alias (e.g. Presto.TIME_MAPPING = MySQL.TIME_MAPPING):
        # its runtime lookups were recorded directly on its owner table.
        if "alias_of" in prof:
            owner_key = prof["alias_of"]
            owner_prof = mapping_profiles.get(owner_key)
            if owner_prof is None:
                return 0
            prof = owner_prof

        # Any wholesale access outside tests -> unsafe
        if prof.get("import_wholesale", False):
            return 0
        # Any changed key read by value at import time -> unsafe
        imp_val = prof.get("import_val_keys", ())
        if imp_val and any(k in imp_val for k in all_changed_keys):
            return 0
        # Any added/removed key checked for membership at import time -> unsafe
        imp_has = prof.get("import_has_keys", ())
        if imp_has and any(k in imp_has for k in membership_changed_keys):
            return 0
        # If keys were added and this table overflowed miss tracking, cannot prove key addition!
        if added_keys and prof.get("miss_overflow", False):
            return 0

        unsafe_bits |= prof.get("wholesale", 0)
        if not len_unchanged:
            unsafe_bits |= prof.get("len", 0)
        if not bool_unchanged:
            unsafe_bits |= prof.get("bool", 0)

        val_keys: dict[tuple[Any, ...], int] = prof.get("val_keys", {})
        for k in all_changed_keys:
            unsafe_bits |= val_keys.get(k, 0)

        if membership_changed_keys:
            has_keys: dict[tuple[Any, ...], int] = prof.get("has_keys", {})
            for k in membership_changed_keys:
                unsafe_bits |= has_keys.get(k, 0)

    return candidate_bits & ~unsafe_bits
