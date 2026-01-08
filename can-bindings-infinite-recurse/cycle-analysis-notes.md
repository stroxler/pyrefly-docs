# Cycle Resolution Analysis Notes

These notes capture analysis of Pyrefly's cycle resolution behavior, particularly
around nested/overlapping cycles. The goal is to understand potential issues
before implementing the two-pass cycle resolution (v2-doc).

---

## Example 1: Overlapping Cycles from D77669454

**Source:** D77669454 - "unwind all (applicable) cycles on calculation completed"

### Dependency Graph

```
2 → 8 → 4 → 6 → 9 → 8  (back-edge creates C1)
                ↓
                6      (additional edge 9→6 creates sub-cycle C2)
```

So the edges are:
- 2→8, 8→4, 4→6, 6→9, 9→8 (main chain + back-edge)
- 9→6 (discovered during unwind)

### Trace

**Phase 1: Initial computation**

| Step | Action | CalcStack | Cycles |
|------|--------|-----------|--------|
| 1 | get_idx(2) | [2] | [] |
| 2 | 2→8 | [2, 8] | [] |
| 3 | 8→4 | [2, 8, 4] | [] |
| 4 | 4→6 | [2, 8, 4, 6] | [] |
| 5 | 6→9 | [2, 8, 4, 6, 9] | [] |
| 6 | 9→8: CycleDetected | [2, 8, 4, 6, 9, 8] | [] |

At step 6:
- `current_cycle()` = [8, 9, 6, 4]
- `break_at` = 4 (minimum idx)
- Push C1 with `unwind_stack=[4,6,9,8]`, `recursion_stack=[]`

**Phase 2: Recursion to break_at**

| Step | Action | CalcStack | C1.unwind_stack |
|------|--------|-----------|-----------------|
| 7 | 8's solve needs 4, get_idx(4) | [2,8,4,6,9,8,4] | [4,6,9,8] |
| 8 | 4 is break_at → placeholder | [2,8,4,6,9,8] | [4,6,9,8] |
| 9 | 8 finishes, on_calc_finished | [2,8,4,6,9] | [4,6,9] |

**Phase 3: Sub-cycle discovery**

| Step | Action | CalcStack | Cycles |
|------|--------|-----------|--------|
| 10 | 9's solve needs 6, get_idx(6) | [2,8,4,6,9,6] | C1 |
| 11 | pre_calculate_state(6) → NoDetectedCycle | | |
| 12 | propose_calculation(6) → CycleDetected | | |
| 13 | current_cycle() = [6, 9] | | |
| 14 | break_at=6, BreakHere, push C2 | | C1, C2 |
| 15 | Placeholder for 6, return | [2,8,4,6,9] | C1, C2 |

**Phase 4: Shared unwind**

| Step | Action | C1.unwind | C2.unwind |
|------|--------|-----------|-----------|
| 16 | 9 finishes, on_calc_finished | [4,6] | [6] |
| 17 | Pop 9 from stack | | |
| 18 | 6 finishes, on_calc_finished | [4] | [] → pop C2 |
| 19 | 4 finishes, on_calc_finished | [] → pop C1 | |

### Observations

**The fishy behavior at step 11:**
- 6 IS in C1's `unwind_stack` [4,6,9]
- But `pre_calculate_state` only checks `break_at` and `recursion_stack.last()`
- Returns `NoDetectedCycle` even though 6 is an active cycle participant
- This leads to detecting C2 as a "new" cycle

**Is C2 a real cycle?**
YES - the dependency 6→9→6 is a real cycle in the graph. So C2 is not "garbage."

**What's fishy:**
- Node 6 now participates in TWO cycles (C1 and C2)
- `on_calculation_finished(6)` updates BOTH cycles' unwind_stacks
- The cycle "stack" doesn't map 1:1 to Rust call stack segments
- Reasoning about cycle state becomes harder

**Is this causing stack overflows?**
UNCLEAR. This example completes correctly. The overlapping cycles are handled,
just in a way that's harder to reason about.

**Alternative approach (not implemented):**
Instead of pushing C2, recognize that 6 is in C1 and compute it normally.
6's computation would hit 4's placeholder and stop. The cycles would then
form a proper stack.

---

## The `CycleBroken` Protection

When a cycle's break_at has a placeholder recorded (via `record_cycle`),
subsequent accesses return `CycleBroken(placeholder)` immediately:

```rust
// In propose_calculation:
Status::Calculating(calc) => {
    let (rec, threads) = &mut **calc;
    if threads.insert(thread::current().id()) {
        ProposalResult::Calculatable
    } else {
        match rec {
            None => ProposalResult::CycleDetected,
            Some(r) => ProposalResult::CycleBroken(r.dupe()),  // ← Returns immediately
        }
    }
}
```

This means: once we record a placeholder for break_at=A, ANY path that goes
through A will hit this and stop. This should prevent infinite recursion
"through" an enclosing cycle.

**Open question:** Is this protection sufficient, or are there scenarios where
we can infinite-loop without ever hitting a break_at?

---

## Draft Change 84b4fa5449

This change adds `EnclosingCycleParticipant` detection:
- When accessing a binding B from an inner cycle
- If B is in an OUTER cycle's in-progress set (break_at, recursion_stack, or unwind_stack)
- Return `EnclosingCycleParticipant` instead of `NoDetectedCycle`

**The proposed handling in 84b4fa5449:**
Return a placeholder immediately for B.

**Problem with this approach:**
Only break_at bindings should get placeholders. Returning placeholder for
non-break_at bindings is semantically incorrect.

**Alternative approach (discussed but not implemented):**
Compute B normally. B's computation will eventually hit the enclosing cycle's
break_at placeholder and stop. No need for an extra placeholder.

---

## Counter-Based Cycle Detection (Proposed)

To distinguish "re-entering enclosing cycle" from "new cycle":

**Idea:** Track how many times a binding can legitimately appear on the stack.

- Base: 1 (initial computation)
- Add 1 for each cycle the binding participates in

When occurrences > allowed, it's a new cycle.

**Complication:** The count needs to be per-thread and updated as cycles are
pushed/popped.

---

## Stack Overflow Analysis (from production traces)

**Sources:** P2108270570, P2108272595, P2108274049, P2108279227

### Key Findings

**Two distinct patterns observed:**

1. **Mutual recursion pattern** (Pastes 1 & 4):
   - `KeyClassSynthesizedFields` ↔ `KeyAnnotation` alternating
   - Approximately 65-69 frames between each pair

2. **Self-loop pattern** (Pastes 2 & 3):
   - Only `KeyClassSynthesizedFields` appearing repeatedly

### Paste 1 (P2108270570) Detailed Analysis

**Key type distribution:**
- KeyClassSynthesizedFields: 440 occurrences
- KeyDecorator: 220 occurrences
- KeyAnnotation: 216 occurrences

**The repeating dependency chain:**
```
Frame 55: get_idx<KeyClassSynthesizedFields>
Frame 54: calculate_and_record_answer<KeyDecorator>
Frame 53: as_type_alias
Frame 52: force_for_narrowing
Frame 51: solve_tparams
Frame 50: get_class_tparams
Frame 49: get_idx<KeyAnnotation>
Frame 48: finalize_recursive_answer<KeyClassSynthesizedFields>
Frame 47: solve_yield_from
Frame 46: calculate_class_field
... (expression evaluation) ...
→ back to get_idx<KeyClassSynthesizedFields>
```

**Critical observation:**
This is NOT a cycle of a single binding being re-computed. Each `get_idx` call
has the same function address but operates on DIFFERENT Idx values (different
bindings). This represents an **unbounded chain of distinct dependencies**:

```
ClassA.synthesized_fields → ClassA.decorator → ClassB.annotation →
ClassB.synthesized_fields → ClassB.decorator → ClassC.annotation → ...
```

### Why CycleBroken Doesn't Help

The `CycleBroken` protection only works for TRUE cycles (same binding appearing
twice on the stack). When we have an unbounded chain of DIFFERENT bindings:
- A.fields → B.annotation → B.fields → C.annotation → C.fields → ...

Each binding is distinct, so `propose_calculation` returns `Calculatable` for
each one. The thread ID check passes because it's a new binding we haven't
seen on this thread's stack before.

### Root Cause Hypothesis

The stack overflow is caused by **unbounded transitive dependencies** through
class hierarchies or type alias chains. Possible scenarios:

1. **Recursive type aliases**: `type A = B[C]; type B = A[D]` where the
   parameterization creates new synthesized fields each time

2. **Metaclass chains**: Complex metaclass relationships creating unbounded
   decorator → annotation → synthesized_fields dependencies

3. **Cyclic generic instantiation**: `class A(Generic[T]): pass` where
   instantiation chains like `A[A[A[...]]]` occur

### Implications for Two-Pass Resolution

The two-pass cycle resolution (v2-doc) won't fix this issue because:
1. It's not a cycle detection problem
2. It's an unbounded dependency chain problem
3. Need a separate mechanism to detect and limit depth of dependency chains

### Potential Fixes

1. **Depth limit**: Add a configurable maximum recursion depth for get_idx
   (e.g., 1000 calls) and return error type when exceeded

2. **Memoization**: Ensure intermediate results are cached to prevent
   recomputation of the same types

3. **Cycle detection across bindings**: Track the TYPES being computed,
   not just binding Idx values, to detect when we're computing equivalent
   types repeatedly

4. **Lazier evaluation**: Defer some computations (like class field synthesis)
   to avoid eager evaluation chains

---

## Root Cause Identified: `pre_calculate_state` Bug

**Location:** `answers_solver.rs:274-286`

### The Bug

```rust
fn pre_calculate_state(&mut self, current: &CalcId) -> CycleState {
    if *current == self.break_at {
        CycleState::BreakAt
    } else if let Some(c) = self.recursion_stack.last()
        && *current == *c
    {
        // Correct: checks last item of recursion_stack
        CycleState::Participant
    } else {
        CycleState::NoDetectedCycle  // Bug: doesn't check unwind_stack!
    }
}
```

**The recursion_stack.last() check is correct** because during the recursion phase,
the computation path is deterministic. We encounter recursion_stack elements in
exactly LIFO order as we retrace the dependency path toward break_at.

**The bug is that unwind_stack is never checked.** During the unwind phase:
- Computation completes with placeholder/preliminary values
- Finalization code (force_var, record_recursive, etc.) can access ANY binding
- This is fundamentally unpredictable - not retracing the original path

### How This Causes Stack Overflow (Unwind Phase)

1. Cycle C1 detected with [A, B, C, D], break_at=A
2. After recursion completes, unwind_stack=[A, B, C, D]
3. D finishes, C finishes, now B is finishing
4. B's finalization code reads C (still in unwind_stack)
5. `pre_calculate_state(C)`:
   - C != break_at (A) ✗
   - C != recursion_stack.last() (empty) ✗
   - **Doesn't check unwind_stack!**
   - Returns NoDetectedCycle
6. `propose_calculation(C)` returns CycleDetected (thread already computing C)
7. NEW cycle C2 created with C
8. C now participates in BOTH C1 and C2
9. Repeats, creating unbounded overlapping cycles

### Why Duplicate Detection Doesn't Help

- `DuplicateCycleDetected` only triggers when:
  1. cycles.len() > 100 (MAXIMUM_CYCLE_DEPTH)
  2. AND `participants_normalized()` exactly matches an existing cycle

- With overlapping cycles that have different "shapes" (different entry points,
  different subsets of participants), they never match even though they share
  bindings.

### The Fix

Add an `unwind_stack` check to `pre_calculate_state`:

```rust
fn pre_calculate_state(&mut self, current: &CalcId) -> CycleState {
    if *current == self.break_at {
        CycleState::BreakAt
    } else if let Some(c) = self.recursion_stack.last()
        && *current == *c
    {
        let c = self.recursion_stack.pop().unwrap();
        self.unwind_stack.push(c);
        CycleState::Participant
    } else if self.unwind_stack.contains(current) {
        CycleState::Participant  // The fix: check unwind_stack
    } else {
        CycleState::NoDetectedCycle
    }
}
```

This ensures that bindings accessed during the unwind phase are recognized as
cycle participants, preventing new overlapping cycles from being created.

Note: Checking the entire `recursion_stack` (not just `.last()`) is unnecessary
because the recursion phase is deterministic - we encounter elements in LIFO order.

---

## Updated Hypothesis

The stack overflows are NOT from unbounded chains of distinct bindings.
They're from the SAME bindings being re-encountered through different paths,
creating overlapping cycles that bypass the duplicate detection.

The extremely regular 65-frame pattern supports this: the same computation
is happening repeatedly for the same bindings, not for distinct classes.

---

## Open Questions

1. ~~Can we construct a scenario where infinite recursion actually occurs
   (not prevented by `CycleBroken`)?~~
   **ANSWERED**: Yes - the `pre_calculate_state` bug allows bindings in
   `recursion_stack` (not last) or `unwind_stack` to trigger new cycles.

2. ~~Is the overlapping-cycles behavior in Example 1 related to stack overflow
   reports, or is it a separate issue?~~
   **ANSWERED**: They are the SAME issue. The overlapping cycles from Example 1
   are exactly what causes the stack overflows.

3. Would enforcing proper cycle stacking (each cycle maps to distinct call
   stack segment) simplify the implementation and prevent edge cases?
   **LIKELY YES** - fixing `pre_calculate_state` would prevent overlapping cycles.

4. **NEW**: What Python code patterns produce these unbounded dependency chains?
   Need to find/reproduce the actual source code that triggers P2108270570 etc.

5. ~~Should we add depth limiting as a stopgap before fixing the
   underlying evaluation order issues?~~
   **NO** - fix the root cause in `pre_calculate_state` instead.

---

## True Infinite Recursion vs Quadratic Stack Usage

**Key clarification:** `break_at` is deterministic - it's always the minimum Idx among
cycle participants. For any given set of participants, break_at is always the same.

### Does break_at Alone Prevent Infinite Recursion?

**YES** - break_at prevents TRUE infinite recursion (unbounded, never-terminating).

**Reasoning:**
1. When a cycle is detected, break_at = min(participants)
2. A placeholder is recorded at break_at
3. Any subsequent access to break_at returns CycleBroken(placeholder)
4. This "seals" that binding - no cycle including it can form again

However, overlapping cycles with DIFFERENT participants can still form:
1. C1 forms with {A, B, C, D}, break_at = A (assuming A is min)
2. A gets placeholder (sealed)
3. During C1's unwind, accessing E creates C2 with {B, C, E}, break_at = B
4. B gets placeholder (sealed)
5. During C2's unwind, accessing F creates C3 with {C, E, F}, break_at = C
6. C gets placeholder (sealed)
7. ...continues until all relevant bindings are sealed

### Stack Usage Analysis

With N bindings:
- Maximum number of nested cycles: O(N) (each cycle seals at least one binding)
- Stack frames per cycle: O(N) (each cycle can involve up to N dependencies)
- **Total stack usage: O(N²)**

For programs with thousands of bindings, O(N²) stack frames easily exceeds the
default ~8MB stack limit.

### Explaining the Repeating 65-Frame Patterns

The stack traces show identical repeating patterns because:
1. Each nested cycle involves similar dependency chains
   (synthesized_fields → decorator → annotation → synthesized_fields)
2. These produce identical Rust function call patterns
3. With 100+ nested cycles, the same ~65 frame pattern appears repeatedly

**This is bounded quadratic behavior, not true infinite recursion** - but the
practical effect (stack overflow) is the same.

### Conclusion

- **break_at alone**: Guarantees termination (bounded by O(N²) stack usage)
- **duplicate detection**: Catches cycles with similar structure, potentially
  reducing depth from O(N²) to O(N) in some cases
- **unwind_stack fix**: Would prevent most overlapping cycles, reducing the
  practical depth significantly

The stack overflows are real, but they represent **very deep bounded recursion**
rather than **truly infinite recursion**.