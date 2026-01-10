# Match Exhaustiveness: Implementation As Built

This document describes the actual implementation of the match exhaustiveness feature,
based on an analysis of the 14-commit stack. See individual analysis files for details:
- `analysis_1_4.md` - Phases 1-4 (core implementation)
- `analysis_5_7.md` - Phases 5-7 (edge cases and redundant detection)
- `analysis_8_10.md` - Extended features (TypedDict, duplicate detection, attribute exhaustiveness)
- `analysis_11_14.md` - Attribute exhaustiveness refinements and tests

## Stack Overview

| # | Hash | Title | Phase/Feature |
|---|------|-------|---------------|
| 1 | b2b23a1427ef | phase 1 | Expand type coverage (bool, None, final classes, unions) |
| 2 | 80d278d2ea71 | phase 2 | Connect exhaustiveness to return analysis |
| 3 | 6eee6b6be514 | phase 3 | Improve error messages (show missing cases) |
| 4 | 356e629c5ff5 | phase 4 | Sequence pattern exhaustiveness (tuples) |
| 5 | 309670017767 | phase 5 | Document class pattern limitations |
| 6 | 3c96655fb445 | phase 6 | Verify OR pattern handling |
| 7 | e69c84e979f5 | phase 7 | Redundant case detection |
| 8 | 01b9c871f0d3 | TypedDict | Discriminated TypedDict union support |
| 9 | d8935dac521c | Duplicates | Duplicate pattern detection |
| 10 | 89a45af26a47 | Attr Init | Initial attribute exhaustiveness |
| 11 | d242e8a0ac2e | Attr Basic | MatchArgsPosition facets, basic attr checks |
| 12 | 0143b9a483d7 | Multi-attr | Multi-attribute combination checking |
| 13 | 4061691c9741 | Errors | Improved error messages for attr exhaustiveness |
| 14 | 8b541be79e28 | Tests | Comprehensive test coverage |

---

## Comparison to Original Plan

### What Was Planned (PLAN.md)

| Phase | Priority | Status | Notes |
|-------|----------|--------|-------|
| 1: Expand Type Coverage | P0 | ✅ Complete | Commit 1 |
| 2: Control Flow Integration | P0 | ✅ Complete | Commit 2 |
| 3: Error Messages | P1 | ✅ Complete | Commits 3, 13 |
| 4: Sequence Patterns | P2 | ✅ Complete | Commit 4 |
| 5: Class Pattern Improvements | P3 | ⚠️ Documented as limitation + workaround | Commits 5, 10-12 |
| 6: OR Pattern Verification | P2 | ✅ Complete | Commit 6 |
| 7: Redundant Case Detection | P1 | ✅ Complete | Commits 7, 9 |

### What Was Added (Beyond Plan)

1. **TypedDict Discriminated Unions** (Commit 8)
   - Listed as "Future Work" in plan
   - Enables `{"type": "a"}` patterns on TypedDict unions

2. **Duplicate Pattern Detection** (Commit 9)
   - Extension of Phase 7 to detect exact duplicate patterns

3. **Attribute-Based Exhaustiveness** (Commits 10-14)
   - Novel feature not in original plan
   - Uses "positive ops" approach to work around Placeholder limitation
   - Enables exhaustiveness for `@final` classes with exhaustible attributes

---

## Key Architectural Decisions

### 1. Binding/Solving Split for Return Analysis

The core challenge was that return analysis happens at binding time, but type-based
exhaustiveness is only known at solving time. Solution:

- **Binding time**: Create `Binding::MatchExhaustive` bindings for potentially-exhaustive matches
- **Binding time**: Track matches via `LastStmt::Match(TextRange)` in return analysis
- **Solving time**: Resolve `MatchExhaustive` to `Never` if match is type-exhaustive
- **Return analysis**: Check resolved bindings to determine if function can fall through

### 2. Placeholder Workaround for Attribute Exhaustiveness

Class patterns with arguments use `Placeholder` narrow ops, which break negation-based
exhaustiveness. The workaround:

- Use "positive ops" instead of negated ops
- Collect all matched attribute values across patterns
- Check if collected values cover the full type

### 3. Multi-Attribute Combination Checking

For classes with multiple exhaustible attributes:

1. Enumerate all possible value combinations (cartesian product)
2. For each pattern, mark which combinations it covers
3. Report uncovered combinations as missing cases
4. Limit to 1024 combinations to prevent exponential blowup

---

## Key Files Modified

### Core Type Checking
- `lib/alt/narrow.rs` - Exhaustiveness checking, attribute exhaustiveness, error formatting
- `lib/alt/solve.rs` - Solving MatchExhaustive and ReturnImplicit bindings

### Binding Phase
- `lib/binding/binding.rs` - New binding types (MatchExhaustive, MatchCaseReachability, LastStmt::Match)
- `lib/binding/function.rs` - Track potentially-exhaustive matches for return analysis
- `lib/binding/pattern.rs` - Create exhaustiveness bindings, MatchArgsPosition facets
- `lib/binding/narrow.rs` - NarrowOps algebra improvements

### Type System
- `crates/pyrefly_types/src/facet.rs` - MatchArgsPosition facet kind
- `crates/pyrefly_config/src/error_kind.rs` - RedundantMatchCase error kind
- `lib/alt/class/tuple.rs` - Concrete tuple recognition

### Tests
- `lib/test/pattern_match.rs` - Comprehensive exhaustiveness tests
- `lib/test/flow_branching.rs` - Control flow tests (updated)

---

## Known Limitations

1. **Class patterns with arguments**: Placeholder prevents exact negation
   - Workaround via positive ops for @final classes with exhaustible attributes

2. **None pattern duplicates**: Don't detect duplicate None patterns
   - None patterns don't create comparable narrow ops

3. **Combination limit**: Max 1024 combinations for multi-attribute checking
   - Falls back to independent attribute checking

4. **Non-final classes**: Cannot check exhaustiveness on abstract/non-final classes
   - Unknown subclasses may exist at runtime

---

## Test Status

### Summary
- **Total tests at stack tip:** 3377 passed, 0 failed
- **All commits in stack:** Tests pass at each commit

### Per-Commit Results

| Commit | Hash | Tests | Lints | Status |
|--------|------|-------|-------|--------|
| 1 | b2b23a1427ef | ✅ 29 pattern_match | ✅ Clean | Ready |
| 2 | 80d278d2ea71 | ✅ 44 pattern_match | ✅ Clean | Ready |
| 3 | 6eee6b6be514 | ✅ 60 pattern_match | ⚠️ Dead code | Minor fix needed |
| 4 | 356e629c5ff5 | ✅ 28 pattern_match | ✅ Clean | Ready |
| 5 | 309670017767 | ✅ All tests | ✅ Clean | Ready |
| 6 | 3c96655fb445 | ✅ All tests | ✅ Clean | Ready |
| 7 | e69c84e979f5 | ✅ All tests | ✅ Clean | Ready |
| 8 | 01b9c871f0d3 | ✅ All tests | ✅ Clean | Ready |
| 9 | d8935dac521c | ✅ All tests | ✅ Clean | Ready |
| 10 | 89a45af26a47 | ✅ All tests | ✅ Clean | Ready |
| 11 | d242e8a0ac2e | ✅ All tests | ✅ Clean | Ready |
| 12 | 0143b9a483d7 | ✅ All tests | ✅ Clean | Ready |
| 13 | 4061691c9741 | ✅ All tests | ✅ Clean | Ready |
| 14 | 8b541be79e28 | ✅ 3377 total | ⚠️ Dead code | Minor fix needed |

### Outstanding Issue

**Dead code warning:** The `check_attribute_exhaustiveness` method in `pyrefly/lib/alt/narrow.rs`
(~lines 1400-1445) is unused. This method was an initial negation-based approach that was
superseded by `check_attribute_exhaustiveness_with_positive_ops`.

**Fix:** Remove the unused method or add `#[allow(dead_code)]` if it's intended for future use.

---

## Recommendations

### Overall Assessment: **LANDABLE WITH MINOR CLEANUP**

The stack is high-quality, production-ready code that should be landed with careful review.
It is NOT a prototype that needs to be rewritten.

### Why This Stack Should Be Landed

1. **Comprehensive Implementation**: Implements all 7 phases from the plan, plus additional
   valuable features (TypedDict, attribute exhaustiveness)

2. **Solid Architecture**: The binding/solving split is well-designed and follows existing
   pyrefly patterns. The solution to the "binding time vs solving time" problem is elegant.

3. **All Tests Pass**: 3377 tests pass at the tip with no failures

4. **Good Test Coverage**: Each commit adds appropriate tests for the functionality it introduces

5. **Incremental Structure**: The stack is well-organized with atomic commits that can be
   reviewed individually

6. **Novel Solutions**: The "positive ops" approach for attribute exhaustiveness is creative
   and works around a fundamental limitation without invasive changes

### Pre-Landing Cleanup Required

1. **Remove dead code** (commit 3 or 14): Delete the unused `check_attribute_exhaustiveness`
   method from `pyrefly/lib/alt/narrow.rs`

2. **Review commit messages**: Some commits have minimal messages (e.g., "phase 1"). Consider
   amending with more descriptive summaries

3. **Run `arc f`**: Format all modified files in each commit

### Suggested Review Focus Areas

1. **Commit 2**: The binding/solving coordination is the most architecturally significant
   change - review `LastStmt::Match` and `Binding::MatchExhaustive` carefully

2. **Commits 10-14**: The attribute exhaustiveness logic is complex - review the combination
   checking algorithm and edge cases

3. **Error messages**: Verify the missing case formatting produces clear, actionable messages

### Known Limitations to Document

These limitations should be documented in user-facing docs:

1. Class patterns with arguments on non-final classes don't enable exhaustiveness checking
2. Multi-attribute checking is limited to 1024 combinations (10 bool attributes max)
3. Duplicate None patterns are not detected

---

## Conclusion

This stack represents solid engineering work that successfully implements match exhaustiveness
checking in pyrefly. The implementation follows the planned phases while adding valuable
extensions. The code is well-tested, follows existing patterns, and is ready for careful
review and landing. The only required work is minor cleanup (dead code removal, message
improvements, formatting).
